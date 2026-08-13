"""
Загрузка выписки по расчётному счёту из Т-Банка.

Запускается таймером systemd (deploy/lifteam-tbank.timer). Только читает
выписку и складывает новые поступления в таблицу: оплатой по заказу
поступление становится тогда, когда человек нажмёт «Разнести» на странице
«Поступления». Автоматически деньги по заказам не расходятся — ошибку
в разнесении потом ищут неделю.

Повторный запуск безопасен: операции опознаются по идентификатору банка,
уже загруженные пропускаются.
"""
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import IntegrityError
from django.utils import timezone

from core import tbank
from core.models import BankOperation


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

    def handle(self, *args, **options):
        if not tbank.is_configured():
            self.stdout.write('Т-Банк не настроен: пустой TBANK_TOKEN. Пропускаю.')
            return

        if options['accounts']:
            self._show_accounts()
            return

        days = options['days'] or getattr(settings, 'TBANK_STATEMENT_DAYS', 30)
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

        if options['dry_run']:
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

        added = self._store(operations)
        self.stdout.write(
            f'Выписка с {date_from} по {date_to}: операций {total}, '
            f'поступлений {len(operations)}, новых {added}.'
        )

    def _show_accounts(self):
        try:
            payload = tbank.get_accounts()
        except tbank.TBankError as exc:
            self.stderr.write(f'Счета не получены: {exc}')
            return

        accounts = payload if isinstance(payload, list) else payload.get('accounts', [])
        if not accounts:
            self.stdout.write('Банк не вернул ни одного счёта')
            return
        for account in accounts:
            if isinstance(account, dict):
                number = account.get('accountNumber') or account.get('number') or '?'
                name = account.get('name') or account.get('accountType') or ''
                self.stdout.write(f'{number} {name}'.strip())

    def _store(self, operations):
        added = 0
        for operation in operations:
            # get_or_create, а не exists()+create: выписку может тянуть
            # и таймер, и человек со страницы одновременно
            try:
                _, created = BankOperation.objects.get_or_create(
                    external_id=operation['external_id'],
                    defaults={
                        'operation_date': operation['operation_date'],
                        'amount': operation['amount'],
                        'purpose': operation['purpose'],
                        'counterparty': operation['counterparty'],
                        'counterparty_inn': operation['counterparty_inn'],
                        'document_number': operation['document_number'],
                    },
                )
            except IntegrityError:
                continue
            if created:
                added += 1
        return added
