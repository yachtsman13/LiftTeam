"""
Загрузка выписки по расчётному счёту из Точки.

Запускается таймером systemd (deploy/lifteam-tochka.timer). Только читает
выписку и складывает новые поступления в таблицу: оплатой по заказу
поступление становится тогда, когда человек нажмёт «Разнести» на странице
«Поступления». Автоматически деньги по заказам не расходятся.

Устроено иначе, чем у Т-Банка: выписка у Точки готовится асинхронно
(Created → Processing → Ready), и команда ждёт готовности несколько
попыток подряд, а не дождавшись — не роняет заявку, а откладывает её
до следующего запуска (см. `core/tochka.py`, `fetch_and_store`).

Повторный запуск безопасен: операции опознаются по идентификатору банка,
уже загруженные пропускаются.

Частоту решает **программа**, а не расписание — тот же приём, что у
Т-Банка, и по той же причине: юнит systemd правится от root, а частоту
владелец меняет со страницы «Настройки».
"""
from django.core.management.base import BaseCommand

from core import envfile, tochka


class Command(BaseCommand):
    help = 'Загружает поступления из выписки Точки'

    def add_arguments(self, parser):
        parser.add_argument(
            '--days', type=int, default=None,
            help='За сколько последних дней брать выписку (по умолчанию TOCHKA_STATEMENT_DAYS)',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Показать, что загрузилось бы, ничего не сохраняя',
        )
        parser.add_argument(
            '--scheduled', action='store_true',
            help='Запуск по расписанию: пропустить, если с прошлой загрузки '
                 'прошло меньше TOCHKA_STATEMENT_INTERVAL_MINUTES',
        )

    def handle(self, *args, **options):
        if not tochka.statement_configured():
            self.stdout.write(
                'Точка не настроена для чтения выписки: нужны TOCHKA_TOKEN '
                'и TOCHKA_ACCOUNT_ID. Пропускаю.'
            )
            return

        # Промежуток спрашивается только у запуска по расписанию: человек,
        # набравший команду руками, хочет выписку сейчас, а не через час
        if options['scheduled']:
            due, why = tochka.fetch_due()
            if not due:
                self.stdout.write(why)
                return

        days = options['days'] or envfile.setting('TOCHKA_STATEMENT_DAYS', 30)

        # Само сохранение — в core/tochka.py: тот же путь, что у кнопки
        # «Загрузить сейчас» на странице поступлений
        try:
            result = tochka.fetch_and_store(days=days, dry_run=options['dry_run'])
        except tochka.TochkaError as exc:
            self.stderr.write(f'Выписка не получена: {exc}')
            return

        if options['dry_run']:
            for txn in result['transactions']:
                self.stdout.write(
                    f'[проверка] {txn["operation_date"]} '
                    f'{txn["amount"]} ₽ от {txn["counterparty"] or "?"}: '
                    f'{txn["purpose"][:80]}'
                )

        self.stdout.write(
            f'Выписка с {result["date_from"]} по {result["date_to"]}: '
            f'операций {result["total"]}, поступлений {result["incoming"]}, '
            f'новых {result["added"]}.'
        )
