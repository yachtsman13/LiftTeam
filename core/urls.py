"""
URL-маршруты приложения core.
v2.114.1
"""
from django.urls import path
from . import views

urlpatterns = [
    # Аутентификация
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('my-notifications/', views.my_notifications, name='my_notifications'),
    path('presence/', views.presence, name='presence'),

    # Дашборд
    path('', views.dashboard, name='dashboard'),

    # Клиенты
    path('price-lists/', views.price_list_index, name='price_list_index'),
    path('price-lists/create/', views.price_list_create, name='price_list_create'),
    path('price-lists/<int:pk>/edit/', views.price_list_edit, name='price_list_edit'),
    path('price-lists/<int:pk>/delete/', views.price_list_delete, name='price_list_delete'),
    path('clients/', views.client_list, name='client_list'),
    path('clients/export/', views.client_export, name='client_export'),
    path('clients/create/', views.client_create, name='client_create'),
    path('clients/<int:pk>/edit/', views.client_edit, name='client_edit'),
    path('clients/<int:pk>/delete/', views.client_delete, name='client_delete'),

    # Оборудование
    path('equipment/', views.equipment_list, name='equipment_list'),
    path('equipment/export/', views.equipment_export, name='equipment_export'),
    path('equipment/create/', views.equipment_create, name='equipment_create'),
    path('equipment/<int:pk>/edit/', views.equipment_edit, name='equipment_edit'),
    path('equipment/<int:pk>/history/', views.equipment_history, name='equipment_history'),
    path('equipment/<int:pk>/history/export/', views.equipment_history_export, name='equipment_history_export'),
    path('equipment/<int:pk>/delete/', views.equipment_delete, name='equipment_delete'),
    path('equipment/models/', views.equipment_model_list, name='equipment_model_list'),
    path('equipment/models/create/', views.equipment_model_create, name='equipment_model_create'),
    path('equipment/models/<int:pk>/edit/', views.equipment_model_edit, name='equipment_model_edit'),
    path('equipment/models/<int:pk>/delete/', views.equipment_model_delete, name='equipment_model_delete'),

    # Справочник типов оборудования: «Привод дверей», «Преобразователь частоты»
    path('equipment/types/', views.equipment_type_list, name='equipment_type_list'),
    path('equipment/types/create/', views.equipment_type_create, name='equipment_type_create'),
    path('equipment/types/<int:pk>/edit/', views.equipment_type_edit, name='equipment_type_edit'),
    path('equipment/types/<int:pk>/delete/', views.equipment_type_delete, name='equipment_type_delete'),

    # Версии моделей: EkoDrive 2.0-1.1 и EkoDrive 2.0-0.7
    path('equipment/versions/', views.equipment_version_list, name='equipment_version_list'),
    path('equipment/versions/create/', views.equipment_version_create, name='equipment_version_create'),
    path('equipment/versions/<int:pk>/edit/', views.equipment_version_edit, name='equipment_version_edit'),
    path('equipment/versions/<int:pk>/delete/', views.equipment_version_delete, name='equipment_version_delete'),

    # Типовые неисправности и их рецепты деталей
    path('faults/', views.fault_type_list, name='fault_type_list'),
    path('faults/create/', views.fault_type_create, name='fault_type_create'),
    path('faults/<int:pk>/edit/', views.fault_type_edit, name='fault_type_edit'),
    path('faults/<int:pk>/delete/', views.fault_type_delete, name='fault_type_delete'),

    # Технологические карты: как это делают руками. Карта привязана
    # к модели всегда, к неисправности — по желанию.
    path('tech-cards/', views.tech_card_list, name='tech_card_list'),
    path('tech-cards/create/', views.tech_card_create, name='tech_card_create'),
    path('tech-cards/<int:pk>/', views.tech_card_detail, name='tech_card_detail'),
    path('tech-cards/<int:pk>/edit/', views.tech_card_edit, name='tech_card_edit'),
    path('tech-cards/<int:pk>/delete/', views.tech_card_delete, name='tech_card_delete'),

    # Заказы на ремонт
    path('repair-orders/', views.repair_order_list, name='repair_order_list'),
    path('repair-orders/export/', views.repair_order_export, name='repair_order_export'),
    path('repair-orders/labels/', views.repair_order_labels_batch, name='repair_order_labels_batch'),
    path('repair-orders/<int:pk>/add-unit/', views.repair_order_add_unit, name='repair_order_add_unit'),
    path('repair-orders/<int:pk>/edit-info/', views.repair_order_edit_info, name='repair_order_edit_info'),
    path('repair-orders/<int:pk>/details/<int:detail_pk>/return/', views.repair_order_return_detail, name='repair_order_return_detail'),
    path('repair-orders/<int:pk>/details/<int:detail_pk>/write-off/', views.repair_order_write_off_detail, name='repair_order_write_off_detail'),
    path('repair-orders/<int:pk>/details/<int:detail_pk>/cancel/', views.repair_order_cancel_planned_detail, name='repair_order_cancel_planned_detail'),
    path('repair-orders/<int:pk>/labels/', views.repair_order_equipment_labels, name='repair_order_equipment_labels'),
    path('repair-orders/bulk-status/', views.repair_order_bulk_status, name='repair_order_bulk_status'),
    path('repair-orders/create/', views.repair_order_create, name='repair_order_create'),
    path('repair-orders/<int:pk>/', views.repair_order_detail, name='repair_order_detail'),
    path('repair-orders/<int:pk>/delete/', views.repair_order_delete, name='repair_order_delete'),
    path('repair-orders/<int:pk>/add-detail/', views.repair_order_add_detail, name='repair_order_add_detail'),
    path('repair-orders/<int:pk>/apply-fault-template/', views.repair_order_apply_fault_template, name='repair_order_apply_fault_template'),
    path('repair-orders/<int:pk>/change-status/', views.repair_order_change_status, name='repair_order_change_status'),
    path('repair-orders/<int:pk>/change-payment-status/', views.repair_order_change_payment_status, name='repair_order_change_payment_status'),
    path('repair-orders/<int:pk>/payments/add/', views.repair_order_add_payment, name='repair_order_add_payment'),
    path('repair-orders/<int:pk>/payments/<int:payment_pk>/delete/', views.repair_order_delete_payment, name='repair_order_delete_payment'),
    # Страница единицы: всё про этот прибор в этой работе. Сюда же ведёт
    # наклейка с прибора (короткий адрес /u/<id>/).
    path('repair-orders/<int:order_pk>/equipment/<int:roe_pk>/', views.repair_order_unit_detail, name='repair_order_unit_detail'),
    path('repair-orders/<int:order_pk>/equipment/<int:roe_pk>/edit/', views.repair_order_unit_edit, name='repair_order_unit_edit'),
    path('repair-orders/<int:order_pk>/equipment/<int:roe_pk>/remove/', views.repair_order_unit_remove, name='repair_order_unit_remove'),
    path('repair-orders/<int:order_pk>/equipment/<int:roe_pk>/label/', views.repair_order_equipment_label, name='repair_order_equipment_label'),
    path('repair-orders/<int:pk>/act/receive/', views.repair_order_act_receive, name='repair_order_act_receive'),
    path('repair-orders/<int:pk>/act/complete/', views.repair_order_act_complete, name='repair_order_act_complete'),
    path('repair-orders/<int:order_pk>/equipment/<int:roe_pk>/defect/', views.repair_order_defect_act_edit, name='repair_order_defect_act_edit'),
    path('repair-orders/<int:order_pk>/equipment/<int:roe_pk>/disk-folder/', views.repair_order_unit_disk_folder, name='repair_order_unit_disk_folder'),
    path('repair-orders/<int:order_pk>/equipment/<int:roe_pk>/disk-folder/set/', views.repair_order_unit_disk_folder_set, name='repair_order_unit_disk_folder_set'),
    path('repair-orders/<int:order_pk>/equipment/<int:roe_pk>/parts/write-off/', views.repair_order_unit_write_off_planned, name='repair_order_unit_write_off_planned'),
    path('repair-orders/<int:order_pk>/equipment/<int:roe_pk>/act/defect/', views.repair_order_act_defect, name='repair_order_act_defect'),

    # Детали
    path('parts/', views.part_list, name='part_list'),
    path('parts/search/', views.part_search, name='part_search'),
    path('parts/<int:pk>/', views.part_detail, name='part_detail'),
    path('parts/create/', views.part_create, name='part_create'),
    path('parts/<int:pk>/edit/', views.part_edit, name='part_edit'),
    path('parts/<int:pk>/delete/', views.part_delete, name='part_delete'),
    path('parts/delete-selected/', views.part_bulk_delete, name='part_bulk_delete'),
    path('parts/<int:pk>/stock-incoming/', views.part_stock_incoming, name='part_stock_incoming'),
    path('parts/<int:pk>/stock-outgoing/', views.part_stock_outgoing, name='part_stock_outgoing'),
    path('parts/<int:pk>/assign-cell/', views.part_assign_cell, name='part_assign_cell'),
    path('parts/import/', views.part_import, name='part_import'),
    path('parts/export/', views.part_export, name='part_export'),
    path('parts/<int:pk>/label/', views.part_label, name='part_label'),
    path('parts/labels/', views.part_labels_batch, name='part_labels_batch'),

    # Сканирование. Страница «что это за коробка на полке»: поднести любой
    # код и увидеть, что это и что с этим делать. Сам разбор кода — общий
    # (core/scanning.py), и на нём же стоит слой сканера в браузере.
    path('scan/', views.scan_page, name='scan_page'),
    path('scan/resolve/', views.scan_resolve, name='scan_resolve'),

    # Короткие адреса для QR-кодов: чем короче ссылка, тем крупнее модули кода.
    # `e` сохранён, хотя отдельную этикетку оборудования больше не печатают:
    # коды с уже наклеенных этикеток должны открываться и дальше.
    path('p/<int:pk>/', views.short_part, name='short_part'),
    path('c/<int:pk>/', views.short_cell, name='short_cell'),
    path('e/<int:pk>/', views.short_equipment, name='short_equipment'),
    path('o/<int:pk>/', views.short_order, name='short_order'),
    path('u/<int:pk>/', views.short_order_equipment, name='short_order_equipment'),

    # Те же адреса без косой черты на конце — именно они попадают в QR-коды.
    # Экономия ровно в один символ, но на этикетке заказа она решает: там
    # QR всего 9,6 мм, и 27-й символ переводит код с 25 модулей на 29,
    # то есть с 2,8 точки принтера на модуль до 2,5 — а 2,5 на практике
    # уже не считывалось. Без этих маршрутов сработал бы APPEND_SLASH,
    # но это лишний переход туда-обратно у человека со сканером в руках.
    # Имён нет намеренно: reverse() должен и дальше давать канонический
    # адрес с чертой, эти — только точка входа для сканера.
    path('p/<int:pk>', views.short_part),
    path('c/<int:pk>', views.short_cell),
    path('e/<int:pk>', views.short_equipment),
    path('o/<int:pk>', views.short_order),
    path('u/<int:pk>', views.short_order_equipment),

    # Ячейки хранения
    path('storage-cells/', views.storage_cell_grid, name='storage_cell_grid'),
    path('storage-cells/cabinets/', views.cabinet_list, name='cabinet_list'),
    path('storage-cells/cabinets/create/', views.cabinet_create, name='cabinet_create'),
    path('storage-cells/cabinets/<int:pk>/edit/', views.cabinet_edit, name='cabinet_edit'),
    path('storage-cells/cabinets/<int:pk>/delete/', views.cabinet_delete, name='cabinet_delete'),
    path('storage-cells/move/', views.storage_cell_move, name='storage_cell_move'),
    path('storage-cells/move-all/', views.storage_cell_move_all, name='storage_cell_move_all'),
    path('storage-cells/<int:pk>/label/', views.storage_cell_label, name='storage_cell_label'),
    path('storage-cells/labels/', views.storage_cell_labels_batch, name='storage_cell_labels_batch'),
    path('storage-cells/<int:pk>/add-part/', views.storage_cell_add_part, name='storage_cell_add_part'),
    path('storage-cells/<int:pk>/remove-part/', views.storage_cell_remove_part, name='storage_cell_remove_part'),

    # Инвентаризация
    path('inventory/', views.inventory_list, name='inventory_list'),
    path('inventory/start/<int:pk>/', views.inventory_start, name='inventory_start'),
    path('inventory/<int:pk>/count/', views.inventory_count, name='inventory_count'),
    path('inventory/<int:pk>/confirm/', views.inventory_confirm, name='inventory_confirm'),
    path('inventory/<int:pk>/delete/', views.inventory_delete, name='inventory_delete'),
    path('inventory/<int:pk>/', views.inventory_detail, name='inventory_detail'),

    path('repair-orders/<int:pk>/quote/edit/', views.repair_order_quote_edit, name='repair_order_quote_edit'),
    path('repair-orders/<int:pk>/quote/', views.repair_order_quote, name='repair_order_quote'),
    path('repair-orders/<int:pk>/invoice/', views.repair_order_invoice, name='repair_order_invoice'),
    path('repair-orders/<int:pk>/invoice/show-work/', views.repair_order_invoice_show_work,
         name='repair_order_invoice_show_work'),
    path('repair-orders/<int:pk>/invoice/pdf/', views.repair_order_invoice_pdf, name='repair_order_invoice_pdf'),
    path('repair-orders/<int:pk>/utd/', views.repair_order_utd, name='repair_order_utd'),

    # Поступления из банка
    path('bank/operations/', views.bank_operations, name='bank_operations'),
    path('bank/operations/fetch/', views.bank_statement_fetch, name='bank_statement_fetch'),
    path('bank/operations/<int:pk>/apply/', views.bank_operation_apply, name='bank_operation_apply'),
    path('bank/operations/<int:pk>/skip/', views.bank_operation_skip, name='bank_operation_skip'),
    path('bank/operations/<int:pk>/reset/', views.bank_operation_reset, name='bank_operation_reset'),

    # Отчёты
    path('reports/', views.reports, name='reports'),
    path('reports/purchase-plan/', views.report_purchase_plan, name='report_purchase_plan'),
    path('reports/purchase-plan/export/', views.report_purchase_plan_export, name='report_purchase_plan_export'),
    path('reports/stock-movements/', views.report_stock_movements, name='report_stock_movements'),
    path('reports/stock-movements/export/', views.report_stock_movements_export, name='report_stock_movements_export'),
    path('reports/debtors/', views.report_debtors, name='report_debtors'),
    path('reports/debtors/export/', views.report_debtors_export, name='report_debtors_export'),
    path('reports/repair-analytics/', views.report_repair_analytics, name='report_repair_analytics'),
    path('reports/repair-analytics/export/', views.report_repair_analytics_export, name='report_repair_analytics_export'),
    path('reports/profit/', views.report_profit, name='report_profit'),
    path('reports/profit/export/', views.report_profit_export, name='report_profit_export'),

    # AJAX: создание модели, оборудования и заказчика из формы заказа
    path('ajax/equipment-model/create/', views.ajax_equipment_model_create, name='ajax_equipment_model_create'),
    path('ajax/equipment-model/list/', views.ajax_equipment_model_list, name='ajax_equipment_model_list'),
    path('ajax/equipment/create/', views.ajax_equipment_create, name='ajax_equipment_create'),
    path('ajax/equipment/<int:pk>/history-summary/', views.ajax_equipment_history_summary, name='ajax_equipment_history_summary'),
    path('ajax/equipment/<int:pk>/faults/', views.ajax_equipment_faults, name='ajax_equipment_faults'),
    path('ajax/client/create/', views.ajax_client_create, name='ajax_client_create'),

    # Администрирование
    # Примечание: НЕ используем префикс admin/, т.к. он занят django.contrib.admin
    # (path('admin/', admin.site.urls) в lifteam/urls.py "съедает" весь префикс admin/
    # и эти маршруты становятся недостижимы).
    path('management/users/', views.admin_users, name='admin_users'),
    path('management/users/create/', views.admin_user_create, name='admin_user_create'),
    path('management/users/<int:pk>/edit/', views.admin_user_edit, name='admin_user_edit'),
    # Должности с правами — с v2.98.0 вместо четырёх зашитых в код ролей
    path('management/positions/', views.admin_positions, name='admin_positions'),
    path('management/positions/create/', views.admin_position_create, name='admin_position_create'),
    path('management/positions/<int:pk>/edit/', views.admin_position_edit, name='admin_position_edit'),
    path('management/positions/<int:pk>/delete/', views.admin_position_delete, name='admin_position_delete'),
    path('management/organization/', views.admin_organization, name='admin_organization'),
    path('management/notifications/', views.admin_notifications, name='admin_notifications'),
    path('management/notifications/<int:pk>/retry/', views.admin_notification_retry, name='admin_notification_retry'),
    path('management/settings/', views.admin_settings, name='admin_settings'),
    path('management/settings/save/', views.admin_settings_save, name='admin_settings_save'),
    path('management/settings/check/', views.admin_settings_check, name='admin_settings_check'),
    path('management/settings/restart/', views.admin_settings_restart, name='admin_settings_restart'),
    path('management/update/', views.admin_update, name='admin_update'),
    path('management/update/status/', views.admin_update_status, name='admin_update_status'),
]




