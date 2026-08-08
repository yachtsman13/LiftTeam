"""
Django signals для LiftTeam v2.6.1.
Сигналы используются только для:
- настройки параметров подключения к SQLite
- создания начальной записи истории статуса при создании заказа
- отправки WebSocket-уведомлений
"""
from django.db.backends.signals import connection_created
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from .models import RepairOrder, OrderStatusHistory, SparePart


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




