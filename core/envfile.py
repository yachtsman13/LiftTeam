"""
Живые настройки: файл `.env` как хранилище, а не снимок при запуске.

Зачем это есть
--------------
Токены банков, Яндекс.Диска и ботов лежат в `/opt/lifteam/.env` — файле
с правами 600, принадлежащем пользователю приложения. До этого модуля их
значения попадали в программу один раз, при запуске: `load_dotenv()`
клал их в окружение, `settings.py` читал окружение, и дальше правка файла
не значила ничего, пока не перезапустят службу. А перезапустить её
приложение само не может — оно работает с `NoNewPrivileges`, — значит
за каждой правкой приходилось идти по SSH.

Отсюда напрашивалось хранить настройки в базе, и это было бы ошибкой:
ночная выгрузка увозит `db.sqlite3` в облако, а `.env` не увозит. Токены
банков покидать Raspberry Pi не должны, и держать их в том, что уезжает,
нельзя даже зашифрованными — шифрование пришлось бы чем-то отпирать,
и ключ оказался бы на том же устройстве.

Поэтому хранилищем остался файл, а программа научилась его перечитывать.

Чем главнее файл
----------------
Правило одно и звучит так: **значение из файла главнее только тогда,
когда файл с момента запуска изменился**. Сравнение — с окружением
(`os.environ`), куда `load_dotenv()` положил снимок файла при старте:

* имени в файле нет                → берём значение из настроек Django;
* в файле то же, что в окружении   → файл не менялся, берём из настроек;
* в файле другое                   → файл правили, берём из файла.

Так сделано не из любви к точности. Значения из настроек уже разобраны
по типам (`TBANK_INVOICE_ENABLED` — правда/ложь, `..._DUE_DAYS` — число),
и пока файл не трогали, разбирать строку заново незачем. Заодно
`override_settings` в тестах продолжает работать у всех, включая того,
у кого рядом лежит собственный `.env`: значение в файле совпадает
со снимком, и настройки берут верх.

Из этого следует правило, которое легко нарушить по невнимательности:
**после записи в файл окружение обновлять нельзя**. Обнови его — и
сравнение скажет «не менялось», а программа продолжит отдавать старое
значение из настроек.

И второе: `ENV_FILE_PATH` обязан указывать на тот самый файл, который
читает `load_dotenv()` при запуске. Разойдись они — сравнивать будет
не с чем, и всё, что записано в одном файле, начнёт считаться правкой.

Тип значения берётся у настройки
--------------------------------
Строка из файла приводится к типу того, что лежит в настройках Django:
там оно уже разобрано `settings.py`. Поэтому новую настройку не нужно
описывать здесь дважды — достаточно, что она есть в `settings.py`.

Чего здесь нет
--------------
* **Записи секретов из веб-интерфейса.** `set_value` принимает секретные
  имена только с `allow_secrets=True`, и передаёт его одна команда
  `manage.py setsecret`, которую запускают у самого Pi. Это защита
  от промаха, а не от злоумышленника: файл принадлежит пользователю
  приложения, и взломанное приложение запишет в него что угодно.
* **Чтения секрета наружу.** `describe_secret` отдаёт «задан, 40 знаков»
  и ничего больше. Показанный один раз токен оседает в истории браузера,
  в кэше и на любом незапертом экране.
* **Удаления строк из файла.** «Стереть» означает записать пустое
  значение: у всех наших настроек пусто и означает «не задано», а
  вычёркивание строки из файла оставляло бы после себя дыры в комментариях.
* **`SECRET_KEY`.** Его заводят один раз при установке и не меняют:
  смена разлогинивает всех и обесценивает подписанные ссылки. Такому
  месту в списке правимого делать нечего.

Разбирает файл сам `python-dotenv` — тот же, что читает его при запуске.
Своего разбора здесь нет намеренно: два разбора однажды разошлись бы
на кавычках или на решётке внутри значения, и настройка молча значила бы
разное до и после перезапуска.
"""
import os
from pathlib import Path

