"""
Django settings for lifteam project.
v2.101.0 — standalone (SQLite) / Docker (PostgreSQL + Redis + Nginx)
"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

# Файл с настройками — тот самый, который только что прочитал load_dotenv().
# Путь назван отдельной настройкой, потому что программа не только читает
# его при запуске, но и перечитывает на ходу, и правит: см. core/envfile.py.
# Держать его в базе нельзя — базу увозит ночная выгрузка в облако,
# а токены банков покидать Raspberry Pi не должны.
ENV_FILE_PATH = os.getenv('ENV_FILE_PATH', str(BASE_DIR / '.env'))

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

# Свой прогонщик тестов — ради быстрого хеширования паролей в них.
# Подробности и цифры — в core/test_runner.py. На хранение настоящих паролей
# это не влияет: PASSWORD_HASHERS здесь не переопределяется, Django берёт
# свои умолчания (PBKDF2), а подмена живёт только внутри `manage.py test`.
TEST_RUNNER = 'core.test_runner.FastPasswordTestRunner'

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

# --- Задолженности ------------------------------------------------------
# Через сколько дней после даты счёта долг считается просроченным.
# Отсчёт от счёта, а не от приёма заказа: пока счёт не выставлен,
# требовать оплату не за что.
DEBT_OVERDUE_DAYS = int(os.getenv('DEBT_OVERDUE_DAYS', '14'))

# Сводка по должникам бухгалтерии и администратору. Внутреннее письмо,
# поэтому включено сразу.
NOTIFY_DEBT_DIGEST = os.getenv('NOTIFY_DEBT_DIGEST', 'True').lower() == 'true'
DEBT_DIGEST_COOLDOWN_DAYS = int(os.getenv('DEBT_DIGEST_COOLDOWN_DAYS', '7'))

# Напоминания об оплате самим заказчикам. Выключено, и вдобавок требует
# включённого NOTIFY_CLIENTS: это требование денег от лица фирмы, и
# начинаться само собой после обновления оно не должно.
NOTIFY_DEBTS = os.getenv('NOTIFY_DEBTS', 'False').lower() == 'true'
DEBT_REMINDER_COOLDOWN_DAYS = int(os.getenv('DEBT_REMINDER_COOLDOWN_DAYS', '7'))

# --- Просроченные заказы (SLA) -------------------------------------------
# Через сколько дней без движения в одном статусе заказ считается «зависшим».
# Свой порог на каждый промежуточный статус: диагностика обычно короче
# ремонта, и общий порог на все статусы был бы либо слишком строгим для
# ремонта, либо слишком мягким для диагностики. У «Отгружен» и «Ремонт
# невозможен» порога нет — это завершённые состояния.
ORDER_OVERDUE_DAYS = {
    'accepted': int(os.getenv('ORDER_OVERDUE_DAYS_ACCEPTED', '2')),
    'diagnostic': int(os.getenv('ORDER_OVERDUE_DAYS_DIAGNOSTIC', '3')),
    'repair': int(os.getenv('ORDER_OVERDUE_DAYS_REPAIR', '7')),
    'ready_for_shipment': int(os.getenv('ORDER_OVERDUE_DAYS_READY_FOR_SHIPMENT', '5')),
}

# Внутреннее оповещение персоналу (менеджер по ремонту, администратор) —
# заказчик его никогда не видит, поэтому включено сразу.
NOTIFY_ORDER_OVERDUE = os.getenv('NOTIFY_ORDER_OVERDUE', 'True').lower() == 'true'

# Первое оповещение — сразу по пересечении порога; затем повторно не чаще,
# чем раз в столько дней, пока заказ не сдвинется со статуса (эскалация).
ORDER_OVERDUE_ESCALATION_DAYS = int(os.getenv('ORDER_OVERDUE_ESCALATION_DAYS', '7'))

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

# Сколько дней по умолчанию действует коммерческое предложение.
# Только заготовка для формы: в самом документе стоит дата, и правит её
# человек — у разных заказчиков сроки разные
QUOTE_VALID_DAYS = int(os.getenv('QUOTE_VALID_DAYS', '14'))

# --- Выписка Т-Банка ----------------------------------------------------
# Программа только читает выписку по расчётному счёту и показывает
# поступления рядом с долгами по заказам. Разносит деньги человек кнопкой.
#
# Токен выдаётся в личном кабинете Т-Бизнеса (Настройки → Токены API).
# Выдавайте его с правами ТОЛЬКО на чтение выписки: методов, двигающих
# деньги, в программе нет, и права на них ей не нужны.
# Пустой токен — раздел «Поступления» просто не показывается.
TBANK_TOKEN = os.getenv('TBANK_TOKEN', '')

# Номер расчётного счёта, по которому берётся выписка. Узнать его можно
# командой `python manage.py tbank_statement --accounts`.
TBANK_ACCOUNT = os.getenv('TBANK_ACCOUNT', '')

# Адрес API вынесен в настройки: банк уже переезжал с business.tinkoff.ru
# на business.tbank.ru, и следующий переезд должен чиниться правкой .env.
TBANK_API_URL = os.getenv('TBANK_API_URL', 'https://business.tbank.ru/openapi')

# Выставление счетов через API банка — отдельный выключатель, по умолчанию
# выключенный. Читать выписку и отправлять заказчику счёт от лица фирмы —
# разные по последствиям действия, и включаться они должны порознь.
# Токену вдобавок нужно право на выставление счетов, а не только на выписку.
TBANK_INVOICE_ENABLED = os.getenv('TBANK_INVOICE_ENABLED', 'False').lower() == 'true'

# С какого номера начинать, если программа ещё не видела ни одного счёта.
# Банк номер НЕ выдаёт: он приходит от нас и попадает в документ как есть,
# поэтому за сквозной нумерацией с теми счетами, что выставлены руками
# в личном кабинете, следит человек. Номер на странице открыт для правки.
TBANK_INVOICE_NUMBER_START = int(os.getenv('TBANK_INVOICE_NUMBER_START', '1'))

# Через сколько дней после выставления счёт считается просроченным к оплате.
# Уходит в счёт полем dueDate
TBANK_INVOICE_DUE_DAYS = int(os.getenv('TBANK_INVOICE_DUE_DAYS', '14'))

# Единица измерения и ставка НДС в позициях счёта. «None» — без НДС:
# на УСН это и нужно. Строками, потому что банк принимает их строками
TBANK_INVOICE_UNIT = os.getenv('TBANK_INVOICE_UNIT', 'шт.')
TBANK_INVOICE_VAT = os.getenv('TBANK_INVOICE_VAT', 'None')

# За сколько последних дней тянуть выписку при каждом запуске. С запасом:
# повторная загрузка уже известных операций ничего не портит, а пропущенный
# из-за суточного простоя Pi день стоил бы потерянного поступления.
TBANK_STATEMENT_DAYS = int(os.getenv('TBANK_STATEMENT_DAYS', '30'))

# Как часто тянуть выписку. Таймер systemd только тикает (раз в четверть
# часа в рабочее время), а решает программа: расписание в юните меняется
# от root, а эта настройка правится со страницы «Настройки».
# 0 — на каждый тик.
TBANK_STATEMENT_INTERVAL_MINUTES = int(
    os.getenv('TBANK_STATEMENT_INTERVAL_MINUTES', '60'))

# --- Точка Банк -----------------------------------------------------------
#
# Второй банк. Наборы секретов у банков разные и намеренно не смешаны
# в одну группу: токен Т-Банка в Точке не работает и наоборот, а общая
# группа настроек рано или поздно означала бы запрос в один банк
# с ключом от другого.
#
# Точка предлагает два способа авторизации: JWT-токен и OAuth 2.0.
# Нам нужен JWT: OAuth предназначен для сервисов, обслуживающих ЧУЖИХ
# клиентов банка, а мы выставляем счета за себя. Токен выпускается
# в личном кабинете Точки и в заголовке идёт как `Bearer <токен>`.
TOCHKA_TOKEN = os.getenv('TOCHKA_TOKEN', '')

# Код клиента и идентификатор счёта. Оба узнаются у самого банка
# методами «список клиентов» и «список счетов»; accountId — это номер
# счёта и БИК, а не просто номер счёта.
TOCHKA_CUSTOMER_CODE = os.getenv('TOCHKA_CUSTOMER_CODE', '')
TOCHKA_ACCOUNT_ID = os.getenv('TOCHKA_ACCOUNT_ID', '')

# Адрес и версия API — в настройках по той же причине, что и у Т-Банка:
# переезд банка должен чиниться правкой .env, а не кодом
TOCHKA_API_URL = os.getenv('TOCHKA_API_URL', 'https://enter.tochka.com/uapi')
TOCHKA_API_VERSION = os.getenv('TOCHKA_API_VERSION', 'v1.0')

# Свой выключатель выставления счетов, как и у Т-Банка: счёт уходит
# заказчику от лица фирмы, и включаться сам собой он не должен
TOCHKA_INVOICE_ENABLED = os.getenv('TOCHKA_INVOICE_ENABLED', 'False').lower() == 'true'

# ==================== ЯНДЕКС.ДИСК ====================
# Папка под снимки ремонта у каждой единицы в заказе. Программа заводит
# её по единому пути и записывает ссылку — раньше это делали руками
# в веб-интерфейсе, и снимки одного ремонта уезжали в папку другого.
#
# Токен выдаёт приложение OAuth Яндекса, заведённое владельцем
# (см. DEPLOY.md). Прав достаточно на чтение и запись в папке приложения
# или всего Диска; удалять программа не умеет вовсе.
YANDEX_DISK_TOKEN = os.getenv('YANDEX_DISK_TOKEN', '')

# Корневая папка программы. Отдельная, а не весь Диск: там же лежит
# личное владельца, и мы туда не ходим.
YANDEX_DISK_ROOT = os.getenv('YANDEX_DISK_ROOT', 'LiftTeam')

# Адрес API — в настройках по той же причине, что у банков: переезд
# должен чиниться правкой .env, а не кодом
YANDEX_DISK_API_URL = os.getenv(
    'YANDEX_DISK_API_URL', 'https://cloud-api.yandex.net/v1/disk'
)

# Ряд номеров счетов один на всю программу и при двух юрлицах — начало
# ряда задаётся прежней TBANK_INVOICE_NUMBER_START. Здесь только срок оплаты
TOCHKA_INVOICE_DUE_DAYS = int(os.getenv('TOCHKA_INVOICE_DUE_DAYS', '14'))

# Единица измерения и ставка НДС в позициях счёта. Названия ставок у Точки
# свои: without_nds, nds_0, nds_5, nds_7, nds_10, nds_20. «without_nds» —
# без НДС, на УСН это и нужно.
TOCHKA_INVOICE_UNIT = os.getenv('TOCHKA_INVOICE_UNIT', 'шт.')
TOCHKA_INVOICE_NDS = os.getenv('TOCHKA_INVOICE_NDS', 'without_nds')

# Суммы числом или строкой. По документации банка (OpenAPI) — числом,
# и так стоит по умолчанию. Но рабочий сторонний SDK шлёт их строками
# вида «54000.00», а проверить на живом счёте пока не на чем. Если банк
# откажет по сумме — поставьте True, это единственное, что тут меняется.
TOCHKA_INVOICE_AMOUNTS_AS_STRING = os.getenv(
    'TOCHKA_INVOICE_AMOUNTS_AS_STRING', 'False').lower() == 'true'

# --- Приём уведомлений от банков (вебхуки) -------------------------------
#
# Адрес, на который банк присылает уведомление об оплате, обязан быть
# виден из интернета. Всё остальное приложение живёт в Tailscale и снаружи
# недоступно, поэтому наружу открывается ровно один каталог — /webhooks/
# (порядок настройки — в DEPLOY.md, раздел «Приём уведомлений от банков»).
#
# Приём выключен по умолчанию у каждого банка отдельно. У Т-Банка проверка
# написана (адрес отправителя плюс заголовок авторизации, см. ниже), и его
# приём можно включить, заполнив WEBHOOKS_TBANK_SECRET. У Точки проверки
# нет: её документация об уведомлениях недоступна, и её проверяющий
# отказывает на каждом запросе — включать выключатель Точки бессмысленно.
WEBHOOKS_TBANK_ENABLED = os.getenv('WEBHOOKS_TBANK_ENABLED', 'False').lower() == 'true'
WEBHOOKS_TOCHKA_ENABLED = os.getenv('WEBHOOKS_TOCHKA_ENABLED', 'False').lower() == 'true'

# Подпись уведомления у Т-Банка отсутствует как таковая: банк ничем тело
# не заверяет. Вместо неё — заголовок авторизации, который мы придумываем
# сами и сообщаем банку при подключении вебхука; банк присылает его
# в каждом уведомлении. Здесь хранится значение заголовка Authorization
# целиком, вместе со словом Bearer или Basic: банк поддерживает и то
# и другое, и разбирать схему за него незачем.
#
# Пусто — приём отказывает. Один список адресов слишком слабая защита,
# чтобы остаться единственной: адрес подделывается на пути к нам, а сеть
# банка велика.
WEBHOOKS_TBANK_SECRET = os.getenv('WEBHOOKS_TBANK_SECRET', '')
# У Точки схема неизвестна; настройка заведена, но ничем не проверяется.
WEBHOOKS_TOCHKA_SECRET = os.getenv('WEBHOOKS_TOCHKA_SECRET', '')

# Адреса, с которых Т-Банк присылает уведомления. Значения по умолчанию —
# ровно те шесть, что перечислены в его документации (раздел «Вебхуки»).
# В настройке, а не в коде: банк может сменить их, не дожидаясь нашего
# выпуска. Те же шесть адресов перечислены в deploy/nginx-lifteam-hooks.conf,
# чтобы чужой запрос отсекался ещё на прокси; за тем, что списки не разошлись,
# следит отдельный тест.
#
# Сеть 91.194.226.0/23 из той же страницы сюда НЕ входит: она относится
# к товарному кредитованию покупателей, которым программа не пользуется,
# а 510 адресов вместо шести — это ослабление проверки без всякой пользы.
WEBHOOKS_TBANK_IPS_DOCUMENTED = (
    '212.233.80.7',
    '91.218.132.2',
    '91.194.226.234',
    '91.194.226.235',
    '91.194.226.250',
    '91.194.226.251',
)
# Пустая переменная в .env означает «взять список из документации»,
# а не «список пуст»: пустой список отказал бы в приёме всему подряд,
# и понять, почему банк перестал доходить, было бы не по чему.
WEBHOOKS_TBANK_IPS = [
    a.strip()
    for a in (os.getenv('WEBHOOKS_TBANK_IPS', '')
              or ','.join(WEBHOOKS_TBANK_IPS_DOCUMENTED)).split(',')
    if a.strip()
]

# Адреса собственных обратных прокси. Только запросу, пришедшему с одного
# из них, разрешено сообщать настоящий адрес отправителя в X-Forwarded-For —
# и то лишь последним звеном списка, которое дописал сам nginx. Всё, что
# стоит в этом заголовке левее, поставил кто угодно, включая отправителя.
WEBHOOKS_TRUSTED_PROXIES = [
    a.strip()
    for a in (os.getenv('WEBHOOKS_TRUSTED_PROXIES', '') or '127.0.0.1,::1').split(',')
    if a.strip()
]

# Общий список адресов поверх банковского — на случай, когда приём нужно
# сузить ещё сильнее (например, до одного адреса на время проверки).
# Пустой — не фильтруем: у каждого банка есть свой список.
WEBHOOKS_ALLOWED_IPS = [
    a.strip() for a in os.getenv('WEBHOOKS_ALLOWED_IPS', '').split(',') if a.strip()
]

# Больше этого тело запроса не читается вовсе: отказ идёт по заголовку
# длины, до чтения. Уведомление банка — это несколько килобайт, а память
# на Raspberry Pi считанная, и первый же сканер попробует прислать файл.
WEBHOOKS_MAX_BODY_BYTES = int(os.getenv('WEBHOOKS_MAX_BODY_BYTES', '1048576'))

# Основа ссылок в QR-кодах этикеток. Пусто — берётся адрес, по которому
# открыта страница печати. На Raspberry Pi значение по умолчанию другое:
# там прошит адрес в Tailscale, см. settings_pi.py.
LABEL_BASE_URL = os.getenv('LABEL_BASE_URL', '')

# Срок гарантии на ремонт, месяцев. Отсчитывается от даты завершения заказа.
# 0 отключает гарантию: отметки о ней просто не показываются.
WARRANTY_MONTHS = int(os.getenv('WARRANTY_MONTHS', '12'))

# Сколько миллиметров этикетки ячейки закрыто выступом на передней стенке
# кассетницы — тем, за который ящик выдвигают. Всё важное на этикетке
# и так стоит у нижнего края, а это число сдвигает вниз ещё и описание
# с номиналами, чтобы они не начинались в закрытой части. Ноль — ничего
# не закрыто. Этикетки деталей это не касается: на пакете выступа нет.
LABEL_CELL_HIDDEN_TOP_MM = int(os.getenv('LABEL_CELL_HIDDEN_TOP_MM', '0'))

# --- Присутствие сотрудников ---------------------------------------------
# Сколько секунд отметка активности считается свежей. Браузер шлёт сигнал
# втрое чаще (значение делится на три и уходит на страницу), поэтому 90
# секунд — это два подряд пропущенных сигнала: связь по Tailscale рвётся,
# когда телефон переходит между сетями, и мигать индикатором на каждой такой
# заминке нельзя. При этом закрытый ноутбук перестаёт числиться в сети
# примерно за полторы минуты — достаточно быстро, чтобы решить, идти
# к человеку или звонить.
PRESENCE_TIMEOUT_SECONDS = int(os.getenv('PRESENCE_TIMEOUT_SECONDS', '90'))

# Login
LOGIN_URL = '/login/'
LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/login/'




