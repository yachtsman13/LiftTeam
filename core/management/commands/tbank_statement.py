"""
Загрузка выписки по расчётному счёту из Т-Банка.

Запускается таймером systemd (deploy/lifteam-tbank.timer). Только читает
выписку и складывает новые поступления в таблицу: оплатой по заказу
поступление становится тогда, когда человек нажмёт «Разнести» на странице
«Поступления». Автоматически деньги по заказам не расходятся — ошибку
в разнесении потом ищут неделю.

Повторный запуск безопасен: операции опознаются по идентификатору банка,
уже загруженные пропускаются.

Частоту решает **программа**, а не расписание. Таймер только тикает раз
в четверть часа, а тянуть ли выписку на этом тике, отвечает
`tbank.fetch_due()` по настройке `TBANK_STATEMENT_INTERVAL_MINUTES`.
Сделано так потому, что юнит systemd правится от root, а частоту владелец
меняет со страницы «Настройки» — расписание в юните он бы не поправил.
Ручной запуск промежутка не спрашивает: человек, набравший команду,
хочет выписку сейчас.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from core import envfile, tbank


class Command(BaseCommand):
    help = 'Загружает поступления из выписки Т-Банка'

    def add_arguments(self, parser):
        parser.add_argument(
            '--days', type=int, default=None,
            help='За сколько последних дней брать выписку (по умолчанию TBANK_STATEMENT_DAYS)',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Показать, что загрузилось бы, ничего не сохраняя',
        )
        parser.add_argument(
            '--accounts', action='store_true',
            help='Показать расчётные счета организации и выйти — '
                 'чтобы узнать номер для TBANK_ACCOUNT',
        )
        parser.add_argument(
            '--scheduled', action='store_true',
            help='Запуск по расписанию: пропустить, если с прошлой загрузки '
                 'прошло меньше TBANK_STATEMENT_INTERVAL_MINUTES',
        )

    def handle(self, *args, **options):
        if not tbank.is_configured():
            self.stdout.write('Т-Банк не настроен: пустой TBANK_TOKEN. Пропускаю.')
            return

        if options['accounts']:
            self._show_accounts()
            return

        # Промежуток спрашивается только у запуска по расписанию: человек,
        # набравший команду руками, хочет выписку сейчас, а не через час
        if options['scheduled']:
            due, why = tbank.fetch_due()
            if not due:
                self.stdout.write(why)
                return

        days = options['days'] or envfile.setting('TBANK_STATEMENT_DAYS', 30)

        if options['dry_run']:
            date_to = timezone.localdate()
            date_from = date_to - timedelta(days=days)
            try:
                payload = tbank.get_statement(date_from, date_to)
            except tbank.TBankError as exc:
                # Не падаем с трассировкой: команду запускает таймер, и в журнале
                # systemd нужна причина, а не стек
                self.stderr.write(f'Выписка не получена: {exc}')
                return
            operations = tbank.incoming_operations(payload)
            total = len(tbank.operation_list(payload))
            for operation in operations:
                self.stdout.write(
                    f'[проверка] {operation["operation_date"]} '
                    f'{operation["amount"]} ₽ от {operation["counterparty"] or "?"}: '
                    f'{operation["purpose"][:80]}'
                )
            self.stdout.write(
                f'Операций в выписке: {total}, из них поступлений: {len(operations)}'
            )
            return

        # Само сохранение — в core/tbank.py: тот же путь, что у кнопки
        # «Загрузить сейчас» на странице поступлений
        try:
            result = tbank.fetch_and_store(days=days)
        except tbank.TBankError as exc:
            self.stderr.write(f'Выписка не получена: {exc}')
            return
        self.stdout.write(
            f'Выписка с {result["date_from"]} по {result["date_to"]}: '
            f'операций {result["total"]}, поступлений {result["incoming"]}, '
            f'новых {result["added"]}.'
        )

    def _show_accounts(self):
        try:
            payload = tbank.get_accounts()
        except tbank.TBankError as exc:
            self.stderr.write(f'Счета не получены: {exc}')
            return

        # Разбор — в core/tbank.py: его же читает проверка связи
        # на странице настроек, и разойтись им нельзя
        accounts = tbank.account_list(payload)
        if not accounts:
            self.stdout.write('Банк не вернул ни одного счёта')
            return
        for account in accounts:
            number = account.get('accountNumber') or account.get('number') or '?'
            name = account.get('name') or account.get('accountType') or ''
            self.stdout.write(f'{number} {name}'.strip())
