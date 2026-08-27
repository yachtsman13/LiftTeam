"""Работа с Точка Банком: выставление счетов.

Второй банк в программе. Первый — Т-Банк (`core/tbank.py`); этот модуль
намеренно повторяет его устройство: те же функции уровня модуля, тот же
транспорт urllib, те же правила («ни одного метода, который двигает
деньги»). Общий для обоих интерфейс собран в `core/invoicing.py`.

ЧТО ЗДЕСЬ ПРОВЕРЕНО ПО ДОКУМЕНТАЦИИ, А ЧТО НЕТ
-----------------------------------------------
Сайт документации Точки (developers.tochka.com) из среды разработки
недоступен, поэтому схема запроса собрана из двух источников и сверена
между ними:

* SDK на TypeScript, сгенерированный из OpenAPI-описания самого банка
  (github.com/FatherOctber/tochka-sdk; там же прописан базовый адрес
  `https://enter.tochka.com/uapi/`);
* независимый SDK на Go (github.com/sharpvik/tochka).

Совпало у обоих и считается проверенным:

  POST /invoice/v1.0/bills
  {"Data": {
     "accountId": "40802…/044525104",
     "customerCode": "300000092",
     "SecondSide": {"taxCode": ИНН, "type": "company"|"ip",
                    "secondSideName": …, "kpp": …, "legalAddress": …},
     "Content": {"Invoice": {
        "Positions": [{"positionName", "unitCode", "ndsKind",
                       "price", "quantity", "totalAmount"}],
        "number": "943", "date": "2026-08-13",
        "paymentExpiryDate": "2026-08-27", "totalAmount": 54000}}}}
  Ответ: {"Data": {"documentId": "…"}}

  POST /invoice/v1.0/bills/{customerCode}/{documentId}/email
       {"Data": {"email": "buh@example.ru"}}
  GET  /invoice/v1.0/bills/{customerCode}/{documentId}/payment-status
       → {"Data": {"paymentStatus": "payment_waiting|payment_expired|payment_paid"}}

  Авторизация — JWT в заголовке `Authorization: Bearer …`. Именно JWT,
  а не OAuth 2.0: OAuth в документации предназначен для сервисов, которые
  обслуживают ЧУЖИХ клиентов банка, а мы выставляем счета за себя.

НЕ проверено и ждёт подтверждения на первом настоящем счёте:

* суммы. Сгенерированное из OpenAPI описание объявляет их числами,
  SDK на Go отправляет строками вида «54000.00». Что примет банк —
  неизвестно, поэтому есть переключатель TOCHKA_INVOICE_AMOUNTS_AS_STRING;
* точный вид accountId (счёт и БИК через косую черту) — берётся из .env
  как есть и никак не разбирается;
* обязательность secondSideName.

Ссылки на PDF счёта Точка не отдаёт вовсе: файл забирается методом
`…/file` по токену. Поэтому `invoice_pdf_url` здесь всегда пусто —
это не недоделка, а свойство банка.
"""
import json
import logging
from decimal import Decimal
from urllib import error, request

from django.conf import settings

from . import envfile

from .net import explain, redact, safe_headers

logger = logging.getLogger(__name__)

DEFAULT_API_URL = 'https://enter.tochka.com/uapi'
DEFAULT_API_VERSION = 'v1.0'

# Сколько раз повторять запрос, если сеть не ответила. Повторяются ТОЛЬКО
# чтения: повтор создания счёта после неясного исхода первой попытки
# выставил бы заказчику два счёта с одним номером. Это то же правило,
# по которому в программе нет очереди действий на время обрыва связи.
READ_RETRIES = 2

# Ставки НДС, как их называет Точка
NDS_KINDS = ('without_nds', 'nds_0', 'nds_5', 'nds_7', 'nds_10', 'nds_20')

# Тип второй стороны в счёте
COUNTERPART_COMPANY = 'company'
COUNTERPART_IP = 'ip'


class TochkaError(Exception):
    """Обращение к Точке не удалось. Текст пригоден для показа человеку."""


def token():
    return envfile.setting('TOCHKA_TOKEN', '')


def customer_code():
    return envfile.setting('TOCHKA_CUSTOMER_CODE', '')


def account_id():
    return envfile.setting('TOCHKA_ACCOUNT_ID', '')


def api_url():
    return envfile.setting('TOCHKA_API_URL', DEFAULT_API_URL).rstrip('/')


def api_version():
    return envfile.setting('TOCHKA_API_VERSION', DEFAULT_API_VERSION)


def is_configured():
    """Настроен ли банк. Без токена, кода клиента и счёта — нет."""
    return bool(token() and customer_code() and account_id())