from django.conf import settings
from dotenv import dotenv_values, set_key


# Имена, которые считаются секретами: их не показывают, не пишут в журнал
# и не принимают из веб-интерфейса. `SECRET_KEY` сюда не входит — почему,
# сказано в шапке модуля.
SECRET_NAMES = (
    'TBANK_TOKEN',
    'TOCHKA_TOKEN',
    'YANDEX_DISK_TOKEN',
    'MAX_BOT_TOKEN',
    'TELEGRAM_BOT_TOKEN',
    'WEBHOOKS_TBANK_SECRET',
    'WEBHOOKS_TOCHKA_SECRET',
    'EMAIL_HOST_PASSWORD',
)

# Что это за секрет и подхватывается ли он без перезапуска. Не подхватывается
# то, что читает не наш код: пароль почты берёт почтовый слой Django, и он
# смотрит прямо в настройки, мимо этого модуля.
SECRET_TITLES = {
    'TBANK_TOKEN': 'Токен Т-Банка',
    'TOCHKA_TOKEN': 'Токен Точки',
    'YANDEX_DISK_TOKEN': 'Токен Яндекс.Диска',
    'MAX_BOT_TOKEN': 'Токен бота MAX',
    'TELEGRAM_BOT_TOKEN': 'Токен бота Telegram',
    'WEBHOOKS_TBANK_SECRET': 'Секрет уведомлений Т-Банка',
    'WEBHOOKS_TOCHKA_SECRET': 'Секрет уведомлений Точки',
    'EMAIL_HOST_PASSWORD': 'Пароль почтового ящика',
}

SECRETS_NEEDING_RESTART = frozenset({'EMAIL_HOST_PASSWORD'})


class EnvFileError(Exception):
    """Файл настроек не прочитать или не записать. Текст годится человеку
    у Pi: он же и будет его читать."""


def path():
    """Где лежит файл настроек."""
    return Path(getattr(settings, 'ENV_FILE_PATH', '') or '')


def exists():
    return bool(str(path())) and path().is_file()


def is_secret(name):
    return name in SECRET_NAMES


# Разобранный файл держим до его следующей правки: узнаём об этом
# по времени изменения и размеру. Иначе каждое обращение к токену читало бы
# файл с карты памяти — а обращений на одно выставление счёта несколько.
_cache = {'key': None, 'values': {}}


def values():
    """Всё, что записано в файле. Пусто, если файла нет."""
    target = path()
    if not str(target):
        return {}
    try:
        stamp = target.stat()
    except OSError:
        _cache['key'] = None
        _cache['values'] = {}
        return {}
    key = (str(target), stamp.st_mtime_ns, stamp.st_size)
    if _cache['key'] != key:
        try:
            _cache['values'] = {
                name: ('' if value is None else value)
                for name, value in dotenv_values(target).items()
            }
        except OSError as error:
            raise EnvFileError(
                'Файл настроек %s не прочитать: %s' % (target, error)
            ) from error
        _cache['key'] = key
    return _cache['values']


def forget():
    """Забыть разобранное. Нужно после записи и в тестах: время изменения
    файла на некоторых файловых системах не различает правки внутри одной
    и той же секунды."""
    _cache['key'] = None
    _cache['values'] = {}


def changed_since_start(name):
    """Правили ли эту настройку в файле после запуска программы.

    Сравнение — со снимком, который `load_dotenv()` положил в окружение
    при старте. Имени нет в файле — правкой это не считается: настройка
    как жила в `settings.py`, так и живёт.
    """
    raw = values().get(name)
    if raw is None:
        return False
    return os.environ.get(name) != raw


def setting(name, default=None):
    """Значение настройки с оглядкой на живой файл.

    Порядок разобран в шапке модуля: файл главнее только там, где его
    после запуска правили.
    """
    fallback = getattr(settings, name, default)
    if not changed_since_start(name):
        return fallback
    return _coerce(values()[name], fallback)


