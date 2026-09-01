"""
Постановка оповещений в очередь.

Здесь только решение «нужно ли оповещать и о чём», без единой попытки
что-то отправить: отправкой занимается команда `send_notifications`.
Разделение не формальность — сохранение заказа не должно ждать SMTP.
"""
from datetime import timedelta

from django.conf import settings
from django.db.models import Q

from . import envfile
from django.utils import timezone

from . import messengers
from .models import Employee, Notification, format_amount, order_overdue_days


def _setting(name, default):
    # Через envfile, а не напрямую из настроек: правка .env на Pi
    # должна действовать сразу, без перезапуска службы —
    # приложение работает с NoNewPrivileges и перезапустить себя
    # не может. Порядок главенства разобран в шапке core/envfile.py
    return envfile.setting(name, default)


# Кому какие оповещения. С v2.99.0 это **право должности**, а не роль:
# «кому приходит про дефицит» решает тот же список галочек, что и всё
# остальное про должность, и заводится новая должность без правки кода.
# Названия прав — из явного списка `models.PERMISSIONS`.
STOCK_PERMISSION = 'notify_low_stock'
DEBT_PERMISSION = 'notify_debts'
ORDER_OVERDUE_PERMISSION = 'notify_overdue'


def _staff(permission=STOCK_PERMISSION):
    """Действующие сотрудники, которым дано это право.

    Должность с полным доступом получает всё — так же, как прежняя роль
    «Администратор» стояла во всех трёх списках получателей. `distinct`
    обязателен: соединение с правами размножило бы строку сотрудника.
    """
    return Employee.objects.filter(is_active=True).filter(
        Q(position__is_admin=True) | Q(position__permissions__code=permission)
    ).distinct()


def staff_recipients(permission=STOCK_PERMISSION):
    """Почтовые адреса сотрудников.

    У кого не заполнена почта, тот и не получит, — это нормально и не ошибка.
    У кого выключен личный выбор «оповещения на почту» — тоже, и это тоже
    не ошибка, а его собственная настройка.
    """
    return list(
        _staff(permission).filter(notify_by_email=True)
        .exclude(email='').values_list('email', flat=True)
    )


def _messenger_recipients(enabled, configured, group_chat, id_field, notify_field,
                          personal=str, group=str, permission=STOCK_PERMISSION):
    """Получатели складских оповещений в одном из мессенджеров.

    Если задан общий чат, пишем один раз в него: в маленькой конторе всем
    и так интересно, что кончилось, а три одинаковых сообщения подряд —
    это не оповещение, а шум. Личный выбор канала тут ни при чём — это
    не адресованное лично сообщение, а общая доска объявлений.
    Иначе — каждому, кто указал свой идентификатор и не выключил канал
    у себя в настройках.
    """
    if not enabled or not configured:
        return []

    chat = str(group_chat or '').strip()
    if chat:
        return [group(chat)]

    return [
        personal(value)
        for value in _staff(permission).filter(**{notify_field: True})
        .exclude(**{id_field: ''}).values_list(id_field, flat=True)
    ]


def staff_max_recipients(permission=STOCK_PERMISSION):
    return _messenger_recipients(
        _setting('NOTIFY_MAX', False),
        messengers.max_is_configured(),
        _setting('MAX_GROUP_CHAT_ID', ''),
        'max_user_id', 'notify_by_max',
        personal=lambda value: messengers.format_recipient('user', value),
        group=lambda value: messengers.format_recipient('chat', value),
        permission=permission,
    )


def staff_telegram_recipients(permission=STOCK_PERMISSION):
    # Приставок «user:»/«chat:» здесь нет: в Telegram и человек, и группа —
    # это chat_id, и различать их в получателе незачем
    return _messenger_recipients(
        _setting('NOTIFY_TELEGRAM', False),
        messengers.telegram_is_configured(),
        _setting('TELEGRAM_GROUP_CHAT_ID', ''),
        'telegram_chat_id', 'notify_by_telegram',
        permission=permission,
    )


def queue(event, recipient, subject, body, repair_order=None, part=None,
          channel=Notification.CHANNEL_EMAIL):
    """Кладёт оповещение в очередь. Без адреса ничего не создаёт."""
    if not recipient:
        return None
    return Notification.objects.create(
        event=event,
        channel=channel,
        recipient=recipient,
        subject=subject,
        body=body,
        repair_order=repair_order,
        part=part,
    )


