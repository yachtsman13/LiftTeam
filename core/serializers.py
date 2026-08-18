"""
Django REST Framework serializers для LiftTeam v2.47.0.
"""
from rest_framework import serializers
from .models import (
    Client, EquipmentModel, Equipment, RepairOrder, RepairOrderEquipment,
    SparePart, StorageCell, StockMovement, RepairOrderDetail, OrderStatusHistory, Employee
)


class ClientSerializer(serializers.ModelSerializer):
    class Meta:
        model = Client
        fields = '__all__'


class EquipmentModelSerializer(serializers.ModelSerializer):
    class Meta:
        model = EquipmentModel
        fields = '__all__'


class EquipmentSerializer(serializers.ModelSerializer):
    model_name = serializers.CharField(source='model.name', read_only=True)
    client_name = serializers.CharField(source='current_client.name', read_only=True)

    class Meta:
        model = Equipment
        fields = '__all__'


class RepairOrderEquipmentSerializer(serializers.ModelSerializer):
    equipment_info = serializers.SerializerMethodField()

    class Meta:
        model = RepairOrderEquipment
        fields = '__all__'

    def get_equipment_info(self, obj):
        return str(obj.equipment)


class RepairOrderDetailSerializer(serializers.ModelSerializer):
    part_name = serializers.CharField(source='part.name', read_only=True)
    part_number = serializers.CharField(source='part.part_number', read_only=True)

    class Meta:
        model = RepairOrderDetail
        fields = '__all__'


class OrderStatusHistorySerializer(serializers.ModelSerializer):
    changed_by_name = serializers.CharField(source='changed_by.full_name', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    payment_status_display = serializers.CharField(source='get_payment_status_display', read_only=True)

    class Meta:
        model = OrderStatusHistory
        fields = '__all__'


class RepairOrderSerializer(serializers.ModelSerializer):
    client_name = serializers.CharField(source='client.name', read_only=True)
    equipment_info = serializers.SerializerMethodField()
    order_equipments = RepairOrderEquipmentSerializer(many=True, read_only=True)
    details = RepairOrderDetailSerializer(many=True, read_only=True)
    status_history = OrderStatusHistorySerializer(many=True, read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    payment_status_display = serializers.CharField(source='get_payment_status_display', read_only=True)
    total_repair_cost = serializers.DecimalField(source='total_repair_cost', max_digits=12, decimal_places=2, read_only=True)

    class Meta:
        model = RepairOrder
        fields = '__all__'

    def get_equipment_info(self, obj):
        return ', '.join([str(oe.equipment) for oe in obj.order_equipments.all()])


class SparePartSerializer(serializers.ModelSerializer):
    is_below_min = serializers.BooleanField(source='is_below_min_stock', read_only=True)
    storage_cell_address = serializers.SerializerMethodField()
    stock_deficit = serializers.IntegerField(read_only=True)

    class Meta:
        model = SparePart
        fields = '__all__'

    def get_storage_cell_address(self, obj):
        cell = obj.current_cell
        return cell.address if cell else None


class StorageCellSerializer(serializers.ModelSerializer):
    parts_info = SparePartSerializer(source='parts', many=True, read_only=True)
    status = serializers.CharField(source='get_status', read_only=True)

    class Meta:
        model = StorageCell
        fields = '__all__'


class StockMovementSerializer(serializers.ModelSerializer):
    part_name = serializers.CharField(source='part.name', read_only=True)
    part_number = serializers.CharField(source='part.part_number', read_only=True)
    movement_type_display = serializers.CharField(source='get_movement_type_display', read_only=True)
    created_by_name = serializers.CharField(source='created_by.full_name', read_only=True)

    class Meta:
        model = StockMovement
        fields = '__all__'


class EmployeeSerializer(serializers.ModelSerializer):
    role_display = serializers.CharField(source='get_role_display', read_only=True)

    class Meta:
        model = Employee
        fields = ['id', 'username', 'full_name', 'email', 'role', 'role_display', 'is_active', 'date_joined']




