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

Ссылки на PDF счёта Точка не отдаёт вовсе: файл забирается отдельным
запросом по document_id (`get_invoice_pdf`). Поэтому `invoice_pdf_url`
здесь всегда пусто — это не недоделка, а свойство банка; ссылку
«Открыть PDF» на карточке заказа собирает `invoicing.TochkaProvider.pdf_link`
через прокси-представление, а не эта функция.

Путь подтверждён тем же способом, что у клиентов и счетов (два независимых
источника сходятся): TypeScript SDK, сгенерированный из OpenAPI банка
(github.com/FatherOctber/tochka-sdk, tochka-api.ts, метод getInvoice,
`GET /invoice/{apiVersion}/bills/{customerCode}/{documentId}/file`) и
независимый Go SDK (github.com/sharpvik/tochka, invoice.go,
`GetInvoicePDF`, тот же путь). Ответ — не JSON-обёртка, а голый файл.

Чтение выписки (`init_statement`/`get_statement`, с v2.108.0) устроено
**иначе**, чем у Т-Банка, и это не наше решение, а свойство банка: выписка
асинхронная. `POST /open-banking/{apiVersion}/statements` только просит
её подготовить и отдаёт `statementId` в статусе Created; готова она
становится через Processing, и добывать её надо отдельным опросом —
`GET /open-banking/{apiVersion}/accounts/{accountId}/statements/{statementId}`
— пока статус не станет Ready (или Error). Подтверждено **одним**
источником, а не двумя, как обычно в этом модуле: у независимого Go SDK
выписки нет вовсе, зато оба метода и все поля сверены с сырым
OpenAPI-описанием банка (`spec/tochka-api-spec.json` в репозитории
TypeScript SDK) — то есть напрямую с тем, что банк сам о себе заявляет,
и это не слабее второго SDK. Даты в запросе — **YYYY-MM-DD**, без времени
(в отличие от Т-Банка, которому нужен полный RFC 3339); поле операции —
`Transaction[]` внутри готовой выписки, приход отличается от расхода
полем `creditDebitIndicator` (`Credit`/`Debit`), а плательщик при приходе —
`DebtorParty.name`/`.inn`. Форма самого конверта ответа (`Data.Statement`
— объект у создания, список у получения, как и предписывает схема)
на живом счёте не проверена — разбор терпимый, тем же приёмом, что
у клиентов и счетов.
"""
import json
import logging
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib import error, request

from django.conf import settings
from django.utils import timezone

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


def statement_configured():
    """Настроено ли чтение выписки — у неё требования мягче выставления
    счетов: `customerCode` заявке на выписку не нужен вовсе (см. схему
    в шапке модуля), и просить его настраивать было бы лишним шагом
    для того, кто хочет только читать поступления."""
    return bool(token() and account_id())


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


def _call(path, payload=None, method=None, timeout=30, retries=0, raw=False):
    """Запрос к API Точки. Возвращает разобранный ответ.

    `retries` больше нуля допустим только для чтений — см. READ_RETRIES.

    `raw=True` — для единственного бинарного метода банка (PDF счёта,
    `get_invoice_pdf`): тело возвращается байтами как есть, без
    `json.loads`, и `Accept` не сужается до `application/json`.
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
        'Accept': 'application/json' if not raw else '*/*',
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
                body = response.read() if raw else response.read().decode('utf-8', errors='replace')
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

    if raw:
        return body

    try:
        return json.loads(body) if body else {}
    except ValueError:
        raise TochkaError(f'Точка вернула не JSON: {redact(body[:200], token())}')


# --- Клиенты: узнать свой customerCode ------------------------------------
#
# Появилось после первого настоящего отказа банка — 403 «Forbidden by
# consent». Собственная страница Точки «Вопросы и ошибки» называет эту
# ошибку частой и первой причиной — неверный TOCHKA_CUSTOMER_CODE, а
# способ узнать верный один: этот метод, объект с полем
# customerType: "Business". У Точки нет команды вроде --accounts
# у Т-Банка, а customerCode подобрать неоткуда — отсюда и такая же по духу
# команда здесь: core/management/commands/tochka_check.py.
#
# Путь подтверждён дважды независимо: он совпадает у стороннего PHP SDK
# (github.com/lee-to/php-tochka-api-v2-sdk, Models/Customer.php) и у их же
# самих ссылок на редок-документацию в README. Форма обёртки ответа
# (Data.Customer, голый список и т.п.) не подтверждена ничем — сайт
# документации из среды разработки недоступен, поэтому разбор терпимый,
# как и у выписки Т-Банка.

