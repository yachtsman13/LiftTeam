"""
Настройки для развёртывания на Raspberry Pi (или любом Linux-сервере) с SQLite.

Отличие от settings_production.py: там PostgreSQL + Redis в Docker, здесь
SQLite и один процесс Daphne — для нескольких сотрудников этого достаточно,
а на Pi экономит около 200 МБ ОЗУ и два обслуживаемых сервиса.

Запуск:
    DJANGO_SETTINGS_MODULE=lifteam.settings_pi daphne lifteam.asgi:application
"""
import os
from urllib.parse import urlsplit

from .settings import *  # noqa: F401,F403
from .settings import BASE_DIR

# --- Обязательные параметры -------------------------------------------------

SECRET_KEY = os.getenv('SECRET_KEY')
if not SECRET_KEY:
    raise ValueError(
        'SECRET_KEY обязателен. Сгенерировать: '
        'python -c "from django.core.management.utils import get_random_secret_key; '
        'print(get_random_secret_key())"'
    )

DEBUG = os.getenv('DEBUG', 'False').lower() == 'true'

# Сюда нужно вписать адреса, по которым открывают приложение: локальный IP
# Raspberry Pi в офисной сети и имя устройства в Tailscale.
ALLOWED_HOSTS = [
    h.strip() for h in os.getenv('ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',') if h.strip()
]

CSRF_TRUSTED_ORIGINS = [
    o.strip() for o in os.getenv('CSRF_TRUSTED_ORIGINS', '').split(',') if o.strip()
]
if not CSRF_TRUSTED_ORIGINS:
    CSRF_TRUSTED_ORIGINS = [
        f'{scheme}://{host}'
        for host in ALLOWED_HOSTS if host not in ('*', '')
        for scheme in ('http', 'https')
    ]

# --- Статика ----------------------------------------------------------------

STATIC_ROOT = BASE_DIR / 'staticfiles'
STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.ManifestStaticFilesStorage'},
}

# --- Безопасность -----------------------------------------------------------

# Включать вместе с TLS. При доступе через Tailscale трафик уже шифруется
# на транспортном уровне, и обычно эти параметры остаются выключенными —
# иначе SECURE_SSL_REDIRECT уводит в бесконечный редирект, а Secure-куки
# не дают войти в систему по HTTP.
SECURE_SSL_REDIRECT = os.getenv('SECURE_SSL_REDIRECT', 'False').lower() == 'true'
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
SECURE_HSTS_SECONDS = 31536000 if SECURE_SSL_REDIRECT else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = SECURE_SSL_REDIRECT
SECURE_HSTS_PRELOAD = SECURE_SSL_REDIRECT
SESSION_COOKIE_SECURE = SECURE_SSL_REDIRECT
CSRF_COOKIE_SECURE = SECURE_SSL_REDIRECT

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = 'DENY'

# --- Этикетки ---------------------------------------------------------------

# Основа ссылок в QR-кодах на этикетках.
#
# По умолчанию — адрес Pi в Tailscale. Он один и тот же в офисе, дома и с
# телефона в дороге, поэтому этикетка читается одинаково независимо от того,
# кто и откуда её печатал. Локальный IP так не умеет: напечатанная через
# Tailscale этикетка получала бы адрес, недоступный из офиса, и наоборот.
#
# Плата за это — сканирующее устройство обязано быть в Tailscale. Телефон
# без него получит «страница недоступна», даже стоя рядом с Pi.
#
# Порт не указан: снаружи отвечает nginx на 80 (deploy/nginx-lifteam.conf),
# а Daphne слушает 127.0.0.1:8000 за ним. Если nginx не ставили, здесь нужен
# LABEL_BASE_URL=http://100.108.92.92:8000
LABEL_BASE_URL = os.getenv('LABEL_BASE_URL', 'http://100.108.92.92')

# Адрес из ссылки обязан быть среди разрешённых, иначе отсканированный код
# приведёт на «400 Bad Request»: Django отвергает незнакомый заголовок Host.
# Дописываем сами, а не полагаемся на .env — забыть об этом слишком легко,
# а проявится оно у того, кто стоит со сканером у стеллажа.
_label_parts = urlsplit(LABEL_BASE_URL) if LABEL_BASE_URL else None
if _label_parts and _label_parts.hostname:
    if '*' not in ALLOWED_HOSTS and _label_parts.hostname not in ALLOWED_HOSTS:
        ALLOWED_HOSTS.append(_label_parts.hostname)
    # В доверенных источниках порт учитывается, в отличие от ALLOWED_HOSTS,
    # поэтому берём адрес целиком
    _label_origin = f'{_label_parts.scheme}://{_label_parts.netloc}'
    if _label_origin not in CSRF_TRUSTED_ORIGINS:
        CSRF_TRUSTED_ORIGINS.append(_label_origin)

# --- Каналы -----------------------------------------------------------------

# InMemoryChannelLayer работает только внутри одного процесса. Это допустимо
# при запуске одного Daphne (см. deploy/lifteam.service). Если процессов станет
# несколько, WebSocket-уведомления придут не всем — тогда понадобится Redis.
REDIS_URL = os.getenv('REDIS_URL', '')
if REDIS_URL:
    CHANNEL_LAYERS = {
        'default': {
            'BACKEND': 'channels_redis.core.RedisChannelLayer',
            'CONFIG': {'hosts': [REDIS_URL]},
        },
    }
else:
    CHANNEL_LAYERS = {
        'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'},
    }

# --- Логирование ------------------------------------------------------------

# Пишем в два места намеренно.
#
# Вывод в stdout подхватывает journald (journalctl -u lifteam -f), но по
# умолчанию journald на Raspberry Pi OS хранит записи в оперативной памяти
# и стирает их при перезагрузке. Однажды это уже стоило нам возможности
# разобраться в ошибке 500: перезагрузка починила приложение и одновременно
# уничтожила единственное свидетельство причины.
#
# Поэтому ошибки дублируются в файл с ограничением размера. Он переживает
# перезагрузку независимо от настроек системы, и его проще найти, чем
# вспоминать нужные ключи journalctl.
LOG_DIR = BASE_DIR / 'logs'
LOG_DIR.mkdir(exist_ok=True)

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'standard': {
            'format': '{levelname} {asctime} {name} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'standard',
        },
        'errorfile': {
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': str(LOG_DIR / 'errors.log'),
            'maxBytes': 5 * 1024 * 1024,
            'backupCount': 3,
            'encoding': 'utf-8',
            'level': 'WARNING',
            'formatter': 'standard',
        },
    },
    'root': {
        'handlers': ['console', 'errorfile'],
        'level': 'INFO',
    },
    'loggers': {
        'django.request': {
            'handlers': ['console', 'errorfile'],
            'level': 'ERROR',
            'propagate': False,
        },
    },
}
