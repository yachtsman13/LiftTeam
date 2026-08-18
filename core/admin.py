"""
Настройка Django Admin для LiftTeam v2.48.0.
"""
from django.contrib import admin
from .models import (
    Employee, Client, EquipmentModel, Equipment, FaultType, FaultTypePart, RepairOrder,
    RepairOrderEquipment, OrderStatusHistory, SparePart, StorageCell, RepairOrderDetail,
    StockMovement, StockAllocation, OrderCost, Payment
)


@admin.register(Employee)
class EmployeeAdmin(admin.ModelAdmin):
    list_display = ['username', 'full_name', 'email', 'role', 'is_active', 'is_staff', 'is_superuser']
    list_filter = ['role', 'is_active', 'is_staff', 'is_superuser']
    search_fields = ['username', 'full_name', 'email']
    ordering = ['username']
    fieldsets = (
        (None, {'fields': ('username', 'password')}),
        ('Личная информация', {'fields': ('full_name', 'email')}),
        ('Права доступа', {'fields': ('role', 'is_active', 'is_staff', 'is_superuser')}),
        ('Важные даты', {'fields': ('last_login', 'date_joined')}),
    )


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = ['name', 'inn', 'phone', 'email']
    search_fields = ['name', 'inn', 'contact_person']
    list_filter = ['name']


@admin.register(EquipmentModel)
class EquipmentModelAdmin(admin.ModelAdmin):
    search_fields = ['name']


@admin.register(Equipment)
class EquipmentAdmin(admin.ModelAdmin):
    list_display = ['model', 'serial_number', 'current_client']
    search_fields = ['serial_number', 'model__name']
    list_filter = ['model']


class FaultTypePartInline(admin.TabularInline):
    model = FaultTypePart
    extra = 1
    autocomplete_fields = ['part']


@admin.register(FaultType)
class FaultTypeAdmin(admin.ModelAdmin):
    list_display = ['name', 'equipment_model']
    list_filter = ['equipment_model']
    search_fields = ['name', 'equipment_model__name']
    inlines = [FaultTypePartInline]


class RepairOrderEquipmentInline(admin.TabularInline):
    model = RepairOrderEquipment
    extra = 1


class RepairOrderDetailInline(admin.TabularInline):
    model = RepairOrderDetail
    extra = 1
    autocomplete_fields = ['part']


class OrderStatusHistoryInline(admin.TabularInline):
    model = OrderStatusHistory
    extra = 0
    readonly_fields = ['status', 'payment_status', 'changed_at', 'changed_by']
    can_delete = False


@admin.register(RepairOrder)
class RepairOrderAdmin(admin.ModelAdmin):
    list_display = ['order_number', 'client', 'status', 'payment_status', 'date_received']
    list_filter = ['status', 'payment_status', 'date_received']
    search_fields = ['order_number', 'client__name', 'order_equipments__equipment__serial_number']
    readonly_fields = ['order_number', 'date_received']
    inlines = [RepairOrderEquipmentInline, RepairOrderDetailInline, OrderStatusHistoryInline]
    date_hierarchy = 'date_received'


@admin.register(SparePart)
class SparePartAdmin(admin.ModelAdmin):
    list_display = ['part_number', 'name', 'component_type', 'package', 'current_stock', 'min_stock', 'price', 'lead_time_days']
    list_filter = ['component_type', 'package']
    search_fields = ['part_number', 'name', 'component_type', 'package']
    readonly_fields = ['current_stock']


@admin.register(StorageCell)
class StorageCellAdmin(admin.ModelAdmin):
    list_display = ['address', 'cabinet', 'row_number', 'cell_row', 'parts_display']
    list_filter = ['cabinet']
    search_fields = ['parts__part_number', 'parts__name']
    filter_horizontal = ['parts']

    @admin.display(description='Детали')
    def parts_display(self, obj):
        return ', '.join(p.part_number for p in obj.parts.all()) or '—'


@admin.register(StockMovement)
class StockMovementAdmin(admin.ModelAdmin):
    list_display = ['part', 'movement_type', 'quantity', 'unit_price', 'movement_date', 'document_number', 'created_by']
    list_filter = ['movement_type', 'movement_date']
    search_fields = ['part__part_number', 'document_number']
    date_hierarchy = 'movement_date'


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ['repair_order', 'amount', 'payment_date', 'note', 'created_by']
    list_filter = ['payment_date']
    search_fields = ['repair_order__order_number', 'note']
    date_hierarchy = 'payment_date'


@admin.register(StockAllocation)
class StockAllocationAdmin(admin.ModelAdmin):
    list_display = ['outgoing', 'incoming', 'quantity']
    search_fields = ['outgoing__part__part_number', 'incoming__part__part_number']


@admin.register(OrderCost)
class OrderCostAdmin(admin.ModelAdmin):
    list_display = ['repair_order', 'category', 'amount', 'created_at']
    list_filter = ['category']
    search_fields = ['repair_order__order_number']
    date_hierarchy = 'created_at'
