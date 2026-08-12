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
from .models import Employee, Notification


def _setting(name, default):
    return getattr(settings, name, default)


def _staff():
    """Кладовщики и администраторы: заказывать детали — их дело."""
    return Employee.objects.filter(is_active=True, role__in=['warehouse', 'admin'])


def staff_recipients():
    """Почтовые адреса сотрудников для складских оповещений.

    У кого не заполнена почта, тот и не получит, — это нормально и не ошибка.
    """
    return list(_staff().exclude(email='').values_list('email', flat=True))


def staff_max_recipients():
    """Получатели складских оповещений в MAX.

    Если задан общий чат, пишем один раз в него: в маленькой конторе всем
    и так интересно, что кончилось, а три одинаковых сообщения подряд —
    это не оповещение, а шум. Иначе — каждому, кто указал свой идентификатор.
    """
    if not _setting('NOTIFY_MAX', False) or not messengers.is_configured():
        return []

    group_chat = str(_setting('MAX_GROUP_CHAT_ID', '') or '').strip()
    if group_chat:
        return [messengers.format_recipient('chat', group_chat)]

    return [
        messengers.format_recipient('user', max_user_id)
        for max_user_id in _staff().exclude(max_user_id='').values_list('max_user_id', flat=True)
    ]


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


def notify_order_status(order, changed_by=None):
    """Заказчику — о смене статуса его заказа.

    Только для тех статусов, которые заказчику что-то говорят: принят
    и готов к отгрузке. «Диагностика» и «ремонт» — внутренняя кухня,
    письмо о них выглядит как спам.
    """
    if not _setting('NOTIFY_CLIENTS', False):
        return None
    if order.status not in ('accepted', 'ready_for_shipment', 'shipped'):
        return None

    email = (order.client.email or '').strip()
    if not email:
        return None

    equipment = ', '.join(str(roe.equipment) for roe in order.order_equipments.all())
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

    queued = [
        queue('low_stock', email, subject, body, part=part)
        for email in staff_recipients()
    ]
    # В MAX уходит то же самое, но темой первой строкой: у сообщения в
    # мессенджере нет отдельного поля темы
    queued += [
        queue('low_stock', recipient, subject, f'{subject}\n\n{body}', part=part,
              channel=Notification.CHANNEL_MAX)
        for recipient in staff_max_recipients()
    ]
    return [item for item in queued if item is not None]
