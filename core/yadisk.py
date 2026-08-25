"""Яндекс.Диск: папка заказа под фотографии ремонта.

Зачем. У каждой единицы в заказе есть поле «Папка на Яндекс.Диске» —
туда мастер складывает снимки платы до и после, фото пломб, экран стенда.
До сих пор папку заводили руками в веб-интерфейсе Диска и вручную же
вставляли сюда ссылку. Ошибиться в этом легко, и тогда снимки одного
ремонта уезжают в папку другого.

Теперь папку заводит программа: одно нажатие на карточке заказа —
и папка создана по единому пути, а ссылка записана в единицу.

ЧТО ЗДЕСЬ ПРОВЕРЕНО, А ЧТО НЕТ
------------------------------
**На живом Диске не проверено ничего.** Сайт документации Яндекса
из среды разработки недоступен (как и документация Точки), поэтому схема
запросов написана по памяти о REST API Диска, а не сверена с описанием.
Порядок тот же, что и с Точкой: код написан, обвешан проверками
и понятными отказами, но первое обращение к настоящему Диску обязано
пройти под присмотром — и то, что подтвердится, надо записать сюда.

Что считается верным и на чём всё держится:

  Базовый адрес   https://cloud-api.yandex.net/v1/disk
  Заголовок       Authorization: OAuth <токен>     (именно OAuth, не Bearer)
  Создать папку   PUT  /resources?path=disk:/путь  → 201; 409 — уже есть
  Сведения        GET  /resources?path=disk:/путь  → {type, path, ...}
  Опубликовать    PUT  /resources/publish?path=... → потом GET даёт public_url

  Ошибки приходят JSON-ом с полями `message`, `description`, `error`.
  Родительские папки сами не создаются: путь заводится по одному
  уровню сверху вниз.

Чего здесь нет и не будет без отдельного решения:

* **загрузки файлов из программы.** Снимки кладут с телефона, а телефон
  и так умеет класть их на Диск; программа даёт папку и ссылку на неё.
  Заводить в программе загрузку — это ещё и очередь, и повторы,
  и место на карте памяти под то, что уже лежит на Диске;
* **публикации папки наружу по умолчанию.** Публичная ссылка открывается
  кем угодно, а внутри — снимки оборудования заказчика. Ссылка, которую
  программа записывает, ведёт в веб-интерфейс Диска и открывается только
  тем, кто вошёл в этот аккаунт. Публиковать можно отдельным действием
  и осознанно;
* **удаления чего-либо.** Ни одной функции, стирающей на Диске, здесь
  нет и быть не должно — по той же причине, по которой в банковских
  модулях нет платёжных поручений.
"""
import json
import logging
import re
from urllib import error, parse, request

from django.conf import settings

from .net import explain, redact, safe_headers

logger = logging.getLogger(__name__)

DEFAULT_API_URL = 'https://cloud-api.yandex.net/v1/disk'
DEFAULT_ROOT = 'LiftTeam'

# Куда складываются папки заказов внутри корня
ORDERS_DIR = 'Заказы'

# Веб-интерфейс Диска: ссылка для человека, а не для программы.
# Открывается только тем, кто вошёл в этот аккаунт, — в отличие
# от публичной ссылки, которую открывает кто угодно.
WEB_BASE = 'https://disk.yandex.ru/client/disk'

# Что Диск в имени не принимает. Серийные номера приходят с завода
# и содержат что угодно, вплоть до косой черты — а она на Диске означает
# новый уровень пути, то есть тихо превратила бы одну папку в две.
FORBIDDEN = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


class YandexDiskError(Exception):
    """Обращение к Диску не удалось. Текст пригоден для показа человеку."""


def token():
    return getattr(settings, 'YANDEX_DISK_TOKEN', '')


def api_url():
    return getattr(settings, 'YANDEX_DISK_API_URL', DEFAULT_API_URL).rstrip('/')


def root():
    """Корневая папка программы на Диске.

    Отдельная, а не весь Диск: там лежит и личное владельца, и мы туда
    не ходим вовсе.
    """
    return getattr(settings, 'YANDEX_DISK_ROOT', DEFAULT_ROOT).strip('/')


def is_configured():
    return bool(token())


def unconfigured_reason():
    """Чего не хватает — словами, для показа человеку.

    Пустая строка означает «всё на месте». Отдельная функция, а не текст
    в представлении: одну и ту же причину показывают несколько экранов.
    """
    if not token():
        return ('Не задан YANDEX_DISK_TOKEN. Токен получает владелец '
                'в приложении OAuth Яндекса (см. DEPLOY.md).')
    return ''