def _queue_to_client(event, client, subject, body, **extra):
    """Заказчику — по всем контактам с включённым каналом (с v2.99.0).

    Контактов не завели — используем то единственное поле, что было
    раньше (`Client.email`): так заказчик, для которого никто не открывал
    страницу контактов, продолжает получать письма без единой новой
    настройки. Контакт есть, но канал у него выключен или пуст — этот
    контакт просто ничего не получит, и это не ошибка, а его настройка.

    В мессенджер тема уходит первой строкой текста — тем же приёмом,
    что `queue_for_staff`.
    """
    contacts = list(client.contacts.all())
    if not contacts:
        email = (client.email or '').strip()
        return [queue(event, email, subject, body, **extra)] if email else []

    queued = []
    for contact in contacts:
        if contact.notify_by_email and contact.email:
            queued.append(queue(event, contact.email, subject, body, **extra))
        if (contact.notify_by_max and contact.max_user_id
                and messengers.max_is_configured()):
            queued.append(queue(
                event, messengers.format_recipient('user', contact.max_user_id),
                subject, f'{subject}\n\n{body}',
                channel=Notification.CHANNEL_MAX, **extra
            ))
        if (contact.notify_by_telegram and contact.telegram_chat_id
                and messengers.telegram_is_configured()):
            queued.append(queue(
                event, contact.telegram_chat_id, subject, f'{subject}\n\n{body}',
                channel=Notification.CHANNEL_TELEGRAM, **extra
            ))
    return [item for item in queued if item is not None]


def queue_for_staff(event, subject, body, permission=STOCK_PERMISSION, **extra):
    """Одно и то же сообщение сотрудникам — почтой и в мессенджеры.

    В мессенджер тема уходит первой строкой текста: отдельного поля темы
    там нет, а без неё сообщение начинается с середины мысли.
    """
    queued = [
        queue(event, email, subject, body, **extra)
        for email in staff_recipients(permission)
    ]
    for channel, recipients in (
        (Notification.CHANNEL_MAX, staff_max_recipients(permission)),
        (Notification.CHANNEL_TELEGRAM, staff_telegram_recipients(permission)),
    ):
        queued += [
            queue(event, recipient, subject, f'{subject}\n\n{body}',
                  channel=channel, **extra)
            for recipient in recipients
        ]
    return [item for item in queued if item is not None]


def notify_order_status(order, changed_by=None):
    """Заказчику — о смене статуса его заказа.

    Только для тех статусов, которые заказчику что-то говорят: принят,
    готов к отгрузке, отгружен, «ремонт невозможен» и «частично
    отремонтирован» — все четыре финалы или начало, и заказчику разумно
    узнать о них. «Диагностика» и «ремонт» — внутренняя кухня, письмо
    о них выглядит как спам.

    С v2.99.0 уходит не одним письмом на `Client.email`, а по всем
    контактам заказчика и их каналам — см. `_queue_to_client`.
    """
    if not _setting('NOTIFY_CLIENTS', False):
        return []
    if order.status not in (
        'accepted', 'ready_for_shipment', 'shipped',
        'unrepairable', 'partially_repaired',
    ):
        return []

    equipment = ', '.join(str(roe.equipment) for roe in order.order_equipments.all())
    if order.status == 'unrepairable':
        lines = [
            f'Здравствуйте, {order.client.name}.',
            '',
            f'По заказу {order.order_number} ремонт признан невозможным.',
        ]
    elif order.status == 'partially_repaired':
        lines = [
            f'Здравствуйте, {order.client.name}.',
            '',
            f'По заказу {order.order_number} часть оборудования '
            f'отремонтирована, часть — нет.',
        ]
    else:
        lines = [
            f'Здравствуйте, {order.client.name}.',
            '',
            f'Заказ {order.order_number}: {order.get_status_display().lower()}.',
        ]
    if equipment:
        lines.append(f'Оборудование: {equipment}.')
    if order.status == 'shipped' and order.tracking_number:
        lines.append(f'Трек-номер: {order.tracking_number}.')
    lines += ['', 'Это письмо отправлено программой учёта LiftTeam.']

    return _queue_to_client(
        'order_status', order.client,
        f'Заказ {order.order_number} — {order.get_status_display().lower()}',
        '\n'.join(lines),
        repair_order=order,
    )