def get_customers(timeout=30):
    """Список клиентов Точки — чтобы найти свой customerCode."""
    return _call('/open-banking/v1.0/customers', method='GET', timeout=timeout)


def customer_list(payload):
    """Клиенты из ответа — одним местом, терпимо к форме обёртки."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ('Data', 'data', 'Customer', 'customers', 'items', 'result'):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                nested = customer_list(value)
                if nested:
                    return nested
    return []


def business_customer_code(payload):
    """customerCode вашей организации — из объекта с customerType Business.

    Точное имя поля с кодом и типом клиента подтверждено документацией
    Точки текстом («customerCode», «customerType: "Business"»), но не
    подтверждено на живом ответе — сверяем терпимо, по паре кандидатов,
    тем же приёмом, что и у полей операции в выписке Т-Банка.
    """
    for customer in customer_list(payload):
        kind = str(customer.get('customerType') or customer.get('customer_type') or '')
        if kind.lower() == 'business':
            code = customer.get('customerCode') or customer.get('customer_code')
            if code:
                return str(code)
    return None


# --- Счета: узнать свой TOCHKA_ACCOUNT_ID ---------------------------------
#
# Тот же вопрос, что и с customerCode, — код клиента не подобрать
# из реквизитов, а `accountId` тем более: это счёт и БИК банка через
# косую черту одной строкой («40802.../044525104»), а не то, что печатают
# в справке о счёте. Путь метода подтверждён примером curl из самой
# документации Точки, страница «Авторизация по JWT-токену»:
# `GET /uapi/open-banking/v1.0/accounts` — тот же формат, что у клиентов,
# только без customerType.

def get_accounts(timeout=30):
    """Список счетов Точки — чтобы найти свой TOCHKA_ACCOUNT_ID."""
    return _call('/open-banking/v1.0/accounts', method='GET', timeout=timeout)


def account_list(payload):
    """Счета из ответа — тем же терпимым разбором, что и у клиентов
    (`customer_list`) и у выписки Т-Банка (`tbank.account_list`)."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ('Data', 'data', 'Account', 'accounts', 'items', 'result'):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                nested = account_list(value)
                if nested:
                    return nested
    return []


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
    """У Точки ссылки на PDF нет: файл достаётся отдельным запросом
    по document_id, а не по прямому адресу — см. `get_invoice_pdf`.

    Возвращаем пусто осознанно, чтобы не подставлять в заказ адрес,
    который заказчик всё равно не откроет.
    """
    return ''


def get_invoice_pdf(document_id, timeout=30):
    """PDF выставленного счёта — байты, не ссылка (путь подтверждён
    дважды независимо, см. шапку модуля).

    Тот же customerCode и токен, что и у выставления счёта; второго
    согласия банк не спрашивает. Читающий запрос — с повтором, как
    у остальных чтений этого модуля.
    """
    if not customer_code():
        raise TochkaError('Не задан TOCHKA_CUSTOMER_CODE')
    if not document_id:
        raise TochkaError('Нет идентификатора счёта в Точке')

    path = f'/invoice/{api_version()}/bills/{customer_code()}/{document_id}/file'
    pdf = _call(path, method='GET', timeout=timeout, retries=READ_RETRIES, raw=True)
    if not pdf:
        raise TochkaError('Точка вернула пустой файл счёта')
    return pdf


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


# --- Выписка: поступления по расчётному счёту -----------------------------
#
# Устроена не так, как у Т-Банка (см. шапку модуля) — асинхронно: запрос
# только просит подготовить выписку, готова она становится не сразу.
# Общий приём (get_or_create по external_id, свой файл отметки о последней
# загрузке, решение «пора ли тянуть» — программой, а не расписанием) тот
# же, что у Т-Банка, и он же ниже.

