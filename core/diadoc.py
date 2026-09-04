"""
УПД через Диадок — генерация титула продавца, с v2.109.0.

Что это и чего это не делает
-----------------------------
Программа собирает данные заказа в XML и просит Диадок сгенерировать
из него правильно оформленный УПД (по схеме ФНС), функция **ДОП** —
только передаточный документ, без счёта-фактуры: у обоих юрлиц —
«Без НДС, применяется УСН» (`Organization.tax_note`).

Дальше — **вручную**: готовый файл скачивает бухгалтер, заходит
в веб-интерфейс Диадока под своей учётной записью (там подключён
токен КЭП — у Т-Банка и у Точки они разные, у разных бухгалтеров)
и там подписывает и отправляет документ заказчику. Программа не
подписывает документ и не отправляет его сама:

* API Диадока в принципе не создаёт подпись под документом — это
  прямо написано в его документации («Технические особенности»),
  и остаётся действием того, у кого сертификат;
* даже будь у нас техническая возможность подписать, `PostMessage`
  (метод, которым документ уходит контрагенту) требует уже готовый
  файл подписи в теле запроса — а он и есть то единственное, что
  наша сторона предоставить не может.

Как подтверждено это устройство
--------------------------------
Первоисточник — сама документация Диадока (`developer.kontur.ru`),
недоступная из среды разработки: разбирались по сохранённым
страницам, которые прислал заказчик, а не угадывали по чужим SDK.
Подтверждены:

* адреса API — `https://diadoc-api.kontur.ru` (рабочий),
  `https://diadoc-api-staging.kontur.ru` (тестовый), OIDC-провайдер —
  `identity.kontur.ru`;
* авторизация — OpenID Connect, `Device Authorization Flow`
  (подходит для консольной команды без браузера на сервере):
  `POST /connect/deviceauthorization` → человек открывает ссылку
  и логинится → `POST /connect/token` до получения токена;
* `access_token` живёт недолго, обновляется `refresh_token`
  (scope `offline_access`) без повторного входа;
* обычные методы (`GetDocumentTypes`, `GetMyOrganizations`,
  `GetCounteragents`) — JSON, `Content-Type: application/json`;
* `GenerateTitleXml` — **не JSON**, тело запроса — `UserDataXml`,
  упрощённый XML по XSD, `Content-Type: application/xml`;
* полная структура титула продавца для функции ДОП, включая точное
  значение «без НДС» (`TaxRate="NoVat"`, `WithoutVat="true"` и на
  таблице, и на строке) — из рабочего примера на странице
  «Работа с актами» (`instructions/documents/formal/acceptcert.html`):
  документооборот УПД(ДОП) устроен как у актов, и там дан готовый
  минимальный XML целиком, не «выжимка» из СЧФДОП-примера.

Что не подтверждено и потребует первого живого вызова
-------------------------------------------------------
* `documentVersion` (например, `utd970_05_03_01`) — берётся из ответа
  `GetDocumentTypes` для конкретного ящика, а не считается постоянной:
  ФНС меняет версии формата (5.03 сменила 5.01/5.02 в начале 2025),
  и то, что включено в вашем ящике, правильнее спросить у Диадока,
  чем зашивать в код;
* структура `<Address><RussianAddress ...>` — у `Organization`
  и `Client` адрес хранится одной строкой, а не по полям (регион,
  индекс, город, улица, дом), и надёжно разобрать её на части
  нельзя. Сюда идёт то, что есть (город и остаток строки в `OtherInfo`),
  а обязательность отдельных полей адреса по XSD не проверена —
  если Диадок откажет из-за адреса, это будет видно по тексту ошибки
  сразу, а не молча;
* `SignerPowersConfirmationMethod="6"` — код «по уставу» (директор
  без доверенности), взят из того же примера как правдоподобный,
  а не найден в отдельном справочнике кодов;
* `FnsParticipantId` — свой (`Organization.diadoc_fns_participant_id`)
  и заказчика (`Client.diadoc_fns_participant_id`) нужно узнать
  через `GetMyOrganizations`/`GetCounteragents` и вписать один раз
  в карточку юрлица/заказчика: угадать их неоткуда, это не ИНН,
  а внутренний идентификатор участника ЭДО.

Секреты — `DIADOC_CLIENT_SECRET` и `DIADOC_REFRESH_TOKEN`, читаются
через `envfile.setting`, как у всех банковских модулей. `refresh_token`
Диадок может выдавать новый при каждом обновлении — на этот случай
`refresh_access_token()` каждый раз перезаписывает его в `.env`,
иначе через один обмен старое значение перестало бы приниматься.
"""
import json
import logging
from datetime import date
from urllib import error, parse, request
from xml.sax.saxutils import quoteattr

