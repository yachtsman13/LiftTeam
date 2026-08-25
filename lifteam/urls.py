"""
URL configuration for lifteam project.
v2.71.0
"""
from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    path('admin/', admin.site.urls),
    # Приём уведомлений от банков. Отдельный набор маршрутов и отдельный
    # префикс — это то единственное, что открывается в интернет с домашнего
    # адреса; всё остальное приложение доступно только по Tailscale.
    # Ничего, кроме приёма, под этот префикс не заводить: nginx пускает
    # снаружи весь /webhooks/ целиком (см. DEPLOY.md, раздел «Приём
    # уведомлений от банков»).
    path('webhooks/', include('core.webhook_urls')),
    path('', include('core.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)




