"""
WebSocket consumers для LiftTeam v2.8.0.
"""
import json
from channels.generic.websocket import AsyncWebsocketConsumer


class StockConsumer(AsyncWebsocketConsumer):
    async def connect(self):
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




