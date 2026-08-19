"""Представления для уведомлений банков — единственное, что смотрит наружу.

Отдельный модуль, а не часть `core/views.py`, потому что снаружи должен
быть виден ровно один каталог адресов — `/webhooks/`, — и никакой другой.
Обратный прокси открывает в интернет этот каталог и отвечает 404 на всё
остальное; если бы вьюха приёма стояла среди обычных, разделить их
на прокси было бы нечем, а любая соседняя страница, случайно попавшая
под тот же префикс, оказалась бы открыта миру.

Здесь нет ни одного декоратора доступа из `core/decorators.py` — и это
не забывчивость. Запрос присылает сервер банка, у него нет ни сессии,
ни куки, ни CSRF-токена. Единственная возможная проверка — подпись,
которой банк заверяет тело. Она не написана (см. `core/webhooks.py`),
поэтому каждый запрос сейчас получает отказ.

Порядок проверок задан от дешёвых к дорогим и от «ничего не читаем»
к «читаем тело»: не тот метод → выключено → слишком длинное тело →
адрес не из списка → подпись. Тело запроса читается только после того,
как выяснилось, что приём вообще включён: на Raspberry Pi память
считанная, и мегабайт мусора от сканера незачем даже поднимать.
"""
import logging

from django.conf import settings
from django.core.exceptions import RequestDataTooBig
from django.db import IntegrityError, transaction
from django.http import HttpResponse, HttpResponseNotAllowed
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from . import net, webhooks
from .models import WebhookDelivery

logger = logging.getLogger('core.webhooks')


def _setting(name, default):
    return getattr(settings, name, default)


def _remote_addr(request):
    """Адрес, с которого пришёл запрос.

    Только `REMOTE_ADDR`. Заголовки `X-Forwarded-For` и `X-Real-IP` здесь
    намеренно не читаются: их ставит кто угодно, и допускать по ним —
    значит не допускать ни по чему. За прокси это будет адрес самого
    прокси, поэтому список разрешённых адресов и заведён пустым:
    отсекать по адресу правильнее на прокси, до приложения.
    """
    return request.META.get('REMOTE_ADDR', '')


def _log_reject(provider, request, reason, size=None):
    """Пишет в журнал отказ. Секретов в записи нет.

    Через `net.redact` пропускается всё сообщение: если секрет когда-нибудь
    попадёт в текст причины, он не уедет в журнал и в резервную копию.
    Тело запроса не пишется никогда — в нём чужие данные, а разбирать
    отказы можно и по причине с размером.
    """
    message = (
        f'Отказ в приёме уведомления: банк {provider}, '
        f'адрес {_remote_addr(request) or "неизвестен"}, '
        f'причина: {reason}'
    )
    if size is not None:
        message += f', размер тела {size} байт'
    secrets = [
        _setting('WEBHOOKS_TBANK_SECRET', ''),
        _setting('WEBHOOKS_TOCHKA_SECRET', ''),
        _setting('TBANK_TOKEN', ''),
        _setting('TOCHKA_TOKEN', ''),
    ]
    logger.warning(net.redact(message, *secrets))


def _text(status, message):
    """Ответ банку. Короткий и без подробностей.

    Подробности здесь — подсказка тому, кто подбирает: по разнице
    ответов видно, на каком шаге проверки он остановился. Причина
    отказа целиком пишется в журнал, а наружу уходит код.
    """
    return HttpResponse(message, status=status, content_type='text/plain; charset=utf-8')


