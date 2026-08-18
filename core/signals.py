"""
Django signals для LiftTeam v2.49.0.
Сигналы используются только для:
- настройки параметров подключения к SQLite
- создания начальной записи истории статуса при создании заказа
- отправки WebSocket-уведомлений
"""
import re
from functools import lru_cache

from django.db.backends.signals import connection_created
from django.db.models.signals import post_delete, post_save, pre_delete
from django.dispatch import receiver
from django.utils import timezone
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from .models import BankOperation, Payment, RepairOrder, OrderStatusHistory, SparePart


@lru_cache(maxsize=512)
def _like_pattern(pattern, escape):
    """Переводит шаблон SQL LIKE в регулярное выражение без учёта регистра."""
    parts = []
    literal = ''
    escaped = False
    for char in pattern:
        if escaped:
            literal += char
            escaped = False
        elif escape and char == escape:
            escaped = True
        elif char == '%':
            parts.append(re.escape(literal))
            literal = ''
            parts.append('.*')
        elif char == '_':
            parts.append(re.escape(literal))
            literal = ''
            parts.append('.')
        else:
            literal += char
    parts.append(re.escape(literal))
    return re.compile(''.join(parts) + r'\Z', re.IGNORECASE | re.DOTALL)


def _unicode_like(pattern, value, escape=None):
    """Замена встроенной функции LIKE, понимающая кириллицу.

    Встроенный LIKE в SQLite приводит к одному регистру только латиницу:
    «скрип» не находит «Скрип», а «БУАД» не находит «буад». В приложении,
    где все данные на русском, это означает, что поиск работает через раз —
    и не только в заказах, а везде, где используется icontains.

    SQLite разрешает переопределять like() своей функцией. Плата за это —
    вызов Python на каждое сравнение и потеря оптимизации LIKE по индексу,
    но искать по подстроке («%текст%») индекс всё равно не помогает,
    а объёмы здесь измеряются тысячами строк, не миллионами.
    """
    if pattern is None or value is None:
        return False
    return _like_pattern(str(pattern), escape).match(str(value)) is not None


@receiver(connection_created)
def configure_sqlite(sender, connection, **kwargs):
    """Настройка SQLite для многопользовательской работы.

    journal_mode=WAL — читатели не блокируются пишущим соединением. Без него
    страница у одного сотрудника падает с 'database is locked', пока другой
    оформляет приход на склад.

    synchronous=FULL — оставляем максимальную надёжность вместо скорости:
    приложение рассчитано на Raspberry Pi, где отключение питания реально,
    а объём записи мал и выигрыш от NORMAL всё равно незаметен.

    busy_timeout — сколько ждать освобождения блокировки, прежде чем упасть
    с ошибкой. Значение по умолчанию (5 с) мало при одновременной работе.
    """
    if connection.vendor != 'sqlite':
        return
    with connection.cursor() as cursor:
        cursor.execute('PRAGMA journal_mode=WAL;')
        cursor.execute('PRAGMA synchronous=FULL;')
        cursor.execute('PRAGMA busy_timeout=30000;')

    # Поиск без учёта регистра для кириллицы (подробности в _unicode_like).
    # Django строит icontains и как LIKE ? ESCAPE ?, и как LIKE ? — нужны
    # обе арности, иначе часть запросов пойдёт мимо замены
    connection.connection.create_function('like', 2, _unicode_like, deterministic=True)
    connection.connection.create_function('like', 3, _unicode_like, deterministic=True)


@receiver(post_save, sender=RepairOrder)
def create_status_history(sender, instance, created, **kwargs):
    """Создание начальной записи истории при создании заказа."""
    if created:
        OrderStatusHistory.objects.create(
            order=instance,
            status=instance.status,
            payment_status=instance.payment_status,
            changed_at=timezone.now(),
            notes='Заказ создан'
        )


@receiver(post_save, sender=SparePart)
def notify_stock_update(sender, instance, created, **kwargs):
    """Отправка WebSocket-уведомления при изменении остатка детали."""
    update_fields = kwargs.get('update_fields') or []
    if not created and 'current_stock' not in update_fields:
        return

    # Ушла в дефицит — ставим письмо кладовщикам в очередь. Импорт внутри
    # функции: core.notifications обращается к моделям, а сигналы грузятся
    # раньше, чем приложение готово.
    from . import notifications
    notifications.notify_low_stock(instance)
    channel_layer = get_channel_layer()
    async_to_sync(channel_layer.group_send)(
        "stock_updates",
        {
            "type": "stock_update",
            "data": {
                "part_id": instance.id,
                "part_number": instance.part_number,
                "name": instance.name,
                "current_stock": instance.current_stock,
                "min_stock": instance.min_stock,
                "is_below_min": instance.is_below_min_stock(),
            }
        }
    )






@receiver(post_save, sender=Payment)
@receiver(post_delete, sender=Payment)
def refresh_payment_status(sender, instance, **kwargs):
    """Статус оплаты идёт следом за деньгами.

    Иначе он расходится с суммами: внесли последний платёж, а заказ
    остался «частично оплачен» и продолжал висеть в должниках и в
    напоминаниях.
    """
    order = instance.repair_order
    # Заказ мог удаляться целиком — тогда пересчитывать нечего и незачем
    if not RepairOrder.objects.filter(pk=order.pk).exists():
        return
    order.refresh_from_db(fields=['payment_status'])
    order.refresh_payment_status()


@receiver(pre_delete, sender=Payment)
def release_bank_operation(sender, instance, **kwargs):
    """Удалили оплату — поступление снова считается неразнесённым.

    Иначе оно осталось бы помеченным «разнесено» без единой оплаты за этим
    словом: деньги в банке есть, по заказу их нет, и никто об этом не узнает.

    Именно pre_delete: к post_delete Django уже обнулит ссылку по SET_NULL,
    и найти нужное поступление будет не по чему.
    """
    BankOperation.objects.filter(payment_id=instance.pk).update(
        status='new', payment=None, processed_by=None, processed_at=None
    )
