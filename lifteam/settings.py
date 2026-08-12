"""
Django settings for lifteam project.
v2.25.0 — standalone (SQLite) / Docker (PostgreSQL + Redis + Nginx)
"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.getenv('SECRET_KEY', 'django-insecure-change-me-in-production')
DEBUG = os.getenv('DEBUG', 'True').lower() == 'true'
ALLOWED_HOSTS = [h.strip() for h in os.getenv('ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',') if h.strip()]

INSTALLED_APPS = [
    'daphne',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'rest_framework',
    'django_filters',
    'channels',
    'core.apps.CoreConfig',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'lifteam.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'core' / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'lifteam.wsgi.application'
ASGI_APPLICATION = 'lifteam.asgi.application'

# Database — SQLite по умолчанию, PostgreSQL через DATABASE_URL
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': str(BASE_DIR / 'db.sqlite3'),
        # Сколько ждать освобождения блокировки на уровне драйвера.
        # Остальные параметры (WAL и др.) — в core/signals.py::configure_sqlite
        'OPTIONS': {
            'timeout': 30,
        },
    }
}

database_url = os.getenv('DATABASE_URL', '')
if database_url and not database_url.startswith('sqlite'):
    import dj_database_url
    DATABASES['default'] = dj_database_url.config(
        default=database_url,
        conn_max_age=600
    )

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = os.getenv('LANGUAGE_CODE', 'ru-ru')
TIME_ZONE = os.getenv('TIME_ZONE', 'Europe/Moscow')
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
# core/static находит AppDirectoriesFinder, так как core — установленное
# приложение. Дублировать каталог в STATICFILES_DIRS не нужно: collectstatic
# тогда обнаруживает каждый файл дважды и предупреждает о конфликте.

MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Custom user model — авторизация по username
AUTH_USER_MODEL = 'core.Employee'
AUTHENTICATION_BACKENDS = [
    'django.contrib.auth.backends.ModelBackend',
]

# Email
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = os.getenv('EMAIL_HOST', 'smtp.yandex.ru')

_email_port = os.getenv('EMAIL_PORT', '465')
try:
    EMAIL_PORT = int(_email_port) if _email_port else 465
except (ValueError, TypeError):
    EMAIL_PORT = 465

EMAIL_USE_SSL = os.getenv('EMAIL_USE_SSL', 'True').lower() == 'true'
EMAIL_HOST_USER = os.getenv('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.getenv('EMAIL_HOST_PASSWORD', '')
DEFAULT_FROM_EMAIL = os.getenv('DEFAULT_FROM_EMAIL', 'LiftTeam <noreply@example.com>')

# Redis / Cache — опционально (fallback на LocMemCache)
REDIS_URL = os.getenv('REDIS_URL', '')

if REDIS_URL:
    CACHES = {
        'default': {
            'BACKEND': 'django_redis.cache.RedisCache',
            'LOCATION': REDIS_URL,
            'OPTIONS': {
                'CLIENT_CLASS': 'django_redis.client.DefaultClient',
            }
        }
    }
    SESSION_ENGINE = 'django.contrib.sessions.backends.cache'
    SESSION_CACHE_ALIAS = 'default'
else:
    # Fallback для standalone без Redis
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        }
    }
    SESSION_ENGINE = 'django.contrib.sessions.backends.db'

SESSION_COOKIE_AGE = 86400

# Channels — fallback на InMemory если Redis недоступен
if REDIS_URL:
    CHANNEL_LAYERS = {
        'default': {
            'BACKEND': 'channels_redis.core.RedisChannelLayer',
            'CONFIG': {
                'hosts': [REDIS_URL],
            },
        },
    }
else:
    CHANNEL_LAYERS = {
        'default': {
            'BACKEND': 'channels.layers.InMemoryChannelLayer',
        },
    }

# REST Framework
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework.authentication.SessionAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 25,
    'DEFAULT_FILTER_BACKENDS': [
        'django_filters.rest_framework.DjangoFilterBackend',
        'rest_framework.filters.SearchFilter',
        'rest_framework.filters.OrderingFilter',
    ],
}

# --- Оповещения ---------------------------------------------------------
# Очередь наполняется всегда; эти параметры управляют только отправкой.
#
# NOTIFICATIONS_ENABLED — главный выключатель. По умолчанию выключено:
# пока не настроена почта и не проверено, что и кому уходит, письма
# копятся в очереди и никуда не идут.
NOTIFICATIONS_ENABLED = os.getenv('NOTIFICATIONS_ENABLED', 'False').lower() == 'true'

# Письма заказчикам — отдельный выключатель, тоже выключенный. Это переписка
# от лица фирмы с внешними людьми: включать её должен человек осознанно,
# а не «оно само заработало после обновления».
NOTIFY_CLIENTS = os.getenv('NOTIFY_CLIENTS', 'False').lower() == 'true'

# Оповещения сотрудникам о дефиците деталей
NOTIFY_LOW_STOCK = os.getenv('NOTIFY_LOW_STOCK', 'True').lower() == 'true'

# Не повторять письмо об одной и той же детали чаще, чем раз в столько часов:
# при разборе заказа списывают по несколько деталей подряд
NOTIFY_LOW_STOCK_COOLDOWN_HOURS = int(os.getenv('NOTIFY_LOW_STOCK_COOLDOWN_HOURS', '24'))

# Сколько раз пробовать отправить, прежде чем признать неудачу
NOTIFICATIONS_MAX_ATTEMPTS = int(os.getenv('NOTIFICATIONS_MAX_ATTEMPTS', '5'))

# Оповещения старше этого срока не отправляются: иначе после включения
# отправки заказчику придёт месячная пачка новостей о давно закрытых заказах
NOTIFICATIONS_MAX_AGE_HOURS = int(os.getenv('NOTIFICATIONS_MAX_AGE_HOURS', '24'))

# --- Мессенджер MAX -----------------------------------------------------
# Второй канал складских оповещений. Почта на телефоне часто молчит до
# следующего открытия ящика, а сообщение в мессенджере видно сразу.
#
# Токен выдаёт @MasterBot в самом MAX по команде /create. Пустой токен
# означает «канал не настроен»: очередь пополняется только письмами.
MAX_BOT_TOKEN = os.getenv('MAX_BOT_TOKEN', '')

# Адрес API вынесен в настройки потому, что уже менялся: старый
# platform-api.max.ru отключили в июле 2026. Следующая смена должна
# чиниться правкой .env, а не обновлением программы.
MAX_API_URL = os.getenv('MAX_API_URL', 'https://platform-api2.max.ru')

# Слать ли сотрудникам оповещения в MAX. Выключено по умолчанию: пока
# идентификаторы не прописаны, слать всё равно некому.
NOTIFY_MAX = os.getenv('NOTIFY_MAX', 'False').lower() == 'true'

# Общий чат для складских оповещений. Если задан, сообщение уходит один раз
# в чат, а не каждому кладовщику отдельно.
MAX_GROUP_CHAT_ID = os.getenv('MAX_GROUP_CHAT_ID', '')

# --- Мессенджер Telegram -------------------------------------------------
# Третий канал складских оповещений, устроен так же, как MAX. Токен выдаёт
# @BotFather по команде /newbot. Пустой токен — канал не используется.
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')

# Адрес API. Меняется, если Telegram недоступен напрямую и запросы идут
# через зеркало или локальный Bot API server.
TELEGRAM_API_URL = os.getenv('TELEGRAM_API_URL', 'https://api.telegram.org')

NOTIFY_TELEGRAM = os.getenv('NOTIFY_TELEGRAM', 'False').lower() == 'true'

# Общая группа для складских оповещений. Если задана, сообщение уходит один
# раз в неё, а не каждому кладовщику отдельно.
TELEGRAM_GROUP_CHAT_ID = os.getenv('TELEGRAM_GROUP_CHAT_ID', '')

# Основа ссылок в QR-кодах этикеток. Пусто — берётся адрес, по которому
# открыта страница печати. На Raspberry Pi значение по умолчанию другое:
# там прошит адрес в Tailscale, см. settings_pi.py.
LABEL_BASE_URL = os.getenv('LABEL_BASE_URL', '')

# Срок гарантии на ремонт, месяцев. Отсчитывается от даты завершения заказа.
# 0 отключает гарантию: отметки о ней просто не показываются.
WARRANTY_MONTHS = int(os.getenv('WARRANTY_MONTHS', '12'))

# Login
LOGIN_URL = '/login/'
LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/login/'




