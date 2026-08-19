"""Адреса, открытые в интернет. Всё остальное приложение — только по Tailscale.

Отдельный файл маршрутов ради одного: обратный прокси на Raspberry Pi
пускает снаружи запросы, начинающиеся с `/webhooks/`, и отвечает 404 на
все прочие. Значит, под этим префиксом не должно оказаться ничего, кроме
приёма уведомлений от банков, — иначе оно тоже станет доступно миру.
За этим следит тест `WebhookUrlIsolationTests` в `core/tests.py`: он
проверяет, что под префиксом нет посторонних маршрутов, а вьюхи приёма
не встречаются нигде вне его.

Новый банк — одна строка здесь и класс проверяющего в `core/webhooks.py`.
Ничего, кроме приёма, сюда не добавлять: этот файл — не «маршруты для
внешних систем вообще», а именно то, что выставлено наружу.
"""
from django.urls import path

from . import invoicing, webhook_views

urlpatterns = [
    path('tbank/', webhook_views.receive, {'provider': invoicing.TBANK},
         name='webhook_tbank'),
    path('tochka/', webhook_views.receive, {'provider': invoicing.TOCHKA},
         name='webhook_tochka'),
]
