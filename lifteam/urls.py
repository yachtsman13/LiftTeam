"""
URL configuration for lifteam project.
v2.123.0
"""
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    # Встроенная админка Django (admin.site.urls) здесь НЕ подключена —
    # с v2.121.0, намеренно. Она проверяет только is_staff/is_superuser
    # и не знает о Position/PermissionCode вовсе: через неё можно было бы
    # править Employee.position, Payment, StockMovement в обход
    # _admin_access_kept()/LastAdminError, которые защищают только
    # штатный путь сохранения (views.py). Своя административная часть
    # программы живёт под /management/ (views.admin_*) и идёт через
    # тот же permission_required, что и всё остальное — см. CLAUDE.md.
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