def _date_only(value):
    """Дата в виде YYYY-MM-DD — так, как её просит Точка (не RFC 3339)."""
    if hasattr(value, 'strftime'):
        return value.strftime('%Y-%m-%d')
    return str(value)


def _as_date(value):
    """Дата из «2026-08-13» или «2026-08-13T10:20:30»."""
    text = str(value or '').strip()
    if not text:
        return None
    head = text.replace('T', ' ').split(' ')[0]
    try:
        return datetime.strptime(head, '%Y-%m-%d').date()
    except ValueError:
        return None


def _as_decimal(value):
    """Сумма из числа или строки — без разбора вложенных объектов: у Точки
    сумма транзакции всегда простое число (`Amount.amount`)."""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def init_statement(date_from, date_to, account=None, timeout=30):
    """Просит банк подготовить выписку — не читает её саму.

    Готовность нужно опрашивать отдельно (`get_statement`) по вернувшемуся
    `statementId`: банк готовит выписку асинхронно, и в самом ответе
    на этот запрос операций ещё нет.
    """
    account = account or account_id()
    if not account:
        raise TochkaError('Не задан TOCHKA_ACCOUNT_ID — идентификатор счёта')

    payload = {'Data': {'Statement': {
        'accountId': account,
        'startDateTime': _date_only(date_from),
        'endDateTime': _date_only(date_to),
    }}}
    return _call(f'/open-banking/{api_version()}/statements', payload=payload, timeout=timeout)


def get_statement(account, statement, timeout=30):
    """Текущее состояние заявленной выписки — Created/Processing/Ready/Error.

    Чтение — значит, повторы допустимы, как и у остальных чтений банка.
    """
    return _call(
        f'/open-banking/{api_version()}/accounts/{account}/statements/{statement}',
        method='GET', timeout=timeout, retries=READ_RETRIES,
    )


def _statement_object(payload):
    """Единственная запись о выписке из ответа — терпимо к обеим формам:
    у создания `Data.Statement` объект, у получения — список из одного
    элемента (так предписывает схема банка, и это не наша прихоть)."""
    if not isinstance(payload, dict):
        return {}
    data = payload.get('Data')
    if not isinstance(data, dict):
        return {}
    statement = data.get('Statement')
    if isinstance(statement, list):
        return statement[0] if statement and isinstance(statement[0], dict) else {}
    if isinstance(statement, dict):
        return statement
    return {}


def statement_id(payload):
    """Идентификатор подготавливаемой выписки — из ответа на создание."""
    return str(_statement_object(payload).get('statementId') or '')


def statement_status(payload):
    """Created / Processing / Ready / Error — прогресс подготовки."""
    return str(_statement_object(payload).get('status') or '')


def transaction_list(payload):
    """Операции готовой выписки. Пока статус не Ready, их в ответе нет —
    это не повод считать разбор сломанным."""
    transactions = _statement_object(payload).get('Transaction')
    if isinstance(transactions, list):
        return [item for item in transactions if isinstance(item, dict)]
    return []


def parse_transaction(item):
    """Одна операция выписки в понятный программе вид.

    Имена ключей результата те же, что у `tbank.parse_operation`
    намеренно: `fetch_and_store` ниже собирает `BankOperation` тем же
    кодом, что и у Т-Банка, а два разных банка с двумя разными формами
    результата означали бы либо дублирование этого кода, либо перевод
    одной формы в другую в третьем месте.
    """
    debtor = item.get('DebtorParty')
    if not isinstance(debtor, dict):
        debtor = {}
    amount = item.get('Amount')
    if not isinstance(amount, dict):
        amount = {}
    return {
        'external_id': str(item.get('transactionId') or ''),
        'operation_date': _as_date(item.get('documentProcessDate')),
        'amount': _as_decimal(amount.get('amount')),
        'purpose': str(item.get('description') or ''),
        # DebtorParty — контрагент именно для кредитной операции (банк
        # называет это в описании поля буквально), то есть для прихода
        # это и есть плательщик
        'counterparty': str(debtor.get('name') or ''),
        'counterparty_inn': str(debtor.get('inn') or ''),
        'document_number': str(item.get('documentNumber') or ''),
        'is_credit': str(item.get('creditDebitIndicator') or '').lower() == 'credit',
    }


