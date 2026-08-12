"""
Отправка сообщений в мессенджер MAX.

Только транспорт: что и кому писать, решает core/notifications.py, а когда
отправлять — команда send_notifications. Здесь один HTTP-запрос и разбор
ответа.

Почему urllib, а не requests: на Raspberry Pi чем меньше зависимостей, тем
меньше поводов обновлению сломаться. Запрос ровно один и совсем простой —
ради него тянуть библиотеку не стоит.

Бот заводится в самом MAX у @MasterBot командой /create, тот выдаёт токен.
Написать первым бот не может: человек должен сам ему написать хоть раз,
после чего его числовой идентификатор виден в `manage.py max_updates`.
"""
import json
from urllib import error, parse, request

from django.conf import settings

# Значение по умолчанию вынесено в настройки не от хорошей жизни: адрес
# API уже менялся (старый platform-api.max.ru отключили летом 2026),
# и когда он сменится снова, это должно чиниться правкой .env, а не кодом.
DEFAULT_API_URL = 'https://platform-api2.max.ru'

# Личный диалог и групповой чат адресуются разными параметрами запроса
RECIPIENT_PARAMS = {
    'user': 'user_id',
    'chat': 'chat_id',
}


class MaxError(Exception):
    """Отправить не удалось. Текст пригоден для показа в очереди оповещений."""


def is_configured():
    """Настроен ли MAX. Без токена канал просто не используется."""
    return bool(getattr(settings, 'MAX_BOT_TOKEN', ''))


def api_url():
    return getattr(settings, 'MAX_API_URL', DEFAULT_API_URL).rstrip('/')


def format_recipient(kind, value):
    """«user», «842910» → «user:842910». Обратное разбирается через split(':')."""
    return f'{kind}:{value}'


def _parse_recipient(recipient):
    kind, _, value = str(recipient).partition(':')
    param = RECIPIENT_PARAMS.get(kind)
    if not param or not value:
        raise MaxError(
            f'Непонятный получатель MAX: {recipient!r}. '
            'Ожидается «user:<id>» или «chat:<id>»'
        )
    return param, value


def send_max_message(recipient, text, timeout=15):
    """Отправляет текст одному получателю MAX.

    При любой неудаче поднимает MaxError с человекочитаемой причиной:
    вызывающий код записывает её в очередь и повторяет попытку позже.
    """
    token = getattr(settings, 'MAX_BOT_TOKEN', '')
    if not token:
        raise MaxError('Не задан MAX_BOT_TOKEN')

    param, value = _parse_recipient(recipient)
    url = f'{api_url()}/messages?' + parse.urlencode({param: value})
    payload = json.dumps({'text': text}).encode('utf-8')

    req = request.Request(url, data=payload, method='POST')
    # Токен идёт заголовком без префикса Bearer — так требует MAX.
    # Передача токена в строке запроса когда-то работала, но её убрали
    req.add_header('Authorization', token)
    req.add_header('Content-Type', 'application/json')

    try:
        with request.urlopen(req, timeout=timeout) as response:
            body = response.read().decode('utf-8', errors='replace')
    except error.HTTPError as exc:
        detail = exc.read().decode('utf-8', errors='replace')[:300]
        raise MaxError(f'MAX ответил {exc.code}: {detail}') from exc
    except error.URLError as exc:
        raise MaxError(f'MAX недоступен: {exc.reason}') from exc
    except TimeoutError as exc:
        raise MaxError('MAX не ответил вовремя') from exc

    # Ответ 200 ещё не значит «доставлено»: ошибку кладут в тело
    try:
        data = json.loads(body) if body else {}
    except ValueError:
        raise MaxError(f'MAX вернул не JSON: {body[:200]}')

    if isinstance(data, dict) and data.get('code'):
        raise MaxError(f'MAX: {data.get("code")} — {data.get("message", "")}'.strip(' —'))

    return data


def get_max_updates(limit=100, timeout=15):
    """Свежие события бота — чтобы узнать идентификаторы написавших ему людей.

    Используется только командой max_updates при настройке; в обычной работе
    бот ничего не читает.
    """
    token = getattr(settings, 'MAX_BOT_TOKEN', '')
    if not token:
        raise MaxError('Не задан MAX_BOT_TOKEN')

    url = f'{api_url()}/updates?' + parse.urlencode({'limit': limit})
    req = request.Request(url, method='GET')
    req.add_header('Authorization', token)

    try:
        with request.urlopen(req, timeout=timeout) as response:
            body = response.read().decode('utf-8', errors='replace')
    except error.HTTPError as exc:
        detail = exc.read().decode('utf-8', errors='replace')[:300]
        raise MaxError(f'MAX ответил {exc.code}: {detail}') from exc
    except error.URLError as exc:
        raise MaxError(f'MAX недоступен: {exc.reason}') from exc

    try:
        return json.loads(body) if body else {}
    except ValueError:
        raise MaxError(f'MAX вернул не JSON: {body[:200]}')
