"""
Диагностика подключения к Точке: клиенты и счета, customerCode и accountId.

Отдельная команда, не часть какого-то регулярного запуска: в отличие
от Т-Банка, Точка в программе не читает выписку по расписанию — только
выставляет счета по нажатию кнопки на карточке заказа. Эта команда нужна
один раз при настройке (узнать TOCHKA_CUSTOMER_CODE и TOCHKA_ACCOUNT_ID)
и дальше — при отказе банка «Forbidden by consent»: собственная
документация Точки называет неверный customerCode самой частой причиной
этой ошибки, а узнать верный неоткуда, кроме как этим запросом.
Подробности — DEPLOY.md, раздел «Выставление счетов через Точку».
"""
from django.core.management.base import BaseCommand

from core import tochka


class Command(BaseCommand):
    help = 'Показывает клиентов и счета Точки — TOCHKA_CUSTOMER_CODE и TOCHKA_ACCOUNT_ID'

    def handle(self, *args, **options):
        if not tochka.token():
            self.stdout.write('Точка не настроена: пустой TOCHKA_TOKEN. Пропускаю.')
            return

        self._show_customers()
        self.stdout.write('')
        self._show_accounts()

    def _show_customers(self):
        try:
            payload = tochka.get_customers()
        except tochka.TochkaError as exc:
            self.stderr.write(f'Список клиентов не получен: {exc}')
            return

        customers = tochka.customer_list(payload)
        if not customers:
            self.stdout.write(
                'Банк не вернул ни одного клиента в ожидаемом виде. '
                'Сырой ответ ниже — пришлите его для разбора формы:'
            )
            self.stdout.write(str(payload))
            return

        self.stdout.write('Клиенты (для TOCHKA_CUSTOMER_CODE):')
        for customer in customers:
            code = customer.get('customerCode') or customer.get('customer_code') or '?'
            kind = customer.get('customerType') or customer.get('customer_type') or '?'
            name = customer.get('name') or customer.get('customerName') or ''
            marker = (
                '  <-- впишите этот customerCode в настройку TOCHKA_CUSTOMER_CODE'
                if str(kind).lower() == 'business' else ''
            )
            self.stdout.write(f'{code}  [{kind}]  {name}{marker}')

    def _show_accounts(self):
        try:
            payload = tochka.get_accounts()
        except tochka.TochkaError as exc:
            self.stderr.write(f'Список счетов не получен: {exc}')
            return

        accounts = tochka.account_list(payload)
        if not accounts:
            self.stdout.write(
                'Банк не вернул ни одного счёта в ожидаемом виде. '
                'Сырой ответ ниже — пришлите его для разбора формы:'
            )
            self.stdout.write(str(payload))
            return

        self.stdout.write('Счета (для TOCHKA_ACCOUNT_ID):')
        for account in accounts:
            account_id = account.get('accountId') or account.get('account_id') or '?'
            status = account.get('accountStatus') or account.get('status') or ''
            name = account.get('name') or account.get('accountName') or ''
            self.stdout.write(f'{account_id}  {status}  {name}')
