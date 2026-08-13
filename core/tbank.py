"""
Работа с Т-Банком: выписка по расчётному счёту и выставление счетов.

Ни одного метода, который двигает деньги, здесь нет и быть не должно.
Выписка только читается; выставление счёта создаёт документ и отправляет
его заказчику — списать по нему деньги банк не даст никому. Платёжные
поручения — руками в банке.

Выставление счёта вдобавок закрыто отдельным выключателем
`TBANK_INVOICE_ENABLED`: это переписка с заказчиком от лица фирмы,
и начинаться сама собой после обновления она не должна.

Транспорт urllib, как и в core/messengers.py: на Raspberry Pi чем меньше
зависимостей, тем меньше поводов обновлению сломаться.

Про имена полей. Ответ выписки разбирается терпимо — по спискам возможных
имён, а не по одному жёстко заданному. Причина простая: у банка это
не единственный формат ответа, и он менялся (v1 statement, v1 bank-statement,
v3 bank-accounts живут одновременно). Ошибиться в имени поля здесь означает
тихо потерять поступление, поэтому лучше перебрать кандидатов и, если ни один
не подошёл, честно вернуть операцию без этого поля — сотрудник увидит её
в списке и разнесёт руками.
"""
import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from urllib import error, parse, request

from django.conf import settings

DEFAULT_API_URL = 'https://business.tbank.ru/openapi'

# Пути методов. Версии разные — так они и опубликованы у банка
ACCOUNTS_PATH = '/api/v3/bank-accounts'
STATEMENT_PATH = '/api/v1/statement'
INVOICE_PATH = '/api/v1/invoice/send'

# Где в ответе лежит список операций
OPERATION_LIST_KEYS = ('operations', 'items', 'data', 'result', 'transactions')

# Кандидаты на каждое поле операции, в порядке предпочтения
ID_KEYS = ('operationId', 'id', 'operationUuid', 'uuid', 'ucid', 'transactionId')
DATE_KEYS = ('operationDate', 'date', 'chargeDate', 'drawDate', 'authorizationDate',
             'documentDate')
AMOUNT_KEYS = ('accountAmount', 'operationAmount', 'amount', 'sum')
DIRECTION_KEYS = ('typeOfOperation', 'operationType', 'direction', 'type')
PURPOSE_KEYS = ('paymentPurpose', 'purpose', 'description', 'paymentDetails')
COUNTERPARTY_KEYS = ('payerName', 'counterPartyName', 'senderName', 'contractorName')
INN_KEYS = ('payerInn', 'counterPartyInn', 'senderInn', 'contractorInn', 'inn')
DOCUMENT_KEYS = ('documentNumber', 'docNumber', 'number')

# Значения, которыми банк помечает приход. Расход нас не интересует вовсе:
# программа сводит поступления с задолженностью, а не ведёт учёт
CREDIT_VALUES = {'credit', 'income', 'incoming', 'in', 'приход', 'кредит'}


class TBankError(Exception):
    """Обращение к банку не удалось. Текст пригоден для показа человеку."""


def is_configured():
    """Настроена ли выписка. Без токена раздел просто не используется."""
    return bool(token())


def token():
    return getattr(settings, 'TBANK_TOKEN', '')


def account_number():
    return getattr(settings, 'TBANK_ACCOUNT', '')


def api_url():
    # Адрес вынесен в настройки: банк уже переезжал с tinkoff.ru на tbank.ru,
    # и следующий переезд должен чиниться правкой .env, а не кодом
    return getattr(settings, 'TBANK_API_URL', DEFAULT_API_URL).rstrip('/')