def safe_name(name):
    """Имя папки, которое Диск примет.

    Запрещённые знаки заменяются подчёркиванием, а не выбрасываются:
    из «SN/12» и «SN12» получились бы одинаковые имена, и снимки двух
    приборов легли бы в одну папку.
    """
    cleaned = FORBIDDEN.sub('_', (name or '').strip())
    # Точки по краям Диск тоже не любит, а пустое имя не имя вовсе
    cleaned = cleaned.strip('. ')
    return cleaned or 'без-номера'


def unit_path(order_equipment):
    """Путь папки одной единицы: LiftTeam/Заказы/<заказ>/<серийник>.

    Считается одним местом: разойдись он между кнопкой и проверкой,
    и снимки одного ремонта уезжали бы в папку другого — ровно то,
    ради чего папку и заводит программа.
    """
    order = order_equipment.repair_order
    return '/'.join([
        root(),
        ORDERS_DIR,
        safe_name(order.order_number),
        safe_name(order_equipment.equipment.serial_number),
    ])


def web_url(path):
    """Ссылка для человека — в веб-интерфейс Диска."""
    quoted = '/'.join(parse.quote(part) for part in path.split('/') if part)
    return f'{WEB_BASE}/{quoted}'


def _call(method, path, params=None, timeout=30):
    """Запрос к API Диска. Возвращает разобранный ответ и код.

    Код нужен наверх: 409 при создании папки означает «уже есть», а это
    не ошибка, а обычный ход дела.
    """
    if not token():
        raise YandexDiskError(unconfigured_reason())

    url = f'{api_url()}{path}'
    if params:
        url += '?' + parse.urlencode(params)

    req = request.Request(url, method=method)
    # Именно OAuth, а не Bearer: у Диска свой заголовок, и с Bearer
    # он отвечает 401
    req.add_header('Authorization', f'OAuth {token()}')
    req.add_header('Accept', 'application/json')

    # В журнал — адрес, метод и заголовки без секретов: журнал живёт
    # до ротации и уезжает в резервную копию, то есть токен в нём
    # перестал бы быть секретом
    logger.info('Яндекс.Диск: %s %s, заголовки %s',
                method, url, safe_headers(req.headers))

    try:
        with request.urlopen(req, timeout=timeout) as response:
            body = response.read().decode('utf-8', errors='replace')
            code = response.status
    except error.HTTPError as exc:
        body = exc.read().decode('utf-8', errors='replace')
        code = exc.code
        if code != 409:
            detail = redact(body[:300], token())
            logger.warning('Яндекс.Диск ответил %s: %s', code, detail)
            if code == 401:
                raise YandexDiskError(
                    'Яндекс.Диск не принял токен (401). Проверьте '
                    'YANDEX_DISK_TOKEN — он выдаётся не навсегда.'
                ) from exc
            # Объяснение Диска тоже проходит через redact: в ответ
            # попадает то, что мы послали, и токен из него ушёл бы
            # человеку на экран, а оттуда в переписку
            spoken = redact(_message(body), token()) or detail
            raise YandexDiskError(
                f'Яндекс.Диск ответил {code}: {spoken}'
            ) from exc
    except error.URLError as exc:
        raise YandexDiskError(f'Яндекс.Диск недоступен: {explain(exc.reason)}') from exc
    except TimeoutError as exc:
        raise YandexDiskError('Яндекс.Диск не ответил вовремя') from exc

    try:
        return code, (json.loads(body) if body.strip() else {})
    except ValueError:
        raise YandexDiskError(
            f'Яндекс.Диск вернул не JSON: {redact(body[:200], token())}'
        )


def _message(body):
    """Человеческое объяснение из ответа Диска, если оно там есть."""
    try:
        data = json.loads(body)
    except ValueError:
        return ''
    if not isinstance(data, dict):
        return ''
    return data.get('message') or data.get('description') or ''


def _disk_path(path):
    return 'disk:/' + path.strip('/')


def create_folder(path, timeout=30):
    """Создать одну папку. True — создали, False — уже была.

    409 ошибкой не считается: два мастера могут нажать кнопку по одной
    и той же единице почти одновременно, и вторым ответом будет «уже есть».
    """
    code, _ = _call('PUT', '/resources', {'path': _disk_path(path)}, timeout=timeout)
    return code != 409


def ensure_folder(path, timeout=30):
    """Создать папку вместе со всеми недостающими родителями.

    Диск родителей сам не заводит: путь создаётся по одному уровню
    сверху вниз. Возвращает список того, что пришлось создать, — пустой,
    если всё уже было.
    """
    created = []
    parts = [part for part in path.strip('/').split('/') if part]
    for depth in range(1, len(parts) + 1):
        current = '/'.join(parts[:depth])
        if create_folder(current, timeout=timeout):
            created.append(current)
    return created


def ensure_unit_folder(order_equipment, timeout=30):
    """Папка под снимки одной единицы. Возвращает ссылку для человека."""
    path = unit_path(order_equipment)
    ensure_folder(path, timeout=timeout)
    return web_url(path)