def _coerce(raw, sample):
    """Строку из файла — к типу того, что лежит в настройках.

    Образец берётся из настроек, потому что там значение уже разобрано
    `settings.py`: тип настройки описан один раз и там, а не продублирован
    здесь. Образца нет — отдаём строку как есть.
    """
    if isinstance(sample, bool):
        return raw.strip().lower() == 'true'
    if isinstance(sample, int):
        try:
            return int(raw.strip())
        except ValueError:
            return sample
    if isinstance(sample, dict):
        # Составную настройку одной строкой не задать: она собирается
        # в settings.py из нескольких переменных. Такие читаются
        # по своим именам, а сюда попасть не должны
        return sample
    if isinstance(sample, (list, tuple)):
        parts = [part.strip() for part in raw.split(',') if part.strip()]
        return type(sample)(parts)
    return raw


def describe_secret(name):
    """Состояние секрета — без самого секрета.

    Наружу уходит только «задан и какой длины»: длина помогает отличить
    полностью вставленный токен от обрезанного, а восстановить по ней
    нечего.
    """
    raw = values().get(name)
    if raw is None:
        # В файле имени нет — но оно может быть задано иначе, например
        # переменной окружения самой службы. Тогда врать «не задан» нельзя
        raw = str(getattr(settings, name, '') or '')
        source = 'settings' if raw else ''
    else:
        source = 'env'
    return {
        'name': name,
        'title': SECRET_TITLES.get(name, name),
        'filled': bool(raw),
        'length': len(raw),
        'source': source,
        'needs_restart': name in SECRETS_NEEDING_RESTART,
    }


def set_value(name, value, allow_secrets=False):
    """Записать настройку в файл.

    Запись атомарная и с сохранением комментариев — этим занимается сам
    `python-dotenv`: он пишет во временный файл рядом, повторяет права
    исходного и подменяет одним движением. Своей записи здесь нет по той
    же причине, по которой нет своего разбора.

    Перед первой за вызов правкой рядом остаётся `.env.bak` — на случай
    правки, после которой программа перестала работать: у человека,
    сидящего рядом с Pi, должно быть куда откатиться.
    """
    if not name or not name.replace('_', '').isalnum() or not name[0].isalpha():
        raise EnvFileError(
            'Недопустимое имя настройки: %r. Ожидается ИМЯ_ПЕРЕМЕННОЙ.' % (name,)
        )
    name = name.upper()
    if is_secret(name) and not allow_secrets:
        raise EnvFileError(
            '%s — секрет, и через веб-интерфейс он не правится. '
            'Задать его можно у самого Raspberry Pi: '
            'python manage.py setsecret %s' % (name, name)
        )
    target = path()
    if not str(target):
        raise EnvFileError('Не задан путь к файлу настроек (ENV_FILE_PATH).')

    value = '' if value is None else str(value)
    if '\n' in value or '\r' in value:
        raise EnvFileError('Значение настройки не может быть многострочным.')

    _backup(target)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            # Создаём сразу закрытым от посторонних: в файле окажутся токены,
            # и промежутка, в котором он читается всеми, быть не должно
            target.touch(mode=0o600)
        set_key(str(target), name, value)
    except OSError as error:
        raise EnvFileError(
            'Файл настроек %s не записать: %s' % (target, error)
        ) from error
    forget()


def _backup(target):
    """Копия перед правкой — с теми же правами, что у оригинала."""
    if not target.is_file():
        return
    backup = target.with_name(target.name + '.bak')
    try:
        data = target.read_bytes()
        mode = target.stat().st_mode & 0o777
        # Открываем сами с нужными правами: записать токены в файл,
        # который потом станет читаемым всем, нельзя даже на минуту
        handle = os.open(str(backup), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        try:
            os.write(handle, data)
        finally:
            os.close(handle)
        os.chmod(backup, mode)
    except OSError as error:
        raise EnvFileError(
            'Не сделать копию %s: %s' % (backup, error)
        ) from error