def notify_low_stock(part):
    """Сотрудникам — о детали, ушедшей в дефицит.

    Повторно об одной и той же детали не пишем, пока не пройдёт пауза:
    при разборе заказа списывают несколько деталей подряд, и без этого
    в почту падало бы письмо на каждое списание.
    """
    if not _setting('NOTIFY_LOW_STOCK', True):
        return []
    if part.stock_state != part.STOCK_BELOW:
        return []

    cooldown = _setting('NOTIFY_LOW_STOCK_COOLDOWN_HOURS', 24)
    recent = timezone.now() - timedelta(hours=cooldown)
    if Notification.objects.filter(
        event='low_stock', part=part, created_at__gte=recent
    ).exists():
        return []

    cell = part.current_cell
    subject = f'Дефицит: {part.part_number} ({part.current_stock} из {part.min_stock})'
    body = '\n'.join([
        f'Деталь {part.part_number} — {part.name}.',
        f'Остаток: {part.current_stock} при минимуме {part.min_stock}.',
        f'Ячейка: {cell.address if cell else "не назначена"}.',
        f'Поставщик: {part.preferred_supplier or "не указан"}.',
        f'Срок поставки: {part.lead_time_days} дн.' if part.lead_time_days else '',
        '',
        'Деталь попала в план закупок.',
    ])

    return queue_for_staff('low_stock', subject, body, part=part)


def money(value):
    """Сумма для письма: «15 000 ₽». Пробел перед знаком неразрывный."""
    return format_amount(value) + '\u00a0₽'


def plural(count, one, few, many):
    """Русское склонение по числу: 1 заказ, 2 заказа, 5 заказов.

    Сводку читает человек, и «по 1 заказам» в ней выглядит так, будто
    программу писали второпях.
    """
    if 11 <= count % 100 <= 14:
        return many
    last = count % 10
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many


def debt_line(order):
    invoice = (
        f'счёт {order.invoice_number} от {order.invoice_date:%d.%m.%Y}'
        if order.invoice_number else f'счёт от {order.invoice_date:%d.%m.%Y}'
    )
    # Если часть денег уже пришла, называем остаток и рядом — из чего он
    # вышел: иначе бухгалтер сверяет сумму с платёжкой и не сходится
    amount = money(order.debt)
    if order.paid_amount:
        amount += f' (из {money(order.total_repair_cost)}, внесено {money(order.paid_amount)})'
    return (
        f'{order.order_number} — {order.client.name}, {invoice}, '
        f'{amount}, просрочено {order.days_overdue} '
        f'{plural(order.days_overdue, "день", "дня", "дней")}.'
    )


def notify_debt(order):
    """Заказчику — напоминание об оплате.

    Выключено по умолчанию и вдобавок к общему выключателю писем заказчикам:
    это требование денег от лица фирмы, и начинаться само собой после
    обновления оно не должно ни при каких обстоятельствах.

    Повторно по одному заказу не пишем, пока не пройдёт пауза: иначе
    ежедневная проверка превратилась бы в ежедневное письмо.
    """
    if not _setting('NOTIFY_CLIENTS', False) or not _setting('NOTIFY_DEBTS', False):
        return []

    if order.debt <= 0:
        return []

    cooldown = _setting('DEBT_REMINDER_COOLDOWN_DAYS', 7)
    recent = timezone.now() - timedelta(days=cooldown)
    if Notification.objects.filter(
        event='debt_reminder', repair_order=order, created_at__gte=recent
    ).exists():
        return []

    invoice = f'по счёту {order.invoice_number}' if order.invoice_number else 'по счёту'
    lines = [
        f'Здравствуйте, {order.client.name}.',
        '',
        f'Напоминаем об оплате {invoice} от {order.invoice_date:%d.%m.%Y} '
        f'за ремонт по заказу {order.order_number}.',
    ]
    # Требуем остаток, а не полную стоимость: часть денег заказчик уже
    # перевёл, и просить их повторно — верный способ испортить отношения
    if order.paid_amount:
        lines += [
            f'Сумма по счёту: {money(order.total_repair_cost)}, '
            f'из них поступило {money(order.paid_amount)}.',
            f'Остаток к оплате: {money(order.debt)}.',
        ]
    else:
        lines.append(f'Сумма: {money(order.debt)}.')
    lines += [
        '',
        'Если оплата уже прошла, это письмо можно не учитывать — '
        'сведения о поступлении вносятся в программу вручную.',
        '',
        'Это письмо отправлено программой учёта LiftTeam.',
    ]

    return _queue_to_client(
        'debt_reminder', order.client,
        f'Оплата по заказу {order.order_number}',
        '\n'.join(lines),
        repair_order=order,
    )


