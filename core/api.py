"""
REST API для LiftTeam v2.39.3 (Django REST Framework).
"""
from django.db.models import Q
from django.utils import timezone
from rest_framework import viewsets, filters, status
from rest_framework.decorators import action
from rest_framework.response import Response
from django_filters.rest_framework import DjangoFilterBackend
from .models import (
    Client, EquipmentModel, Equipment, RepairOrder, SparePart,
    StorageCell, StockMovement, RepairOrderDetail, OrderStatusHistory
)
from .serializers import (
    ClientSerializer, EquipmentModelSerializer, EquipmentSerializer,
    RepairOrderSerializer, SparePartSerializer, StorageCellSerializer,
    StockMovementSerializer, RepairOrderDetailSerializer, OrderStatusHistorySerializer
)


class ClientViewSet(viewsets.ModelViewSet):
    queryset = Client.objects.all()
    serializer_class = ClientSerializer
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['name', 'inn', 'contact_person']
    ordering_fields = ['name', 'id']


class EquipmentModelViewSet(viewsets.ModelViewSet):
    queryset = EquipmentModel.objects.all()
    serializer_class = EquipmentModelSerializer


class EquipmentViewSet(viewsets.ModelViewSet):
    queryset = Equipment.objects.select_related('model', 'current_client')
    serializer_class = EquipmentSerializer
    filter_backends = [filters.SearchFilter]
    search_fields = ['serial_number', 'model__name']


class RepairOrderViewSet(viewsets.ModelViewSet):
    queryset = RepairOrder.objects.select_related('client').prefetch_related(
        'order_equipments__equipment', 'details', 'status_history'
    )
    serializer_class = RepairOrderSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status', 'payment_status', 'client']
    search_fields = ['order_number', 'client__name', 'order_equipments__equipment__serial_number']
    ordering_fields = ['date_received', 'status']

    @action(detail=True, methods=['post'])
    def status(self, request, pk=None):
        """Изменение статуса заказа."""
        order = self.get_object()
        new_status = request.data.get('status')
        if new_status not in dict(RepairOrder.STATUS_CHOICES):
            return Response({'error': 'Недопустимый статус'}, status=status.HTTP_400_BAD_REQUEST)

        old_status = order.status
        if old_status == new_status:
            return Response({'status': 'ok', 'message': 'Статус не изменился'})

        order.status = new_status
        if new_status == 'shipped':
            order.shipping_date = timezone.now()
            order.date_completed = timezone.now()
        else:
            order.date_completed = None
            if old_status == 'shipped':
                order.shipping_date = None
        order.save()

        # Создание записи истории
        OrderStatusHistory.objects.create(
            order=order,
            status=new_status,
            notes=f'Статус изменён через API с "{dict(RepairOrder.STATUS_CHOICES).get(old_status)}"'
        )

        return Response({'status': 'ok', 'new_status': order.status})

    @action(detail=True, methods=['get'])
    def history(self, request, pk=None):
        """История статусов заказа."""
        order = self.get_object()
        history = order.status_history.all()
        serializer = OrderStatusHistorySerializer(history, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['post'])
    def payment_status(self, request, pk=None):
        """Изменение статуса оплаты заказа."""
        order = self.get_object()
        new_status = request.data.get('payment_status')
        if new_status not in dict(RepairOrder.PAYMENT_STATUS_CHOICES):
            return Response({'error': 'Недопустимый статус оплаты'}, status=status.HTTP_400_BAD_REQUEST)

        old_status = order.payment_status
        if old_status == new_status:
            return Response({'status': 'ok', 'message': 'Статус оплаты не изменился'})

        order.payment_status = new_status
        order.save()

        # Создание записи истории
        OrderStatusHistory.objects.create(
            order=order,
            payment_status=new_status,
            notes=f'Статус оплаты изменён через API с "{dict(RepairOrder.PAYMENT_STATUS_CHOICES).get(old_status)}"'
        )

        return Response({'status': 'ok', 'new_payment_status': order.payment_status})


class SparePartViewSet(viewsets.ModelViewSet):
    queryset = SparePart.objects.all()
    serializer_class = SparePartSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['component_type', 'package']
    search_fields = ['part_number', 'name', 'component_type', 'package']
    ordering_fields = ['part_number', 'name', 'current_stock']

    @action(detail=False, methods=['get'])
    def search(self, request):
        """Поиск деталей с фильтрами."""
        queryset = self.get_queryset()
        search = request.query_params.get('q', '')
        if search:
            queryset = queryset.filter(
                Q(part_number__icontains=search) |
                Q(name__icontains=search) |
                Q(component_type__icontains=search) |
                Q(package__icontains=search)
            )
        component_type = request.query_params.get('component_type')
        if component_type:
            queryset = queryset.filter(component_type=component_type)
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)


class StorageCellViewSet(viewsets.ModelViewSet):
    queryset = StorageCell.objects.prefetch_related('parts')
    serializer_class = StorageCellSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['cabinet']


class StockMovementViewSet(viewsets.ModelViewSet):
    queryset = StockMovement.objects.select_related('part', 'repair_order')
    serializer_class = StockMovementSerializer
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['movement_type', 'part']
    ordering_fields = ['movement_date']

    @action(detail=False, methods=['post'])
    def incoming(self, request):
        """Приход деталей на склад."""
        part_id = request.data.get('part_id')
        quantity = request.data.get('quantity')
        document_number = request.data.get('document_number', '')
        try:
            part = SparePart.objects.get(pk=part_id)
            qty = int(quantity)
            if qty <= 0:
                return Response({'error': 'Количество должно быть положительным'}, status=status.HTTP_400_BAD_REQUEST)
            part.current_stock += qty
            part.save(update_fields=['current_stock'])
            movement = StockMovement.objects.create(
                part=part,
                quantity=qty,
                movement_type='incoming',
                document_number=document_number,
                created_by=request.user
            )
            return Response({'status': 'ok', 'movement_id': movement.id})
        except SparePart.DoesNotExist:
            return Response({'error': 'Деталь не найдена'}, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)