@csrf_exempt
def receive(request, provider):
    """Приём уведомления от банка. Сейчас отказывает всегда.

    `provider` приходит из `core/webhook_urls.py` — по одному адресу
    на банк, поэтому неизвестный банк сюда не доходит: он не совпадает
    ни с одним маршрутом и получает 404.
    """
    # CSRF-токена у банка нет и быть не может, но метод проверить надо:
    # GET по этому адресу — это сканер, а не банк
    if request.method != 'POST':
        _log_reject(provider, request, f'метод {request.method}, а нужен POST')
        return HttpResponseNotAllowed(['POST'])

    verifier = webhooks.get_verifier(provider)

    # 1. Выключено — 503. Ничего не читаем, ничего не пишем.
    #    503, а не 404: банк по нему поймёт, что адрес верный, но приём
    #    временно недоступен, и повторит доставку позже, когда приём
    #    включат. 404 он счёл бы ошибкой настройки.
    if not _setting(verifier.enable_setting, False):
        _log_reject(provider, request, 'приём уведомлений выключен настройками')
        return _text(503, 'webhook disabled')

    # 2. Слишком длинное тело — 413, до всякого разбора. Сначала по
    #    заголовку, чтобы не поднимать мегабайты в память Raspberry Pi
    limit = int(_setting('WEBHOOKS_MAX_BODY_BYTES', 1048576))
    declared = request.META.get('CONTENT_LENGTH') or 0
    try:
        declared = int(declared)
    except (TypeError, ValueError):
        declared = 0
    if declared > limit:
        _log_reject(provider, request, 'тело больше допустимого', size=declared)
        return _text(413, 'body too large')

    try:
        body = request.body
    except RequestDataTooBig:
        # Django сам обрывает чтение по DATA_UPLOAD_MAX_MEMORY_SIZE,
        # если тот меньше нашего предела или заголовок соврал
        _log_reject(provider, request, 'тело больше допустимого')
        return _text(413, 'body too large')

    if len(body) > limit:
        _log_reject(provider, request, 'тело больше допустимого', size=len(body))
        return _text(413, 'body too large')

    # 3. Список разрешённых адресов, если он задан. Пустой означает
    #    «здесь не фильтруем» — этим занимается прокси
    allowed = [str(a).strip() for a in _setting('WEBHOOKS_ALLOWED_IPS', ()) if str(a).strip()]
    if allowed and _remote_addr(request) not in allowed:
        _log_reject(provider, request, 'адрес не в списке разрешённых', size=len(body))
        return _text(403, 'forbidden')

    # 4. Подпись. Сейчас отказывает у обоих банков — схема неизвестна.
    #    В проверку уходят точные байты запроса: разбор JSON и обратная
    #    сборка меняют байты, и подпись перестала бы сходиться
    try:
        verifier.verify(request, body)
    except webhooks.WebhookError as exc:
        _log_reject(provider, request, str(exc), size=len(body))
        return _text(403, 'signature verification unavailable')

    # --- Ниже сегодня не выполняется ни при каких условиях -------------
    # Проверка подписи выше отказывает всегда, пока не написана. Код
    # оставлен готовым, чтобы дописывание проверки было правкой одного
    # метода, а не новой задачей.

    digest = webhooks.body_hash(body)
    payload = webhooks.parse_payload(body)
    key = webhooks.dedup_key(verifier, payload, digest)

    try:
        with transaction.atomic():
            delivery, created = WebhookDelivery.objects.get_or_create(
                provider=provider, dedup_key=key,
                defaults={
                    'event_id': verifier.event_id(payload),
                    'body_hash': digest,
                    'body': body.decode('utf-8', errors='replace'),
                },
            )
    except IntegrityError:
        # Две доставки одного события пришли одновременно — вторая
        # проиграла гонку на ограничении уникальности. Это и есть защита
        # от повтора: банку отвечаем «принято», разбирать нечего
        return _text(200, 'duplicate')

    if not created:
        return _text(200, 'duplicate')

    try:
        webhooks.apply_payment(delivery, payload)
    except NotImplementedError as exc:
        delivery.status = WebhookDelivery.STATUS_FAILED
        delivery.result = str(exc)
        delivery.processed_at = timezone.now()
        delivery.save(update_fields=['status', 'result', 'processed_at'])
        _log_reject(provider, request, str(exc), size=len(body))
        return _text(501, 'not implemented')

    delivery.status = WebhookDelivery.STATUS_PROCESSED
    delivery.processed_at = timezone.now()
    delivery.save(update_fields=['status', 'processed_at'])
    return _text(200, 'ok')
