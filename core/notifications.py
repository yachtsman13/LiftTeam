"""
Постановка оповещений в очередь.

Здесь только решение «нужно ли оповещать и о чём», без единой попытки
что-то отправить: отправкой занимается команда `send_notifications`.
Разделение не формальность — сохранение заказа не должно ждать SMTP.
"""
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from . import messengers
from .models import Employee, Notification, format_amount


def _setting(name, default):
    return getattr(settings, name, default)


# Кому какие оповещения. Роли разные: детали заказывает склад, за деньгами
# следит бухгалтерия, администратор нужен и там и там
STOCK_ROLES = ('warehouse', 'admin')
DEBT_ROLES = ('accountant', 'admin')


def _staff(roles=STOCK_ROLES):
    return Employee.objects.filter(is_active=True, role__in=roles)


def staff_recipients(roles=STOCK_ROLES):
    """Почтовые адреса сотрудников.

    У кого не заполнена почта, тот и не получит, — это нормально и не ошибка.
    У кого выключен личный выбор «оповещения на почту» — тоже, и это тоже
    не ошибка, а его собственная настройка.
    """
    return list(
        _staff(roles).filter(notify_by_email=True)
        .exclude(email='').values_list('email', flat=True)
    )


def _messenger_recipients(enabled, configured, group_chat, id_field, notify_field,
                          personal=str, group=str, roles=STOCK_ROLES):
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
        for value in _staff(roles).filter(**{notify_field: True})
        .exclude(**{id_field: ''}).values_list(id_field, flat=True)
    ]


def staff_max_recipients(roles=STOCK_ROLES):
    return _messenger_recipients(
        _setting('NOTIFY_MAX', False),
        messengers.max_is_configured(),
        _setting('MAX_GROUP_CHAT_ID', ''),
        'max_user_id', 'notify_by_max',
        personal=lambda value: messengers.format_recipient('user', value),
        group=lambda value: messengers.format_recipient('chat', value),
        roles=roles,
    )


def staff_telegram_recipients(roles=STOCK_ROLES):
    # Приставок «user:»/«chat:» здесь нет: в Telegram и человек, и группа —
    # это chat_id, и различать их в получателе незачем
    return _messenger_recipients(
        _setting('NOTIFY_TELEGRAM', False),
        messengers.telegram_is_configured(),
        _setting('TELEGRAM_GROUP_CHAT_ID', ''),
        'telegram_chat_id', 'notify_by_telegram',
        roles=roles,
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


def queue_for_staff(event, subject, body, roles=STOCK_ROLES, **extra):
    """Одно и то же сообщение сотрудникам — почтой и в мессенджеры.

    В мессенджер тема уходит первой строкой текста: отдельного поля темы
    там нет, а без неё сообщение начинается с середины мысли.
    """
    queued = [
        queue(event, email, subject, body, **extra)
        for email in staff_recipients(roles)
    ]
    for channel, recipients in (
        (Notification.CHANNEL_MAX, staff_max_recipients(roles)),
        (Notification.CHANNEL_TELEGRAM, staff_telegram_recipients(roles)),
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
    готов к отгрузке, отгружен и «ремонт невозможен» — последнее тоже
    финал, и заказчику разумно узнать о нём тем же письмом. «Диагностика»
    и «ремонт» — внутренняя кухня, письмо о них выглядит как спам.
    """
    if not _setting('NOTIFY_CLIENTS', False):
        return None
    if order.status not in ('accepted', 'ready_for_shipment', 'shipped', 'unrepairable'):
        return None

    email = (order.client.email or '').strip()
    if not email:
        return None

    equipment = ', '.join(str(roe.equipment) for roe in order.order_equipments.all())
    if order.status == 'unrepairable':
        lines = [
            f'Здравствуйте, {order.client.name}.',
            '',
            f'По заказу {order.order_number} ремонт признан невозможным.',
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

    return queue(
        'order_status', email,
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
        return None

    email = (order.client.email or '').strip()
    if not email:
        return None

    if order.debt <= 0:
        return None

    cooldown = _setting('DEBT_REMINDER_COOLDOWN_DAYS', 7)
    recent = timezone.now() - timedelta(days=cooldown)
    if Notification.objects.filter(
        event='debt_reminder', repair_order=order, created_at__gte=recent
    ).exists():
        return None

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

    return queue(
        'debt_reminder', email,
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
    return queue_for_staff('debt_digest', subject, '\n'.join(lines), roles=DEBT_ROLES)