from django.conf import settings

from . import envfile

from .net import explain, redact, safe_headers

logger = logging.getLogger(__name__)

DEFAULT_API_URL = 'https://diadoc-api.kontur.ru'
DEFAULT_OIDC_URL = 'https://identity.kontur.ru'

# Устаревший, а не серверный (client_credentials) способ получения токена —
# в документации Диадока такого нет вовсе, только два интерактивных: код
# из редиректа (нужен бэкенд с публичным адресом) и код устройства (нужен
# только человек с браузером один раз). Для консольной команды на Pi
# подходит второй
SCOPE = 'openid profile email offline_access Diadoc.PublicAPI'


class DiadocError(Exception):
    """Обращение к Диадоку не удалось. Текст пригоден для показа человеку."""


def client_id():
    return envfile.setting('DIADOC_CLIENT_ID', '')


def client_secret():
    return envfile.setting('DIADOC_CLIENT_SECRET', '')


def refresh_token():
    return envfile.setting('DIADOC_REFRESH_TOKEN', '')


def box_id():
    return envfile.setting('DIADOC_BOX_ID', '')


def api_url():
    return envfile.setting('DIADOC_API_URL', DEFAULT_API_URL).rstrip('/')


def oidc_url():
    return envfile.setting('DIADOC_OIDC_URL', DEFAULT_OIDC_URL).rstrip('/')


def is_configured():
    """Достаточно ли данных, чтобы обращаться к Диадоку вообще.

    `refresh_token` сюда не входит: его отсутствие — это не «не настроено»,
    а «не пройден вход», о чём `manage.py diadoc_login` и говорит отдельно.
    """
    return bool(client_id() and client_secret() and box_id())


def missing_settings():
    """Чего не хватает — сказать бухгалтеру, что чинить."""
    absent = []
    if not client_id():
        absent.append('DIADOC_CLIENT_ID')
    if not client_secret():
        absent.append('DIADOC_CLIENT_SECRET')
    if not box_id():
        absent.append('DIADOC_BOX_ID')
    if not refresh_token():
        absent.append('DIADOC_REFRESH_TOKEN (вход: manage.py diadoc_login)')
    return absent


# --- OpenID Connect: Device Authorization Flow ----------------------------
#
# Подходит консольной команде на сервере без своего адреса для редиректа:
# приложение получает код, человек открывает ссылку в любом браузере
# (на телефоне или на компьютере) и логинится там сам.

def _oidc_post(path, params, timeout=30):
    """POST с телом application/x-www-form-urlencoded — так, как этого
    просит OpenID-провайдер Диадока (не JSON, в отличие от самого API)."""
    url = f'{oidc_url()}{path}'
    data = parse.urlencode(params).encode('utf-8')
    req = request.Request(url, data=data, method='POST')
    req.add_header('Content-Type', 'application/x-www-form-urlencoded')
    logger.info('Диадок (OIDC): POST %s', url)
    try:
        with request.urlopen(req, timeout=timeout) as response:
            body = response.read().decode('utf-8', errors='replace')
    except error.HTTPError as exc:
        detail = redact(exc.read().decode('utf-8', errors='replace')[:300],
                        client_secret(), refresh_token())
        raise DiadocError(f'Диадок (вход) ответил {exc.code}: {detail}') from exc
    except error.URLError as exc:
        raise DiadocError(f'Диадок недоступен: {explain(exc.reason)}') from exc
    except TimeoutError:
        raise DiadocError('Диадок не ответил вовремя') from None
    try:
        return json.loads(body) if body else {}
    except ValueError:
        raise DiadocError(f'Диадок (вход) вернул не JSON: {body[:200]}')