def invoice_enabled():
    """Разрешено ли выставлять счета через Точку.

    Отдельным выключателем, как и у Т-Банка: счёт уходит заказчику
    от лица фирмы, и включаться сам собой после обновления он не должен.
    """
    return bool(envfile.setting('TOCHKA_INVOICE_ENABLED', False)) and is_configured()


def missing_settings():
    """Чего именно не хватает. Нужно, чтобы сказать бухгалтеру, что чинить."""
    absent = []
    if not token():
        absent.append('TOCHKA_TOKEN')
    if not customer_code():
        absent.append('TOCHKA_CUSTOMER_CODE')
    if not account_id():
        absent.append('TOCHKA_ACCOUNT_ID')
    return absent


def _call(path, payload=None, method=None, timeout=30, retries=0):
    """Запрос к API Точки. Возвращает разобранный ответ.

    `retries` больше нуля допустим только для чтений — см. READ_RETRIES.
    """
    if not token():
        raise TochkaError('Не задан TOCHKA_TOKEN')

    url = f'{api_url()}{path}'
    data = None
    if payload is not None:
        # ensure_ascii=False: в счёте кириллица, и банку она нужна как есть
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')

    method = method or ('POST' if data else 'GET')
    headers = {
        'Authorization': f'Bearer {token()}',
        'Accept': 'application/json',
    }
    if data is not None:
        headers['Content-Type'] = 'application/json'

    # В журнал — адрес, метод и заголовки без секретов. Тело запроса
    # не пишем: в нём реквизиты заказчика
    logger.info('Точка: %s %s, заголовки %s', method, url, safe_headers(headers))

    body = None
    last_error = None
    for attempt in range(retries + 1):
        req = request.Request(url, data=data, method=method)
        for name, value in headers.items():
            req.add_header(name, value)
        try:
            with request.urlopen(req, timeout=timeout) as response:
                body = response.read().decode('utf-8', errors='replace')
            break
        except error.HTTPError as exc:
            detail = redact(exc.read().decode('utf-8', errors='replace')[:300], token())
            logger.warning('Точка ответила %s: %s', exc.code, detail)
            if exc.code == 401:
                raise TochkaError(
                    'Точка не приняла токен (401). Проверьте TOCHKA_TOKEN'
                ) from exc
            raise TochkaError(f'Точка ответила {exc.code}: {detail}') from exc
        except error.URLError as exc:
            last_error = TochkaError(f'Точка недоступна: {explain(exc.reason)}')
        except TimeoutError:
            last_error = TochkaError('Точка не ответила вовремя')
        if attempt < retries:
            logger.warning('Точка: %s, попытка %s из %s',
                           last_error, attempt + 1, retries + 1)
    else:
        raise last_error

    try:
        return json.loads(body) if body else {}
    except ValueError:
        raise TochkaError(f'Точка вернула не JSON: {redact(body[:200], token())}')


# --- Сборка счёта --------------------------------------------------------

def _amount(value):
    """Сумма в том виде, в каком её ждёт банк.

    Какой из двух видов верен — не подтверждено (см. шапку модуля), поэтому
    выбор оставлен настройкой, а не зашит в код.
    """
    quantized = Decimal(str(value)).quantize(Decimal('0.01'))
    if envfile.setting('TOCHKA_INVOICE_AMOUNTS_AS_STRING', False):
        return str(quantized)
    return float(quantized)


def counterpart_type(inn):
    """«ип» или «компания» по длине ИНН.

    У ИП ИНН двенадцатизначный, у организации — десятизначный. Если ИНН
    не заполнен или длина другая, считаем организацией: так выглядит
    подавляющее большинство заказчиков программы.
    """
    return COUNTERPART_IP if len(str(inn or '').strip()) == 12 else COUNTERPART_COMPANY