def incoming_transactions(payload):
    """Только поступления, только с суммой и опознаваемым идентификатором —
    тот же отбор, что у `tbank.incoming_operations`, и по той же причине:
    без идентификатора операцию не отличить от такой же в следующей
    выписке, и она задвоилась бы по заказам."""
    found = []
    for raw in transaction_list(payload):
        parsed = parse_transaction(raw)
        if not parsed['is_credit'] or not parsed['external_id']:
            continue
        if parsed['amount'] is None or parsed['amount'] <= 0:
            continue
        found.append(parsed)
    return found


# --- Отметка о последней загрузке — тот же приём, что у tbank.py ---------

LAST_FETCH_FILE = Path(settings.BASE_DIR) / '.tochka-last-run'


def last_fetch_at():
    """Когда выписку тянули в последний раз. None — ни разу."""
    try:
        raw = LAST_FETCH_FILE.read_text(encoding='utf-8').strip()
    except OSError:
        return None
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if timezone.is_naive(moment):
        moment = timezone.make_aware(moment)
    return moment


def mark_fetched(when=None):
    """Запомнить, что выписка загружена."""
    moment = when or timezone.now()
    try:
        LAST_FETCH_FILE.write_text(
            moment.isoformat(timespec='seconds'), encoding='utf-8'
        )
    except OSError as exc:
        logger.warning('Отметку о загрузке выписки Точки не записать: %s', exc)


def statement_interval():
    """Через сколько минут после прошлой загрузки тянуть снова."""
    try:
        return max(0, int(envfile.setting('TOCHKA_STATEMENT_INTERVAL_MINUTES', 60)))
    except (TypeError, ValueError):
        return 60


def fetch_due(now=None):
    """Пора ли тянуть выписку. Возвращает (пора, объяснение) — решает
    программа, а не расписание systemd; подробности у tbank.fetch_due,
    приём тот же."""
    interval = statement_interval()
    if not interval:
        return True, 'Промежуток не задан — тянем на каждом тике.'
    previous = last_fetch_at()
    if previous is None:
        return True, 'Выписку ещё не тянули ни разу.'
    now = now or timezone.now()
    passed = (now - previous).total_seconds() / 60
    if passed >= interval:
        return True, 'С прошлой загрузки прошло %d мин при промежутке %d.' % (
            passed, interval,
        )
    return False, (
        'С прошлой загрузки прошло %d мин, а тянуть договорились раз '
        'в %d. Пропускаю.' % (passed, interval)
    )


# --- Незавершённая заявка на выписку между тиками -------------------------
#
# Своя, потому что выписка асинхронная (см. шапку модуля): не дождавшись
# готовности на этом тике, нельзя молча заказать вторую заявку на
# следующем — банк готовил бы обе, а разбираться, какая из них наша,
# было бы нечем. Тот же приём, что у файла отметки о загрузке: состояние
# установки, а не данные, в облачную копию ему уезжать незачем.

PENDING_STATEMENT_FILE = Path(settings.BASE_DIR) / '.tochka-pending-statement'


def _pending_statement():
    try:
        raw = PENDING_STATEMENT_FILE.read_text(encoding='utf-8')
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict) or not data.get('statement_id') or not data.get('account_id'):
        return None
    return data


def _remember_pending(account, statement):
    try:
        PENDING_STATEMENT_FILE.write_text(
            json.dumps({'account_id': account, 'statement_id': statement}),
            encoding='utf-8',
        )
    except OSError as exc:
        logger.warning('Заявку на выписку Точки не запомнить: %s', exc)


def _forget_pending():
    try:
        PENDING_STATEMENT_FILE.unlink()
    except OSError:
        pass


