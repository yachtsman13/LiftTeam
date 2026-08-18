"""
Отправка сообщений в мессенджеры: MAX и Telegram.

Только транспорт: что и кому писать, решает core/notifications.py, а когда
отправлять — команда send_notifications. Здесь один HTTP-запрос на сообщение
и разбор ответа.

Почему urllib, а не requests: на Raspberry Pi чем меньше зависимостей, тем
меньше поводов обновлению сломаться. Запросы простые — ради них тянуть
библиотеку не стоит.

Общее у обоих мессенджеров: бот не может написать человеку первым. Пока
сотрудник сам не напишет боту, его идентификатор неизвестен и слать ему
некуда. Поэтому у каждого канала есть команда, показывающая, кто написал.
"""
import json
from urllib import error, parse, request

from django.conf import settings

from .net import explain

MAX_DEFAULT_API_URL = 'https://platform-api2.max.ru'
TELEGRAM_DEFAULT_API_URL = 'https://api.telegram.org'

# Личный диалог и групповой чат в MAX адресуются разными параметрами запроса.
# В Telegram такого деления нет: там и человек, и группа — это chat_id
MAX_RECIPIENT_PARAMS = {
    'user': 'user_id',
    'chat': 'chat_id',
}


class MessengerError(Exception):
    """Отправить не удалось. Текст пригоден для показа в очереди оповещений."""


class MaxError(MessengerError):
    pass


class TelegramError(MessengerError):
    pass


def _call(url, *, headers=None, payload=None, timeout=15, error_class=MessengerError,
          name='Мессенджер'):
    """Запрос к API мессенджера. Возвращает разобранный ответ.

    Любая неудача — от недоступной сети до неразбираемого ответа —
    превращается в error_class с человекочитаемой причиной: вызывающий код
    запишет её в очередь и повторит попытку позже.
    """
    data = json.dumps(payload).encode('utf-8') if payload is not None else None
    req = request.Request(url, data=data, method='POST' if data else 'GET')
    for header, value in (headers or {}).items():
        req.add_header(header, value)
    if data is not None:
        req.add_header('Content-Type', 'application/json')

    try:
        with request.urlopen(req, timeout=timeout) as response:
            body = response.read().decode('utf-8', errors='replace')
    except error.HTTPError as exc:
        detail = exc.read().decode('utf-8', errors='replace')[:300]
        raise error_class(f'{name} ответил {exc.code}: {detail}') from exc
    except error.URLError as exc:
        raise error_class(f'{name} недоступен: {explain(exc.reason)}') from exc
    except TimeoutError as exc:
        raise error_class(f'{name} не ответил вовремя') from exc

    try:
        return json.loads(body) if body else {}
    except ValueError:
        raise error_class(f'{name} вернул не JSON: {body[:200]}')


# --- MAX -----------------------------------------------------------------
# Бот заводится в самом MAX у @MasterBot командой /create, тот выдаёт токен.

def max_is_configured():
    """Настроен ли MAX. Без токена канал просто не используется."""
    return bool(getattr(settings, 'MAX_BOT_TOKEN', ''))


def max_api_url():
    # Значение по умолчанию вынесено в настройки не от хорошей жизни: адрес
    # API уже менялся (старый platform-api.max.ru отключили летом 2026),
    # и когда он сменится снова, это должно чиниться правкой .env, а не кодом.
    return getattr(settings, 'MAX_API_URL', MAX_DEFAULT_API_URL).rstrip('/')


def format_recipient(kind, value):
    """«user», «842910» → «user:842910». Обратное разбирается через split(':')."""
    return f'{kind}:{value}'


def _parse_max_recipient(recipient):
    kind, _, value = str(recipient).partition(':')
    param = MAX_RECIPIENT_PARAMS.get(kind)
    if not param or not value:
        raise MaxError(
            f'Непонятный получатель MAX: {recipient!r}. '
            'Ожидается «user:<id>» или «chat:<id>»'
        )
    return param, value


def send_max_message(recipient, text, timeout=15):
    """Отправляет текст одному получателю MAX."""
    token = getattr(settings, 'MAX_BOT_TOKEN', '')
    if not token:
        raise MaxError('Не задан MAX_BOT_TOKEN')

    param, value = _parse_max_recipient(recipient)
    data = _call(
        f'{max_api_url()}/messages?' + parse.urlencode({param: value}),
        # Токен идёт заголовком без префикса Bearer — так требует MAX.
        # Передача токена в строке запроса когда-то работала, но её убрали
        headers={'Authorization': token},
        payload={'text': text},
        timeout=timeout, error_class=MaxError, name='MAX',
    )

    # Ответ 200 ещё не значит «доставлено»: ошибку кладут в тело
    if isinstance(data, dict) and data.get('code'):
        raise MaxError(f'MAX: {data.get("code")} — {data.get("message", "")}'.strip(' —'))
    return data


def get_max_updates(limit=100, timeout=15):
    """Свежие события бота MAX — чтобы узнать идентификаторы написавших людей."""
    token = getattr(settings, 'MAX_BOT_TOKEN', '')
    if not token:
        raise MaxError('Не задан MAX_BOT_TOKEN')

    return _call(
        f'{max_api_url()}/updates?' + parse.urlencode({'limit': limit}),
        headers={'Authorization': token},
        timeout=timeout, error_class=MaxError, name='MAX',
    )


# --- Telegram ------------------------------------------------------------
# Бот заводится у @BotFather командой /newbot, тот выдаёт токен.

def telegram_is_configured():
    return bool(getattr(settings, 'TELEGRAM_BOT_TOKEN', ''))


def telegram_api_url():
    return getattr(settings, 'TELEGRAM_API_URL', TELEGRAM_DEFAULT_API_URL).rstrip('/')


def _telegram_method(method):
    """Токен в Telegram — часть адреса, а не заголовок."""
    token = getattr(settings, 'TELEGRAM_BOT_TOKEN', '')
    if not token:
        raise TelegramError('Не задан TELEGRAM_BOT_TOKEN')
    return f'{telegram_api_url()}/bot{token}/{method}'


def _check_telegram(data):
    """Telegram отвечает {"ok": false, "description": "..."} и с кодом 200."""
    if isinstance(data, dict) and not data.get('ok', True):
        raise TelegramError(
            f'Telegram: {data.get("description") or data.get("error_code") or "отказ"}'
        )
    return data


def send_telegram_message(chat_id, text, timeout=15):
    """Отправляет текст в личный диалог или в группу.

    В Telegram и человек, и группа — это chat_id, поэтому получатель
    хранится числом без всяких приставок.
    """
    if not str(chat_id).strip():
        raise TelegramError('Пустой получатель Telegram')

    data = _call(
        _telegram_method('sendMessage'),
        payload={'chat_id': str(chat_id).strip(), 'text': text},
        timeout=timeout, error_class=TelegramError, name='Telegram',
    )
    return _check_telegram(data)


def get_telegram_updates(limit=100, timeout=15):
    """Свежие события бота Telegram — чтобы узнать chat_id написавших."""
    data = _call(
        _telegram_method('getUpdates') + '?' + parse.urlencode({'limit': limit}),
        timeout=timeout, error_class=TelegramError, name='Telegram',
    )
    return _check_telegram(data)