def build_invoice(number, items, payer=None, invoice_date=None, due_date=None,
                  account=None, customer=None):
    """Тело запроса на выставление счёта.

    Чистая функция без сети: то, что она вернула, показывается человеку
    на странице подтверждения слово в слово — как и у Т-Банка.

    Позиции приходят в общем для обоих банков виде
    (`name`, `price`, `unit`, `vat`, `amount`) и переводятся здесь
    в имена полей Точки.
    """
    payer = payer or {}
    nds = envfile.setting('TOCHKA_INVOICE_NDS', 'without_nds')
    if nds not in NDS_KINDS:
        raise TochkaError(
            f'Неизвестная ставка НДС «{nds}» в TOCHKA_INVOICE_NDS. '
            f'Допустимы: {", ".join(NDS_KINDS)}'
        )

    # Единицу берём свою, а не из позиции: в позициях стоит значение
    # TBANK_INVOICE_UNIT, а списки допустимых единиц у банков разные,
    # и настройка одного банка не должна уезжать в запрос к другому
    unit = envfile.setting('TOCHKA_INVOICE_UNIT', 'шт.')

    positions = []
    total = Decimal('0')
    for item in items:
        price = Decimal(str(item['price']))
        quantity = Decimal(str(item.get('amount', 1)))
        line_total = price * quantity
        total += line_total
        positions.append({
            'positionName': item['name'],
            'unitCode': unit,
            'ndsKind': nds,
            'price': _amount(price),
            'quantity': float(quantity),
            'totalAmount': _amount(line_total),
        })

    second_side = {
        'taxCode': str(payer.get('inn') or ''),
        'type': counterpart_type(payer.get('inn')),
    }
    # Пустые поля не отправляем вовсе: у части заказчиков нет КПП,
    # и пустая строка в реквизитах счёта выглядит как ошибка
    for source, target in (('name', 'secondSideName'), ('kpp', 'kpp'),
                           ('address', 'legalAddress')):
        if payer.get(source):
            second_side[target] = payer[source]

    invoice = {
        'Positions': positions,
        'number': str(number),
        'totalAmount': _amount(total),
    }
    if invoice_date is not None:
        invoice['date'] = invoice_date.isoformat()
    if due_date is not None:
        invoice['paymentExpiryDate'] = due_date.isoformat()

    return {'Data': {
        'accountId': account or account_id(),
        'customerCode': customer or customer_code(),
        'SecondSide': second_side,
        'Content': {'Invoice': invoice},
    }}


def send_invoice(payload, timeout=60):
    """Выставляет счёт. Возвращает ответ банка.

    Повторов здесь нет и быть не должно: неясный исход первой попытки
    плюс повтор — это два счёта с одним номером у заказчика.
    """
    if not invoice_enabled():
        absent = missing_settings()
        raise TochkaError(
            'Выставление счетов через Точку выключено: '
            + ('TOCHKA_INVOICE_ENABLED=False' if not absent
               else 'не заполнено ' + ', '.join(absent))
        )

    data = (payload or {}).get('Data') or {}
    invoice = ((data.get('Content') or {}).get('Invoice')) or {}
    if not invoice.get('number'):
        raise TochkaError('Не задан номер счёта')
    if not invoice.get('Positions'):
        raise TochkaError('В счёте нет ни одной позиции')
    if not data.get('accountId'):
        raise TochkaError('Не задан TOCHKA_ACCOUNT_ID — идентификатор счёта')
    if not data.get('customerCode'):
        raise TochkaError('Не задан TOCHKA_CUSTOMER_CODE — код клиента')

    answer = _call(f'/invoice/{api_version()}/bills', payload=payload, timeout=timeout)

    # Ответ 200 ещё не значит «выставлено»: часть отказов банк кладёт в тело
    if isinstance(answer, dict) and answer.get('errors'):
        raise TochkaError(f'Точка отказала: {_error_text(answer)}')
    if not document_id(answer):
        raise TochkaError('Точка не вернула идентификатор счёта')
    return answer


def _error_text(answer):
    """Текст отказа из ответа банка — насколько его удаётся разобрать."""
    errors = answer.get('errors') or []
    parts = []
    for item in errors if isinstance(errors, list) else [errors]:
        if isinstance(item, dict):
            parts.append(str(item.get('message') or item.get('errorCode') or item))
        else:
            parts.append(str(item))
    return '; '.join(parts)[:300] or 'причина не названа'


def document_id(response):
    """Идентификатор выставленного счёта. Он же нужен для статуса и PDF."""
    if not isinstance(response, dict):
        return ''
    return str((response.get('Data') or {}).get('documentId') or '')


def invoice_pdf_url(response):
    """У Точки ссылки на PDF нет: файл отдаётся по токену методом …/file.

    Возвращаем пусто осознанно, чтобы не подставлять в заказ адрес,
    который заказчик всё равно не откроет.
    """
    return ''


def send_invoice_to_email(document, email, timeout=30):
    """Отправляет уже выставленный счёт на почту.

    В Точке это отдельный метод: создание счёта письма не шлёт.
    """
    return _call(
        f'/invoice/{api_version()}/bills/{customer_code()}/{document}/email',
        payload={'Data': {'email': email}}, timeout=timeout,
    )


def payment_status(document, timeout=30):
    """Статус оплаты счёта. Чтение — значит, повторы допустимы."""
    answer = _call(
        f'/invoice/{api_version()}/bills/{customer_code()}/{document}/payment-status',
        method='GET', timeout=timeout, retries=READ_RETRIES,
    )
    if not isinstance(answer, dict):
        return ''
    return str((answer.get('Data') or {}).get('paymentStatus') or '')