# Сколько раз опросить готовность в рамках одного вызова и с какой паузой.
# Не настройка: это не про то, как часто тянуть выписку (для этого есть
# TOCHKA_STATEMENT_INTERVAL_MINUTES), а про то, сколько подождать готовности
# внутри одного обращения, прежде чем отложить заявку до следующего —
# кнопки или тика по расписанию.
STATEMENT_POLL_ATTEMPTS = 4
STATEMENT_POLL_DELAY_SECONDS = 3


class TochkaFetchResult(dict):
    """Итог загрузки выписки — для команды и для кнопки на странице.
    Тот же приём, что у `tbank.TBankFetchResult`."""


def fetch_and_store(days=None, dry_run=False):
    """Тянет выписку Точки и сохраняет новые поступления — одно место.

    Зовут и команда по расписанию, и кнопка «Загрузить сейчас» на странице
    поступлений — тот же приём, что у Т-Банка, и по той же причине: второй
    такой код разошёлся бы с первым, а склад денег — не то место, где
    это можно позволить.

    Не дождавшись готовности за `STATEMENT_POLL_ATTEMPTS` попыток, заявка
    остаётся записанной (`_remember_pending`) — следующий вызов опросит
    ту же самую, а не закажет новую. `mark_fetched()` в этом случае
    не зовётся: следующий тик обязан попробовать снова, не дожидаясь
    полного `TOCHKA_STATEMENT_INTERVAL_MINUTES`.

    `dry_run=True` — только посмотреть, что загрузилось бы: банк
    запрашивается по-настоящему (дешевле не бывает, выписка асинхронная
    в любом случае), но ни `BankOperation`, ни отметка о загрузке
    не пишутся. Список операций для показа — в результате, ключом
    `transactions`, а не отдельным возвращаемым значением: второй вид
    результата для одного и того же вызова расходился бы с обычным.
    """
    from datetime import timedelta

    from django.db import IntegrityError

    from . import notifications
    from .models import BankOperation

    account = account_id()
    if not account:
        raise TochkaError('Не задан TOCHKA_ACCOUNT_ID — идентификатор счёта')

    days = days or envfile.setting('TOCHKA_STATEMENT_DAYS', 30)
    date_to = timezone.localdate()
    date_from = date_to - timedelta(days=days)

    pending = _pending_statement()
    if pending and pending.get('account_id') == account:
        statement = pending['statement_id']
    else:
        answer = init_statement(date_from, date_to, account=account)
        statement = statement_id(answer)
        if not statement:
            raise TochkaError('Точка не вернула идентификатор выписки')
        _remember_pending(account, statement)

    payload, status = None, ''
    for attempt in range(STATEMENT_POLL_ATTEMPTS):
        payload = get_statement(account, statement)
        status = statement_status(payload)
        if status in ('Ready', 'Error'):
            break
        if attempt < STATEMENT_POLL_ATTEMPTS - 1:
            time.sleep(STATEMENT_POLL_DELAY_SECONDS)

    if status == 'Error':
        _forget_pending()
        raise TochkaError('Банк не подготовил выписку (статус Error)')
    if status != 'Ready':
        raise TochkaError('Точка ещё готовит выписку — попробуйте через минуту-другую')

    _forget_pending()

    transactions = incoming_transactions(payload)
    total = len(transaction_list(payload))

    if dry_run:
        return TochkaFetchResult(
            date_from=date_from, date_to=date_to,
            total=total, incoming=len(transactions), added=0,
            transactions=transactions,
        )

    added = 0
    for txn in transactions:
        try:
            stored, created = BankOperation.objects.get_or_create(
                source='tochka', external_id=txn['external_id'],
                defaults={
                    'operation_date': txn['operation_date'],
                    'amount': txn['amount'],
                    'purpose': txn['purpose'],
                    'counterparty': txn['counterparty'],
                    'counterparty_inn': txn['counterparty_inn'],
                    'document_number': txn['document_number'],
                },
            )
        except IntegrityError:
            continue
        if created:
            added += 1
            notifications.notify_new_payment(stored)

    mark_fetched()

    return TochkaFetchResult(
        date_from=date_from, date_to=date_to,
        total=total, incoming=len(transactions), added=added,
    )
