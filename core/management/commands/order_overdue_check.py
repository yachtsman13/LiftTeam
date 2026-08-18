"""
Проверка «зависших» заказов: сколько заказ провёл в текущем статусе без
движения и не превышен ли для этого статуса порог (SLA).

Запускается раз в сутки таймером systemd (deploy/lifteam-order-overdue.timer).
Сама ничего не отправляет — только складывает оповещения в очередь, а
отправляет её потом `send_notifications`. Так у человека остаётся
возможность посмотреть в разделе «Оповещения», что программа собралась
написать, до того как это уйдёт менеджеру по ремонту и администратору.

Ежедневный запуск безопасен: по одному и тому же заказу, пока он не
сдвинется со статуса, оповещение повторяется не чаще, чем раз в
ORDER_OVERDUE_ESCALATION_DAYS (эскалация) — см. `notifications.notify_order_overdue`.
"""
from django.core.management.base import BaseCommand

from core import notifications
from core.models import RepairOrder


class Command(BaseCommand):
    help = 'Ставит в очередь оповещения о заказах, зависших в статусе дольше порога'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Показать, что попало бы в очередь, ничего не ставя',
        )

    def handle(self, *args, **options):
        orders = list(
            RepairOrder.objects.open()
            .select_related('client')
            .order_by('date_received')
        )

        if options['dry_run']:
            stuck = 0
            for order in orders:
                result = notifications.order_stuck_days(order)
                if result is None:
                    continue
                days, threshold = result
                if days < threshold:
                    continue
                stuck += 1
                self.stdout.write(
                    f'[проверка] {order.order_number} — '
                    f'{order.get_status_display()}, {days} дн. без движения '
                    f'(порог {threshold})'
                )
            self.stdout.write(f'Открытых заказов: {len(orders)}, зависших: {stuck}')
            return

        queued = 0
        for order in orders:
            if notifications.notify_order_overdue(order):
                queued += 1

        self.stdout.write(
            f'Открытых заказов: {len(orders)}. В очередь поставлено: {queued}.'
        )
