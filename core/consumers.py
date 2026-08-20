"""
WebSocket consumers для LiftTeam v2.56.1.
"""
import json

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.conf import settings

PRESENCE_GROUP = "presence"


class StockConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        # Остатки склада — не публичные данные. Раньше сокет принимал любого,
        # кто дотянулся до порта: страницы закрыты входом, а этот канал нет.
        user = self.scope.get('user')
        if user is None or not user.is_authenticated:
            await self.close()
            return

        await self.channel_layer.group_add("stock_updates", self.channel_name)
        await self.accept()
        await self.send(text_data=json.dumps({
            "type": "connection_established",
            "message": "Connected to stock updates"
        }))

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard("stock_updates", self.channel_name)

    async def receive(self, text_data):
        data = json.loads(text_data)
        if data.get('action') == 'ping':
            await self.send(text_data=json.dumps({"type": "pong"}))

    async def stock_update(self, event):
        await self.send(text_data=json.dumps({
            "type": "stock_update",
            "data": event['data']
        }))


class PresenceConsumer(AsyncWebsocketConsumer):
    """Присутствие сотрудников: кто сейчас за терминалом.

    Отдельный потребитель, а не ветка в StockConsumer: тот подключается
    только там, где показаны остатки, а присутствие должно отмечаться
    на любой открытой странице.

    Живое соединение и есть признак присутствия. Браузер шлёт сигнал
    каждые PRESENCE_TIMEOUT_SECONDS / 3 секунд, отметка времени ложится
    в Employee.last_seen, а «в сети» считается от неё и таймаута.
    При разрыве соединения отметку намеренно не сбрасываем: обрыв
    неотличим от заминки в сети, и гасить индикатор сразу нельзя.
    """

    async def connect(self):
        # Тот же порядок, что и в StockConsumer: канал закрыт для тех,
        # кто не вошёл в систему.
        user = self.scope.get('user')
        if user is None or not user.is_authenticated:
            await self.close()
            return

        await self.channel_layer.group_add(PRESENCE_GROUP, self.channel_name)
        await self.accept()
        await self.touch()
        await self.send(text_data=json.dumps({
            "type": "connection_established",
            # Период сигнала задаём с сервера: держать его ещё и в скрипте
            # значило бы, что при правке таймаута их легко рассогласовать.
            "heartbeat_seconds": max(1, settings.PRESENCE_TIMEOUT_SECONDS // 3),
        }))
        await self.broadcast_roster()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(PRESENCE_GROUP, self.channel_name)
        # Отметку не трогаем — человек «уходит в офлайн» сам, по таймауту.
        # Рассылаем только затем, чтобы остальные увидели список без задержки,
        # когда отметка ушедшего уже устарела.
        await self.broadcast_roster()

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
        except (ValueError, TypeError):
            return
        if data.get('action') == 'ping':
            await self.touch()
            await self.send(text_data=json.dumps({"type": "pong"}))
            # Отдельного таймера на сервере нет и не нужно: каждый
            # подключённый шлёт сигнал сам, поэтому список рассылается
            # не реже, чем раз в интервал сигнала, и человек, у которого
            # отметка устарела, гаснет у всех без всякого расписания.
            await self.broadcast_roster()

    async def broadcast_roster(self):
        roster = await self.roster()
        await self.channel_layer.group_send(PRESENCE_GROUP, {
            "type": "presence_roster",
            "roster": roster,
        })

    async def presence_roster(self, event):
        await self.send(text_data=json.dumps({
            "type": "presence_roster",
            "roster": event['roster'],
        }))

    @database_sync_to_async
    def touch(self):
        user = self.scope.get('user')
        if user is not None and user.is_authenticated:
            user.touch_presence()

    @database_sync_to_async
    def roster(self):
        from .models import Employee

        return [
            {
                'id': employee.pk,
                'full_name': employee.full_name,
                'role': employee.get_role_display(),
                'last_seen': employee.last_seen.isoformat() if employee.last_seen else None,
                'online': employee.is_online,
            }
            for employee in Employee.objects.filter(is_active=True)
        ]




