"""
Проверка задолженностей: сводка бухгалтерии и напоминания заказчикам.

Запускается раз в сутки таймером systemd (deploy/lifteam-debts.timer).
Сама ничего не отправляет — только складывает в очередь, а отправляет
её потом `send_notifications`. Так у человека остаётся возможность
посмотреть в разделе «Оповещения», что программа собралась написать
заказчикам, до того как это уйдёт.

Ежедневный запуск безопасен: и сводка, и напоминание по одному заказу
повторяются не чаще, чем раз в паузу из настроек.
"""
from django.core.management.base import BaseCommand

from core import notifications
from core.models import RepairOrder


class Command(BaseCommand):
    help = 'Ставит в очередь сводку по задолженностям и напоминания заказчикам'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Показать, что попало бы в очередь, ничего не ставя',
        )

    def handle(self, *args, **options):
        overdue = list(
            RepairOrder.objects.overdue()
            .select_related('client')
            .order_by('invoice_date')
        )
        without_invoice = list(
            RepairOrder.objects.shipped_without_invoice()
            .select_related('client')
            .order_by('-date_received')
        )

        if options['dry_run']:
            for order in overdue:
                self.stdout.write(f'[проверка] {notifications.debt_line(order)}')
            for order in without_invoice:
                self.stdout.write(
                    f'[проверка] без счёта: {order.order_number} — {order.client.name}'
                )
            self.stdout.write(
                f'Просрочено: {len(overdue)}, отгружено без счёта: {len(without_invoice)}'
            )
            return

        digest = notifications.notify_debt_digest(overdue, without_invoice)

        reminders = 0
        for order in overdue:
            if notifications.notify_debt(order):
                reminders += 1

        self.stdout.write(
            f'Просрочено заказов: {len(overdue)}, '
            f'отгружено без счёта: {len(without_invoice)}. '
            f'В очередь: сводок {len(digest)}, напоминаний заказчикам {reminders}.'
        )
