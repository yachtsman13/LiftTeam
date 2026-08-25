"""
ASGI config for lifteam project.
v2.73.0
"""
import os
from django.core.asgi import get_asgi_application
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.auth import AuthMiddlewareStack
from django.urls import path

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'lifteam.settings')

django_asgi_app = get_asgi_application()

from core.consumers import PresenceConsumer, StockConsumer

websocket_urlpatterns = [
    path("ws/stock/", StockConsumer.as_asgi()),
    path("ws/presence/", PresenceConsumer.as_asgi()),
]

application = ProtocolTypeRouter({
    "http": django_asgi_app,
    "websocket": AuthMiddlewareStack(
        URLRouter(websocket_urlpatterns)
    ),
})