def _call(path, params=None, payload=None, timeout=30):
    """Запрос к открытому API банка. Возвращает разобранный ответ.

    POST делается только при явно переданном payload, и единственный, кто
    его передаёт, — выставление счёта. Всё остальное здесь читает.
    """
    if not token():
        raise TBankError('Не задан TBANK_TOKEN')

    url = f'{api_url()}{path}'
    if params:
        url += '?' + parse.urlencode(params)

    data = None
    if payload is not None:
        # ensure_ascii=False: в счёте кириллица, и банку она нужна как есть
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')

    req = request.Request(url, data=data, method='POST' if data else 'GET')
    req.add_header('Authorization', f'Bearer {token()}')
    req.add_header('Accept', 'application/json')
    if data is not None:
        req.add_header('Content-Type', 'application/json')

    try:
        with request.urlopen(req, timeout=timeout) as response:
            body = response.read().decode('utf-8', errors='replace')
    except error.HTTPError as exc:
        detail = exc.read().decode('utf-8', errors='replace')[:300]
        # 401 стоит назвать своим именем: чаще всего это просроченный токен,
        # и человек должен понять, что чинить, не читая кода ответа
        if exc.code == 401:
            raise TBankError('Т-Банк не принял токен (401). Проверьте TBANK_TOKEN') from exc
        raise TBankError(f'Т-Банк ответил {exc.code}: {detail}') from exc
    except error.URLError as exc:
        raise TBankError(f'Т-Банк недоступен: {exc.reason}') from exc
    except TimeoutError as exc:
        raise TBankError('Т-Банк не ответил вовремя') from exc

    try:
        return json.loads(body) if body else {}
    except ValueError:
        raise TBankError(f'Т-Банк вернул не JSON: {body[:200]}')


def get_accounts(timeout=30):
    """Расчётные счета организации — чтобы узнать номер для выписки."""
    return _call(ACCOUNTS_PATH, timeout=timeout)


def get_statement(date_from, date_to, account=None, timeout=60):
    """Выписка за период. Даты — объекты date."""
    account = account or account_number()
    if not account:
        raise TBankError('Не задан TBANK_ACCOUNT — номер расчётного счёта')

    return _call(STATEMENT_PATH, {
        'accountNumber': account,
        'from': date_from.isoformat(),
        'till': date_to.isoformat(),
    }, timeout=timeout)


# --- Разбор ответа -------------------------------------------------------

def _first(source, keys):
    """Первое непустое значение из кандидатов."""
    for key in keys:
        value = source.get(key)
        if value not in (None, '', [], {}):
            return value
    return None


def _as_decimal(value):
    """Сумма из числа, строки или объекта {"value": …, "currency": …}."""
    if isinstance(value, dict):
        value = _first(value, ('value', 'amount', 'sum'))
    if value is None:
        return None
    try:
        # Пробелы-разделители разрядов, в том числе неразрывный
        text = str(value).replace(',', '.').replace(' ', '').replace('\u00a0', '')
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _as_date(value):
    """Дата из «2026-08-13», «2026-08-13T10:20:30+03:00» или «13.08.2026»."""
    if isinstance(value, (int, float)):
        return None
    text = str(value or '').strip()
    if not text:
        return None
    # Отбрасываем время: для сведения с заказами важен день
    head = text.replace('T', ' ').split(' ')[0]
    for fmt in ('%Y-%m-%d', '%d.%m.%Y', '%Y/%m/%d'):
        try:
            return datetime.strptime(head, fmt).date()
        except ValueError:
            continue
    return None


def _is_credit(operation):
    """Приход ли это.

    Если направление не названо вовсе, считаем приходом положительную сумму:
    в некоторых форматах расход приходит отрицательным числом и больше
    ничем не помечен.
    """
    raw = _first(operation, DIRECTION_KEYS)
    if raw is not None:
        return str(raw).strip().lower() in CREDIT_VALUES

    amount = _as_decimal(_first(operation, AMOUNT_KEYS))
    return amount is not None and amount > 0