def notify_debt_digest(orders, without_invoice=()):
    """Бухгалтерии — сводка по долгам.

    Одним письмом на всех должников, а не письмом на каждого: список нужен,
    чтобы обзвонить, и в таком виде он и нужен.

    Повторно не чаще, чем раз в паузу, — команда может запускаться ежедневно,
    а сводка каждый день никому не нужна.
    """
    if not _setting('NOTIFY_DEBT_DIGEST', True):
        return []

    orders = list(orders)
    without_invoice = list(without_invoice)
    if not orders and not without_invoice:
        return []

    cooldown = _setting('DEBT_DIGEST_COOLDOWN_DAYS', 7)
    recent = timezone.now() - timedelta(days=cooldown)
    if Notification.objects.filter(event='debt_digest', created_at__gte=recent).exists():
        return []

    total = sum(order.debt for order in orders)
    lines = [f'Задолженности на {timezone.localdate():%d.%m.%Y}.', '']
    lines += [debt_line(order) for order in orders]
    if orders:
        lines += [
            '',
            f'Итого: {money(total)} по {len(orders)} '
            f'{plural(len(orders), "заказу", "заказам", "заказам")}.',
        ]

    if without_invoice:
        # Это не долг заказчика, а свой недосмотр, и лечится он не звонком,
        # а выставленным счётом
        lines += ['', 'Отгружено, но счёт не выставлен:']
        lines += [
            f'{order.order_number} — {order.client.name}'
            for order in without_invoice
        ]

    subject = (
        f'Задолженности: {len(orders)} '
        f'{plural(len(orders), "заказ", "заказа", "заказов")} на {money(total)}'
        if orders else 'Задолженности: отгрузки без счёта'
    )
    return queue_for_staff('debt_digest', subject, '\n'.join(lines), permission=DEBT_PERMISSION)


def order_last_touched(order):
    """Когда в последний раз что-то происходило с заказом.

    Обычно это `changed_at` самой свежей записи в истории статусов —
    неважно, была ли это смена самого статуса или, скажем, списание детали:
    и то и другое значит, что заказ не забыт. Если истории ещё нет (заказ
    только создан и статус ни разу не менялся), отсчёт начинается от даты
    приёма — до первой правки заказ и так «висит» в статусе «Принят»
    с этого момента.
    """
    latest = order.status_history.order_by('-changed_at').first()
    return latest.changed_at if latest else order.date_received


def order_stuck_days(order):
    """Сколько дней заказ провёл без движения в текущем статусе, и порог
    для этого статуса.

    None, если у статуса порога нет (`shipped`, `unrepairable` — заказ
    завершён, а не завис) или отсчитывать не от чего.
    """
    threshold = order_overdue_days(order.status)
    if threshold is None:
        return None
    last_touched = order_last_touched(order)
    if last_touched is None:
        return None
    return (timezone.now() - last_touched).days, threshold


def notify_order_overdue(order):
    """Персоналу — заказ завис в одном статусе дольше порога.

    Идемпотентность построена на времени, а не на отдельном поле «уже
    оповещали»: ищем оповещение по этому же заказу, созданное позже того
    момента, как заказ в последний раз трогали (`order_last_touched`) —
    то есть уже в текущем статусе. Если такое оповещение свежее порога
    эскалации, повторно не пишем; если оно старше — это и есть повторная
    эскалация. Смена статуса создаёт новую запись истории, отодвигает
    `order_last_touched` вперёд и тем самым сама открывает счёт заново —
    искать «тот же статус» отдельным полем не нужно.
    """
    if not _setting('NOTIFY_ORDER_OVERDUE', True):
        return None

    result = order_stuck_days(order)
    if result is None:
        return None
    days, threshold = result
    if days < threshold:
        return None

    escalation = _setting('ORDER_OVERDUE_ESCALATION_DAYS', 7)
    recent = timezone.now() - timedelta(days=escalation)
    last_touched = order_last_touched(order)
    cutoff = max(recent, last_touched)
    if Notification.objects.filter(
        event='order_overdue', repair_order=order, created_at__gte=cutoff
    ).exists():
        return None

    subject = (
        f'Заказ {order.order_number} завис в статусе '
        f'«{order.get_status_display()}»'
    )
    body = '\n'.join([
        f'Заказ {order.order_number} — {order.client.name}.',
        f'Статус: {order.get_status_display()}, без движения {days} '
        f'{plural(days, "день", "дня", "дней")} (порог — {threshold} '
        f'{plural(threshold, "день", "дня", "дней")}).',
        '',
        'Оповещение сформировано автоматической проверкой просроченных заказов.',
    ])
    return queue_for_staff(
        'order_overdue', subject, body, permission=ORDER_OVERDUE_PERMISSION, repair_order=order
    )