def start_device_authorization():
    """Шаг 1 входа: получить код для человека и код для приложения.

    Возвращает словарь с `device_code`, `user_code`, `verification_uri_complete`,
    `interval`, `expires_in` — ровно то, что описывает документация.
    """
    if not client_id() or not client_secret():
        raise DiadocError('Не заданы DIADOC_CLIENT_ID/DIADOC_CLIENT_SECRET')
    return _oidc_post('/connect/deviceauthorization', {
        'client_id': client_id(),
        'client_secret': client_secret(),
        'scope': SCOPE,
    })


def poll_device_token(device_code):
    """Шаг 2: обменять код устройства на токен — одна попытка.

    Пока человек не залогинился, Диадок отвечает `authorization_pending` —
    это не ошибка входа, а сигнал попробовать ещё раз; отличает его
    вызывающий код (команда `diadoc_login`), а не этот модуль.
    """
    return _oidc_post('/connect/token', {
        'grant_type': 'urn:ietf:params:oauth:grant-type:device_code',
        'client_id': client_id(),
        'client_secret': client_secret(),
        'device_code': device_code,
    })


def refresh_access_token():
    """Обменять refresh_token на свежий access_token.

    Диадок может вернуть новый `refresh_token` вместе с ответом — сохраняем
    его тем же движением, что и секреты банков: следующий обмен обязан
    использовать именно этот, иначе он перестанет приниматься.
    """
    if not refresh_token():
        raise DiadocError(
            'Не пройден вход в Диадок: DIADOC_REFRESH_TOKEN пуст. '
            'Выполните manage.py diadoc_login'
        )
    answer = _oidc_post('/connect/token', {
        'grant_type': 'refresh_token',
        'client_id': client_id(),
        'client_secret': client_secret(),
        'refresh_token': refresh_token(),
    })
    access = answer.get('access_token')
    if not access:
        raise DiadocError('Диадок не вернул access_token при обновлении')
    new_refresh = answer.get('refresh_token')
    if new_refresh and new_refresh != refresh_token():
        try:
            envfile.set_value('DIADOC_REFRESH_TOKEN', new_refresh, allow_secrets=True)
        except envfile.EnvFileError as exc:
            # Не роняем обновление токена из-за этого: доступ на сейчас есть,
            # а без записи следующий вызов попробует обновиться снова тем же
            # (уже недействительным) refresh_token и явно скажет об этом
            logger.warning('Не удалось сохранить новый DIADOC_REFRESH_TOKEN: %s', exc)
    return access


# --- Вызовы самого API: JSON --------------------------------------------

def _call_json(path, payload=None, method=None, timeout=30):
    """JSON-вызов обычного метода API (не генерация титула — у неё свой,
    `generate_title_xml`, тело которого не JSON)."""
    access = refresh_access_token()
    url = f'{api_url()}{path}'
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8') if payload is not None else None
    method = method or ('POST' if data else 'GET')
    headers = {
        'Authorization': f'Bearer {access}',
        'Accept': 'application/json',
    }
    if data is not None:
        headers['Content-Type'] = 'application/json; charset=utf-8'

    logger.info('Диадок: %s %s, заголовки %s', method, url, safe_headers(headers))
    req = request.Request(url, data=data, method=method)
    for name, value in headers.items():
        req.add_header(name, value)
    try:
        with request.urlopen(req, timeout=timeout) as response:
            body = response.read().decode('utf-8', errors='replace')
    except error.HTTPError as exc:
        detail = redact(exc.read().decode('utf-8', errors='replace')[:500], access, client_secret())
        raise DiadocError(f'Диадок ответил {exc.code}: {detail}') from exc
    except error.URLError as exc:
        raise DiadocError(f'Диадок недоступен: {explain(exc.reason)}') from exc
    except TimeoutError:
        raise DiadocError('Диадок не ответил вовремя') from None
    try:
        return json.loads(body) if body else {}
    except ValueError:
        raise DiadocError(f'Диадок вернул не JSON: {redact(body[:300], access)}')


