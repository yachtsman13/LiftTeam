"""
URL-маршруты приложения core.
v2.6.2
"""
from django.urls import path
from . import views

urlpatterns = [
    # Аутентификация
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),

    # Дашборд
    path('', views.dashboard, name='dashboard'),

    # Клиенты
    path('clients/', views.client_list, name='client_list'),
    path('clients/create/', views.client_create, name='client_create'),
    path('clients/<int:pk>/edit/', views.client_edit, name='client_edit'),
    path('clients/<int:pk>/delete/', views.client_delete, name='client_delete'),

    # Оборудование
    path('equipment/', views.equipment_list, name='equipment_list'),
    path('equipment/create/', views.equipment_create, name='equipment_create'),
    path('equipment/<int:pk>/edit/', views.equipment_edit, name='equipment_edit'),
    path('equipment/<int:pk>/delete/', views.equipment_delete, name='equipment_delete'),
    path('equipment/models/', views.equipment_model_list, name='equipment_model_list'),
    path('equipment/models/create/', views.equipment_model_create, name='equipment_model_create'),
    path('equipment/models/<int:pk>/edit/', views.equipment_model_edit, name='equipment_model_edit'),
    path('equipment/models/<int:pk>/delete/', views.equipment_model_delete, name='equipment_model_delete'),

    # Заказы на ремонт
    path('repair-orders/', views.repair_order_list, name='repair_order_list'),
    path('repair-orders/create/', views.repair_order_create, name='repair_order_create'),
    path('repair-orders/<int:pk>/', views.repair_order_detail, name='repair_order_detail'),
    path('repair-orders/<int:pk>/edit/', views.repair_order_edit, name='repair_order_edit'),
    path('repair-orders/<int:pk>/delete/', views.repair_order_delete, name='repair_order_delete'),
    path('repair-orders/<int:pk>/add-detail/', views.repair_order_add_detail, name='repair_order_add_detail'),
    path('repair-orders/<int:pk>/change-status/', views.repair_order_change_status, name='repair_order_change_status'),
    path('repair-orders/<int:pk>/change-payment-status/', views.repair_order_change_payment_status, name='repair_order_change_payment_status'),
    path('repair-orders/<int:order_pk>/equipment/<int:roe_pk>/label/', views.repair_order_equipment_label, name='repair_order_equipment_label'),

    # Детали
    path('parts/', views.part_list, name='part_list'),
    path('parts/<int:pk>/', views.part_detail, name='part_detail'),
    path('parts/create/', views.part_create, name='part_create'),
    path('parts/<int:pk>/edit/', views.part_edit, name='part_edit'),
    path('parts/<int:pk>/delete/', views.part_delete, name='part_delete'),
    path('parts/<int:pk>/stock-incoming/', views.part_stock_incoming, name='part_stock_incoming'),
    path('parts/<int:pk>/stock-outgoing/', views.part_stock_outgoing, name='part_stock_outgoing'),
    path('parts/<int:pk>/assign-cell/', views.part_assign_cell, name='part_assign_cell'),
    path('parts/import/', views.part_import, name='part_import'),
    path('parts/export/', views.part_export, name='part_export'),

    # Ячейки хранения
    path('storage-cells/', views.storage_cell_grid, name='storage_cell_grid'),
    path('storage-cells/move/', views.storage_cell_move, name='storage_cell_move'),
    path('storage-cells/<int:pk>/label/', views.storage_cell_label, name='storage_cell_label'),
    path('storage-cells/<int:pk>/add-part/', views.storage_cell_add_part, name='storage_cell_add_part'),
    path('storage-cells/<int:pk>/remove-part/', views.storage_cell_remove_part, name='storage_cell_remove_part'),

    # Этикетки оборудования
    path('equipment/<int:pk>/label/', views.equipment_label, name='equipment_label'),

    # Отчёты
    path('reports/', views.reports, name='reports'),
    path('reports/purchase-plan/', views.report_purchase_plan, name='report_purchase_plan'),
    path('reports/stock-movements/', views.report_stock_movements, name='report_stock_movements'),
    path('reports/debtors/', views.report_debtors, name='report_debtors'),

    # AJAX: создание модели, оборудования и заказчика из формы заказа
    path('ajax/equipment-model/create/', views.ajax_equipment_model_create, name='ajax_equipment_model_create'),
    path('ajax/equipment-model/list/', views.ajax_equipment_model_list, name='ajax_equipment_model_list'),
    path('ajax/equipment/create/', views.ajax_equipment_create, name='ajax_equipment_create'),
    path('ajax/client/create/', views.ajax_client_create, name='ajax_client_create'),

    # Администрирование
    # Примечание: НЕ используем префикс admin/, т.к. он занят django.contrib.admin
    # (path('admin/', admin.site.urls) в lifteam/urls.py "съедает" весь префикс admin/
    # и эти маршруты становятся недостижимы).
    path('management/users/', views.admin_users, name='admin_users'),
    path('management/users/create/', views.admin_user_create, name='admin_user_create'),
    path('management/users/<int:pk>/edit/', views.admin_user_edit, name='admin_user_edit'),
]




