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
from django.utils import timezone

from . import diadoc, envfile, invoicing, messengers, tbank, tochka, webhooks, yadisk


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
    # Разбор ответа — тот же, что у команды `tbank_statement --accounts`:
    # банк отвечает то списком, то объектом со списком внутри, и второй
    # разбор сказал бы «счетов доступно: 0» на исправном токене
    numbers = tbank.account_numbers(answer)
    if numbers:
        return _ok('Т-Банк принял токен. Счета: %s' % ', '.join(numbers))
    if tbank.account_list(answer):
        return _ok('Т-Банк принял токен, счета получены.')
    return _ok(
        'Т-Банк принял токен, но ни одного счёта не назвал. Номер для '
        'выписки можно посмотреть командой '
        'manage.py tbank_statement --accounts.'
    )


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


def _check_diadoc_client_secret():
    """`client_secret` сам по себе не проверить: OIDC подтверждает его
    только вместе с обменом токена, а для первого обмена нужен живой
    человек (Device Flow) — не то, что можно сделать кнопкой «Проверить».
    Настоящая проверка — у `DIADOC_REFRESH_TOKEN` ниже: её успех и есть
    подтверждение, что оба секрета верны."""
    if not diadoc.client_id() or not diadoc.client_secret():
        return _skipped('Не заданы DIADOC_CLIENT_ID/DIADOC_CLIENT_SECRET.')
    return _skipped(
        'Заданы, но сам по себе секрет не проверить — только вместе '
        'с DIADOC_REFRESH_TOKEN (см. проверку ниже).'
    )


def _check_diadoc_refresh_token():
    if not diadoc.refresh_token():
        return _skipped('Вход не пройден: выполните manage.py diadoc_login.')
    if not diadoc.box_id():
        return _skipped('Не задан DIADOC_BOX_ID — узнаётся при входе.')
    try:
        answer = diadoc.get_document_types()
    except diadoc.DiadocError as error:
        return _fail('Диадок не ответил: %s' % _explain(error))
    types = answer.get('DocumentTypes') if isinstance(answer, dict) else None
    if types:
        return _ok('Диадок принял токен. Типов документов в ящике: %d.' % len(types))
    return _ok('Диадок принял токен, но не назвал ни одного типа документа.')


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


def _check_webhook(provider):
    """Готовность приёма уведомлений банка.

    Связи здесь проверять нечего и некуда: уведомление присылает банк,
    а секрет — это то, что мы ждём **от него**. Позвонить и спросить
    «правильный ли у меня секрет» нельзя ни у кого.

    Зато можно проверить всё остальное, и именно на этом здесь спотыкаются:
    неверная настройка не роняет ничего, банк просто получает отказ,
    а оплата не отмечается — молча, до первого спорного счёта. Поэтому
    проверяются включатель, непустой секрет, его вид, список адресов
    и то, приходило ли хоть одно уведомление на самом деле.
    """
    from .models import WebhookDelivery

    try:
        verifier = webhooks.get_verifier(provider)
    except webhooks.WebhookError:
        return _skipped('Приём уведомлений этого банка не написан.')

    if not verifier.verifiable:
        # Настраивать тут нечего: приём откажет при любых значениях.
        # Рапортовать «настроено верно» было бы прямой неправдой
        return _skipped(
            'Проверка подлинности уведомлений %s не написана, и приём '
            'откажет при любых настройках: неизвестно ни с каких адресов '
            'банк их присылает, ни чем заверяет тело. Нужен раздел её '
            'документации об уведомлениях — угаданная проверка означала бы, '
            'что счета отмечает оплаченными кто угодно.' % verifier.label
        )

    troubles = []
    if not envfile.setting(verifier.enable_setting, False):
        troubles.append(
            'приём выключен — включите «%s» на этой же странице' % verifier.label
        )
    secret = str(envfile.setting(verifier.secret_setting, '') or '').strip()
    if not secret:
        troubles.append(
            'секрет не задан, а без него приём отказывает: один список '
            'адресов слишком слаб, чтобы остаться единственной проверкой'
        )
    elif ' ' not in secret:
        # Банк присылает значение заголовка Authorization целиком, вместе
        # со схемой. Записанный без «Bearer » секрет не совпадёт ни с одним
        # уведомлением, а выглядеть будет заполненным — самая обидная
        # из здешних ошибок
        troubles.append(
            'секрет записан без схемы. В заголовке банк присылает его '
            'целиком — «Bearer ваша-строка», — и сравнивается он целиком'
        )
    addresses = [
        str(a).strip()
        for a in (envfile.setting('WEBHOOKS_TBANK_IPS', ()) or ()) if str(a).strip()
    ]
    if verifier.provider == invoicing.TBANK and not addresses:
        troubles.append('пуст список адресов Т-Банка (WEBHOOKS_TBANK_IPS)')

    delivered = WebhookDelivery.objects.filter(provider=verifier.provider)
    last = delivered.order_by('-received_at').first()

    if troubles:
        return _fail('Приём не готов: %s.' % '; '.join(troubles))
    if last is None:
        return _skipped(
            'Настроено верно, но проверить это может только сам банк: '
            'уведомлений от него пока не приходило ни одного. Секрет вы '
            'придумываете сами и сообщаете банку письмом — совпал он или '
            'нет, станет видно на первом оплаченном счёте.'
        )
    return _ok(
        'Настроено верно. Уведомлений принято: %d, последнее — %s (%s).'
        % (delivered.count(),
           timezone.localtime(last.received_at).strftime('%d.%m.%Y в %H:%M'),
           last.get_status_display())
    )


def _check_webhook_tbank():
    return _check_webhook(invoicing.TBANK)


def _check_webhook_tochka():
    return _check_webhook(invoicing.TOCHKA)


# Какая проверка к какой настройке относится.
CHECKS = {
    'TBANK_TOKEN': _check_tbank,
    'TOCHKA_TOKEN': _check_tochka,
    'YANDEX_DISK_TOKEN': _check_yadisk,
    'MAX_BOT_TOKEN': _check_max,
    'TELEGRAM_BOT_TOKEN': _check_telegram,
    'EMAIL_HOST_PASSWORD': _check_email,
    'WEBHOOKS_TBANK_SECRET': _check_webhook_tbank,
    'WEBHOOKS_TOCHKA_SECRET': _check_webhook_tochka,
    'DIADOC_CLIENT_SECRET': _check_diadoc_client_secret,
    'DIADOC_REFRESH_TOKEN': _check_diadoc_refresh_token,
}


def check(name):
    """Проверить службу, к которой относится настройка `name`."""
    runner = CHECKS.get(name)
    if runner is None:
        return _skipped('Для %s проверки не написано.' % name)
    return runner()