def get_document_types(timeout=30):
    """Типы документов, доступные ящику, — чтобы узнать точную включённую
    версию формата УПД (`documentVersion`), а не держать её постоянной
    в коде: ФНС меняет версии формата время от времени."""
    return _call_json(f'/V3/GetDocumentTypes?boxId={box_id()}', method='GET', timeout=timeout)


def get_my_organizations(timeout=30):
    """Организации своего ящика — среди прочего, здесь ищется
    `FnsParticipantId` своего юрлица для `Organization.diadoc_fns_participant_id`."""
    return _call_json('/GetMyOrganizations', method='GET', timeout=timeout)


def get_counteragents(timeout=30):
    """Контрагенты ящика — здесь ищется `FnsParticipantId` заказчика
    для `Client.diadoc_fns_participant_id`, после того как он принял
    приглашение к обмену документами."""
    return _call_json(f'/V3/GetCounteragents?boxId={box_id()}', method='GET', timeout=timeout)


# --- Генерация титула УПД: XML, не JSON -----------------------------------

def generate_title_xml(document_type_named_id, document_function, document_version,
                       title_index, user_data_xml, timeout=30):
    """Просит Диадок собрать по нашим данным правильно оформленный титул.

    В отличие от прочих методов — тело запроса `application/xml`
    (упрощённый UserDataXml по XSD), не JSON. Ответ читаем как JSON
    (`Accept: application/json`) — предполагается, что он возвращает
    структуру `GeneratedFile` с содержимым в base64; это не проверено
    на живом вызове (см. шапку модуля) и должно быть первым, что
    сверяется при настоящей настройке.
    """
    access = refresh_access_token()
    query = parse.urlencode({
        'boxId': box_id(),
        'documentTypeNamedId': document_type_named_id,
        'documentFunction': document_function,
        'documentVersion': document_version,
        'titleIndex': title_index,
    })
    url = f'{api_url()}/GenerateTitleXml?{query}'
    data = user_data_xml.encode('utf-8')
    headers = {
        'Authorization': f'Bearer {access}',
        'Content-Type': 'application/xml; charset=utf-8',
        'Accept': 'application/json',
    }
    logger.info('Диадок: POST %s, заголовки %s', url, safe_headers(headers))
    req = request.Request(url, data=data, method='POST')
    for name, value in headers.items():
        req.add_header(name, value)
    try:
        with request.urlopen(req, timeout=timeout) as response:
            body = response.read().decode('utf-8', errors='replace')
    except error.HTTPError as exc:
        detail = redact(exc.read().decode('utf-8', errors='replace')[:1000], access, client_secret())
        raise DiadocError(f'Диадок не принял титул УПД ({exc.code}): {detail}') from exc
    except error.URLError as exc:
        raise DiadocError(f'Диадок недоступен: {explain(exc.reason)}') from exc
    except TimeoutError:
        raise DiadocError('Диадок не ответил вовремя') from None
    try:
        return json.loads(body) if body else {}
    except ValueError:
        raise DiadocError(f'Диадок вернул не JSON: {redact(body[:300], access)}')


# --- Сборка UserDataXml для функции ДОП по данным заказа -------------------
#
# Пример взят целиком со страницы «Работа с актами» документации Диадока
# (документооборот УПД с функцией ДОП устроен как у актов) — это готовый,
# минимальный, рабочий XML, а не выжимка из примера СЧФДОП с полями,
# которые нашему случаю не нужны (грузоотправитель/грузополучатель,
# факторинг, госконтракт и т.п. — всё это для одного прибора в ремонте
# неприменимо и в XML не попадает).

