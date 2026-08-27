"""
Проверка связи со сторонними службами: банки, Диск, боты, почта.

Зачем отдельным модулем. Токен, введённый вслепую, отличается от неверного
токена ровно ничем — до того дня, когда не выставится счёт или не заведётся
папка. Проверять надо сразу после ввода, и проверять одинаково: команда
`manage.py setsecret` у Raspberry Pi и страница настроек в браузере обязаны
говорить об одной и той же службе одно и то же.

Каждая проверка — **читающий** запрос: узнать счета, спросить сведения
о Диске, забрать пустой список обновлений бота. Ничего не создаётся
и не отправляется: проверка связи не должна оставлять следов у заказчика
или на Диске.

Чего здесь нет: живой проверки Точки. Её документация из среды разработки
недоступна, читающий адрес не подтверждён, а угаданный отвечал бы «связи
нет» на исправном токене — это хуже, чем честное «проверка не написана».
Поэтому у Точки проверяется только полнота настроек, и об остальном
сказано вслух. Тот же порядок, что у её проверяющего уведомления.
"""
from django.core.mail import get_connection

from . import envfile, messengers, tbank, tochka, yadisk


class CheckResult:
    """Исход проверки: получилось, не получилось или проверять нечем."""

    OK = 'ok'
    FAIL = 'fail'
    SKIPPED = 'skipped'

    def __init__(self, state, message):
        self.state = state
        self.message = message

    @property
    def ok(self):
        return self.state == self.OK

    def __repr__(self):
        return 'CheckResult(%r, %r)' % (self.state, self.message)


def _explain(error):
    """Ошибку — словами, годными владельцу.

    UnicodeEncodeError отдельно: он означает, что в токене есть знаки
    не из латиницы. Так выглядит вставленный из письма токен, в который
    затесалась кириллическая буква — «с» вместо «c» видно только так.
    Сообщение самого исключения («'latin-1' codec can't encode») об этом
    не говорит ничего.
    """
    if isinstance(error, UnicodeEncodeError):
        return ('в значении есть знаки не из латиницы. Так бывает, когда '
                'при вставке в токен попадает кириллическая буква — '
                'наберите значение заново')
    return str(error)


def _ok(message):
    return CheckResult(CheckResult.OK, message)


def _fail(message):
    return CheckResult(CheckResult.FAIL, message)


def _skipped(message):
    return CheckResult(CheckResult.SKIPPED, message)


def _check_tbank():
    if not tbank.is_configured():
        return _skipped('Не задан TBANK_TOKEN — проверять нечего.')
    try:
        answer = tbank.get_accounts()
    except Exception as error:
        return _fail('Т-Банк не ответил: %s' % _explain(error))
    count = len(answer) if isinstance(answer, list) else 0
    return _ok('Т-Банк принял токен, счетов доступно: %d' % count)


def _check_tochka():
    missing = tochka.missing_settings()
    if missing:
        return _skipped('Точка не настроена: не задано %s.' % ', '.join(missing))
    return _skipped(
        'Настройки Точки заполнены, но живой проверки для неё не написано: '
        'её документация из среды разработки недоступна, а угаданный адрес '
        'отвечал бы «связи нет» на исправном токене. Первый настоящий счёт '
        'выставьте под присмотром.'
    )


def _check_yadisk():
    if not yadisk.is_configured():
        return _skipped(yadisk.unconfigured_reason())
    try:
        return _ok(yadisk.check_access())
    except Exception as error:
        return _fail('Яндекс.Диск не ответил: %s' % _explain(error))


def _check_max():
    if not messengers.max_is_configured():
        return _skipped('Не задан MAX_BOT_TOKEN — проверять нечего.')
    try:
        messengers.get_max_updates(limit=1)
    except Exception as error:
        return _fail('MAX не ответил: %s' % _explain(error))
    return _ok('MAX принял токен бота.')


def _check_telegram():
    if not messengers.telegram_is_configured():
        return _skipped('Не задан TELEGRAM_BOT_TOKEN — проверять нечего.')
    try:
        messengers.get_telegram_updates(limit=1)
    except Exception as error:
        return _fail('Telegram не ответил: %s' % _explain(error))
    return _ok('Telegram принял токен бота.')


def _check_email():
    host = envfile.setting('EMAIL_HOST', '')
    if not host:
        return _skipped('Не задан EMAIL_HOST — проверять нечего.')
    connection = get_connection()
    try:
        connection.open()
    except Exception as error:
        return _fail('Почтовый сервер не ответил: %s' % _explain(error))
    finally:
        try:
            connection.close()
        except Exception:
            pass
    # Пароль читает почтовый слой Django, а он берёт его из настроек,
    # мимо envfile: до перезапуска службы проверяется прежний пароль
    return _ok(
        'Почтовый сервер принял вход. Новый пароль вступит в силу '
        'после перезапуска службы.'
    )


# Какая проверка к какой настройке относится. Секрет, которому проверки нет
# (секреты вебхуков — их подтверждает сам банк, когда пришлёт уведомление),
# в этом списке отсутствует, и это не пропуск.
CHECKS = {
    'TBANK_TOKEN': _check_tbank,
    'TOCHKA_TOKEN': _check_tochka,
    'YANDEX_DISK_TOKEN': _check_yadisk,
    'MAX_BOT_TOKEN': _check_max,
    'TELEGRAM_BOT_TOKEN': _check_telegram,
    'EMAIL_HOST_PASSWORD': _check_email,
}


def check(name):
    """Проверить службу, к которой относится настройка `name`."""
    runner = CHECKS.get(name)
    if runner is None:
        return _skipped(
            'Для %s проверки связи нет: подтвердить его может только сам '
            'отправитель уведомления.' % name
        )
    return runner()