def operation_list(payload):
    """Список операций из ответа любой из известных форм."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in OPERATION_LIST_KEYS:
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def parse_operation(operation):
    """Одна операция выписки в понятный программе вид.

    Возвращает словарь; None вместо значения означает «банк этого не прислал»,
    и это не повод отбрасывать операцию: сотрудник разнесёт её руками.
    """
    counterparty = operation.get('counterParty') or operation.get('payer') or {}
    if not isinstance(counterparty, dict):
        counterparty = {}

    return {
        'external_id': str(_first(operation, ID_KEYS) or ''),
        'operation_date': _as_date(_first(operation, DATE_KEYS)),
        'amount': _as_decimal(_first(operation, AMOUNT_KEYS)),
        'purpose': str(_first(operation, PURPOSE_KEYS) or ''),
        'counterparty': str(
            _first(operation, COUNTERPARTY_KEYS) or _first(counterparty, ('name', 'title')) or ''
        ),
        'counterparty_inn': str(
            _first(operation, INN_KEYS) or _first(counterparty, ('inn',)) or ''
        ),
        'document_number': str(_first(operation, DOCUMENT_KEYS) or ''),
        'is_credit': _is_credit(operation),
    }


def incoming_operations(payload):
    """Только поступления, только с суммой и опознаваемым идентификатором.

    Операция без идентификатора отброшена намеренно: без него нельзя отличить
    её от такой же в следующей выписке, и одно поступление разошлось бы
    по заказам дважды.
    """
    found = []
    for raw in operation_list(payload):
        parsed = parse_operation(raw)
        if not parsed['is_credit'] or not parsed['external_id']:
            continue
        if parsed['amount'] is None or parsed['amount'] <= 0:
            continue
        found.append(parsed)
    return found


# --- Выставление счёта ---------------------------------------------------
#
# Схема запроса подтверждена по рабочему стороннему SDK
# (github.com/topvisor/tinkoff-sdk-php, src/Business/Invoice*.php):
#
#   POST /api/v1/invoice/send
#   {
#     "invoiceNumber": "943",              — обязателен, задаём МЫ
#     "invoiceDate": "2026-08-13",
#     "dueDate": "2026-08-27",
#     "accountNumber": "40802…",
#     "payer":    {"name": …, "inn": …, "kpp": …},
#     "items":    [{"name", "price", "unit", "vat", "amount"}],
#     "contacts": [{"email": …}]
#   }
#   Ответ: {"pdfUrl": "…"}
#
# Про номер счёта. Банк его НЕ выдаёт — номер приходит от нас и попадает
# в документ как есть. Значит, за сквозной нумерацией следит человек:
# программа предлагает следующий по своим данным, но о счетах, выставленных
# руками в личном кабинете, она не знает и знать не может. Поэтому номер
# на странице выставления открыт для правки, а не подставлен молча.

def invoice_enabled():
    """Разрешено ли выставлять счета.

    Отдельно от токена: читать выписку и писать заказчику от лица фирмы —
    разные по последствиям действия, и включаться они должны порознь.
    """
    return bool(getattr(settings, 'TBANK_INVOICE_ENABLED', False)) and is_configured()


def build_invoice(number, items, payer=None, emails=(), invoice_date=None,
                  due_date=None, account=None):
    """Тело запроса на выставление счёта.

    Чистая функция без сети: то, что она вернула, показывается человеку
    на странице подтверждения слово в слово — иначе «подтверждаю» означало
    бы согласие с тем, чего он не видел.
    """
    payload = {'invoiceNumber': str(number)}

    if invoice_date is not None:
        payload['invoiceDate'] = invoice_date.isoformat()
    if due_date is not None:
        payload['dueDate'] = due_date.isoformat()

    account = account or account_number()
    if account:
        payload['accountNumber'] = account

    if payer:
        # Пустые поля не отправляем вовсе: у части заказчиков нет КПП,
        # и пустая строка в реквизитах счёта выглядит как ошибка
        payload['payer'] = {key: value for key, value in payer.items() if value}

    payload['items'] = [dict(item) for item in items]

    contacts = [{'email': email} for email in emails if email]
    if contacts:
        payload['contacts'] = contacts

    return payload


def send_invoice(payload, timeout=60):
    """Выставляет счёт и отправляет его заказчику. Возвращает ответ банка.

    Единственный метод модуля, который что-то создаёт. Вызывается только
    из представления, только по нажатию человека и только при включённом
    TBANK_INVOICE_ENABLED — по расписанию счета не выставляются.
    """
    if not invoice_enabled():
        raise TBankError(
            'Выставление счетов выключено: TBANK_INVOICE_ENABLED=False '
            'или пустой TBANK_TOKEN'
        )
    if not payload.get('invoiceNumber'):
        raise TBankError('Не задан номер счёта')
    if not payload.get('items'):
        raise TBankError('В счёте нет ни одной позиции')

    data = _call(INVOICE_PATH, payload=payload, timeout=timeout)

    # Ответ 200 ещё не значит «выставлено»: часть отказов банк кладёт в тело
    if isinstance(data, dict) and (data.get('errorCode') or data.get('errorMessage')):
        raise TBankError(
            f'Т-Банк отказал: {data.get("errorMessage") or data.get("errorCode")}'
        )
    return data if isinstance(data, dict) else {}


def invoice_pdf_url(response):
    """Ссылка на PDF счёта из ответа банка. Пусто — банк её не прислал."""
    if not isinstance(response, dict):
        return ''
    return str(_first(response, ('pdfUrl', 'pdf_url', 'url', 'link')) or '')