# Функция УПД: только передаточный документ, без счёта-фактуры — решение
# владельца. НДС нет ни у нас, ни в счёте (`Organization.tax_note`)
FUNCTION = 'ДОП'

# Название документа — фиксированная формулировка формата приказа №970,
# а не наша, её менять нельзя
DOCUMENT_NAME = (
    'Документ об отгрузке товаров (выполнении работ), передаче '
    'имущественных прав (документ об оказании услуг)'
)

# Рубль, ISO 4217 — общий классификатор, не специфичный для Диадока
CURRENCY_RUB = '643'

# Единица измерения «услуга» — ОКЕИ 796 «Штука»: по одной строке на единицу
# оборудования, как и в счёте (`RepairOrder.invoice_items`). Общероссийский
# классификатор, а не значение, которое придумал Диадок, — его можно
# держать постоянным
UNIT_PIECE = ('796', 'шт.')

# «По уставу» — типичное основание подписи для директора без доверенности.
# Взято из примера в документации как правдоподобное значение, а не
# из отдельного справочника кодов — см. шапку модуля
SIGNER_POWERS_BY_CHARTER = '6'


def _xml_escape_attr(value):
    """Значение атрибута XML — с экранированием и без пустых кавычек:
    пустой атрибут в упрощённом XML для Диадока не то же самое,
    что отсутствующий."""
    return quoteattr(str(value))


def _organization_details_xml(*, org_name, inn, kpp, fns_participant_id, address_text):
    """`<OrganizationDetails>` — общий вид что для продавца, что для
    покупателя (пример Диадока их не различает).

    Адрес хранится в `Organization`/`Client` одной строкой, а не по полям
    (регион, индекс, город, улица) — надёжно разобрать её на части
    нельзя, поэтому целиком идёт в `OtherInfo`. Обязательность отдельных
    полей `RussianAddress` по XSD не проверена (см. шапку модуля).
    """
    kpp_attr = f' Kpp={_xml_escape_attr(kpp)}' if kpp else ''
    fns_attr = f' FnsParticipantId={_xml_escape_attr(fns_participant_id)}' if fns_participant_id else ''
    address = (address_text or '').strip()
    address_xml = (
        f'<Address><RussianAddress OtherInfo={_xml_escape_attr(address)} /></Address>'
        if address else ''
    )
    return (
        f'<OrganizationDetails{fns_attr} OrgType="2" '
        f'OrgName={_xml_escape_attr(org_name)} Inn={_xml_escape_attr(inn)}{kpp_attr}>'
        f'{address_xml}'
        '</OrganizationDetails>'
    )


def build_utd_dop_user_data_xml(order):
    """Собирает `UserDataXml` для титула продавца УПД (функция ДОП)
    по данным заказа.

    Позиции — те же, что в счёте (`RepairOrder.invoice_items`): по строке
    на единицу оборудования со стоимостью, без НДС. Заказ без единиц
    со стоимостью документу не даёт ничего — печатать пустой УПД
    незачем, как и пустой счёт.
    """
    from .models import Organization

    seller = order.legal_entity()
    if seller is None or not isinstance(seller, Organization):
        raise DiadocError('Не определено юрлицо заказа — нечем заполнить продавца')
    if not seller.diadoc_fns_participant_id:
        raise DiadocError(
            f'У юрлица «{seller.name}» не заполнен идентификатор участника ЭДО '
            '(FnsParticipantId) — узнайте его через GetMyOrganizations '
            'и впишите в карточку юрлица'
        )

    client = order.client
    if not client.inn:
        raise DiadocError(f'У заказчика «{client.name}» не заполнен ИНН')
    if not client.diadoc_fns_participant_id:
        raise DiadocError(
            f'У заказчика «{client.name}» не заполнен идентификатор участника ЭДО '
            '(FnsParticipantId) — доступен через GetCounteragents только после '
            'того, как заказчик примет приглашение к обмену документами в Диадоке'
        )

    items = order.invoice_items()
    if not items:
        raise DiadocError('В заказе нет ни одной единицы со стоимостью — печатать нечего')

    unit_code, unit_name = UNIT_PIECE
    total = sum(item['price'] for item in items)

    item_lines = []
    for item in items:
        price = item['price']
        item_lines.append(
            f'<Item TaxRate="NoVat" Product={_xml_escape_attr(item["name"])} '
            f'Unit="{unit_code}" UnitName={_xml_escape_attr(unit_name)} '
            f'Quantity="{item["amount"]}" Price="{price:.2f}" '
            f'SubtotalWithVatExcluded="{price:.2f}" WithoutVat="true" '
            f'Subtotal="{price:.2f}" />'
        )

    today = date.today().strftime('%d.%m.%Y')
    document_creator = (
        f'{seller.name}, ИНН {seller.inn}' + (f', КПП {seller.kpp}' if seller.kpp else '')
    )

    seller_xml = _organization_details_xml(
        org_name=seller.name, inn=seller.inn, kpp=seller.kpp,
        fns_participant_id=seller.diadoc_fns_participant_id,
        address_text=seller.address,
    )
    buyer_xml = _organization_details_xml(
        org_name=client.name, inn=client.inn, kpp=client.kpp,
        fns_participant_id=client.diadoc_fns_participant_id,
        address_text=client.address,
    )

    signer_position = seller.signatory_position or 'Директор'
    signer_name_parts = (seller.signatory_name or '').split()
    last_name, first_name, middle_name = (signer_name_parts + ['', '', ''])[:3]

    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<UniversalTransferDocument DocumentDate="{today}" '
        f'DocumentNumber={_xml_escape_attr(order.order_number)} '
        f'Currency="{CURRENCY_RUB}" Function="{FUNCTION}" '
        f'DocumentName={_xml_escape_attr(DOCUMENT_NAME)} '
        f'SenderFnsParticipantId={_xml_escape_attr(seller.diadoc_fns_participant_id)} '
        f'RecipientFnsParticipantId={_xml_escape_attr(client.diadoc_fns_participant_id)} '
        f'DocumentCreator={_xml_escape_attr(document_creator)} '
        'xmlns:xs="http://www.w3.org/2001/XMLSchema">'
        f'<Sellers><Seller>{seller_xml}</Seller></Sellers>'
        f'<Buyers><Buyer>{buyer_xml}</Buyer></Buyers>'
        f'<Table TotalWithVatExcluded="{total:.2f}" WithoutVat="true" Total="{total:.2f}">'
        f'{"".join(item_lines)}'
        '</Table>'
        f'<TransferInfo OperationInfo="Ремонт лифтового оборудования" TransferDate="{today}" />'
        '<Signers><Signer SignerPowersConfirmationMethod='
        f'"{SIGNER_POWERS_BY_CHARTER}">'
        f'<Fio FirstName={_xml_escape_attr(first_name)} '
        f'LastName={_xml_escape_attr(last_name)} '
        f'MiddleName={_xml_escape_attr(middle_name)} />'
        f'<Position PositionSource="Manual">{signer_position}</Position>'
        '</Signer></Signers>'
        '</UniversalTransferDocument>'
    )


def generated_file_bytes(payload):
    """Содержимое сгенерированного файла из ответа `GenerateTitleXml`.

    Форма ответа (имя поля с base64-содержимым) не подтверждена живым
    вызовом — перебираются правдоподобные варианты, тем же терпимым
    приёмом, что у выписки Точки и Т-Банка."""
    import base64

    for key in ('Content', 'content', 'FileContent', 'Bytes'):
        value = payload.get(key) if isinstance(payload, dict) else None
        if value:
            try:
                return base64.b64decode(value)
            except (ValueError, TypeError) as exc:
                raise DiadocError(f'Не удалось разобрать содержимое файла: {exc}')
    raise DiadocError(
        f'В ответе Диадока не найдено содержимое файла: {list(payload.keys()) if isinstance(payload, dict) else payload}'
    )
