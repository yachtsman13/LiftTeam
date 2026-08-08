"""
Views для LiftTeam v2.6.2.
CRUD операции, дашборд, отчёты, визуальная сетка кассетниц, печать этикеток,
импорт радиодеталей из Excel.
"""
import re
import json
import openpyxl
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib.auth import authenticate, login, logout
from django.contrib import messages
from django.db.models import Q, Sum, Count, F
from django.http import JsonResponse, HttpResponse
from django.utils import timezone
from django.core.paginator import Paginator
from django.views.decorators.http import require_POST
from django.db import transaction
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from .models import (
    Client, EquipmentModel, Equipment, RepairOrder, RepairOrderEquipment,
    SparePart, StorageCell, StockMovement, RepairOrderDetail, OrderStatusHistory, Employee
)
from .forms import (
    LoginForm, ClientForm, EquipmentModelForm, EquipmentForm,
    RepairOrderForm, RepairOrderDetailForm, SparePartForm,
    StockMovementForm, StockOutgoingForm, EmployeeForm, StatusChangeForm,
    RepairOrderEquipmentFormSet, PartImportForm
)
from .utils import generate_barcode_image, generate_qr_image
from .decorators import role_required


def _send_stock_update(part):
    """Отправка обновления остатка через WebSocket."""
    if part is None:
        return
    channel_layer = get_channel_layer()
    async_to_sync(channel_layer.group_send)(
        "stock_updates",
        {
            "type": "stock_update",
            "data": {
                "part_id": part.id,
                "part_number": part.part_number,
                "name": part.name,
                "current_stock": part.current_stock,
                "min_stock": part.min_stock,
                "is_below_min": part.is_below_min_stock(),
            }
        }
    )


# ==================== АУТЕНТИФИКАЦИЯ ====================

def login_view(request):
    if request.user.is_authenticated:
        return redirect('dashboard')
    if request.method == 'POST':
        form = LoginForm(request=request, data=request.POST)
        if form.is_valid():
            user = form.get_user()
            login(request, user)
            return redirect('dashboard')
        else:
            messages.error(request, 'Неверный логин или пароль')
    else:
        form = LoginForm()
    return render(request, 'core/login.html', {'form': form})


def logout_view(request):
    logout(request)
    return redirect('login')


# ==================== ДАШБОРД ====================

@login_required
def dashboard(request):
    """Главная страница — статистика и алерты."""
    total_orders = RepairOrder.objects.count()
    active_orders = RepairOrder.objects.exclude(status='shipped').count()
    low_stock_parts = SparePart.objects.filter(current_stock__lt=F('min_stock'))
    low_stock_count = low_stock_parts.count()
    recent_orders = RepairOrder.objects.select_related('client').prefetch_related('order_equipments__equipment__model').order_by('-date_received')[:10]

    # Статистика по статусам
    status_stats = RepairOrder.objects.values('status').annotate(count=Count('id'))
    status_stats_dict = {item['status']: item['count'] for item in status_stats}

    # Должники (не оплаченные заказы)
    debtors = RepairOrder.objects.filter(
        payment_status__in=['unpaid', 'partially_paid']
    ).select_related('client')
    total_debt = sum(order.total_repair_cost for order in debtors)

    context = {
        'total_orders': total_orders,
        'active_orders': active_orders,
        'low_stock_count': low_stock_count,
        'low_stock_parts': low_stock_parts[:20],
        'recent_orders': recent_orders,
        'status_stats': status_stats_dict,
        'debtors': debtors[:10],
        'total_debt': total_debt,
        'now': timezone.now(),
    }
    return render(request, 'core/dashboard.html', context)


# ==================== КЛИЕНТЫ ====================

@login_required
def client_list(request):
    search = request.GET.get('q', '')
    clients = Client.objects.all()
    if search:
        clients = clients.filter(Q(name__icontains=search) | Q(inn__icontains=search))
    paginator = Paginator(clients.order_by('name'), 25)
    page = request.GET.get('page')
    return render(request, 'core/clients/list.html', {
        'clients': paginator.get_page(page),
        'search': search
    })


@login_required
def client_create(request):
    if request.method == 'POST':
        form = ClientForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, 'Заказчик добавлен')
            return redirect('client_list')
    else:
        form = ClientForm()
    return render(request, 'core/clients/form.html', {'form': form, 'title': 'Новый заказчик'})


@login_required
def client_edit(request, pk):
    client = get_object_or_404(Client, pk=pk)
    if request.method == 'POST':
        form = ClientForm(request.POST, instance=client)
        if form.is_valid():
            form.save()
            messages.success(request, 'Заказчик обновлён')
            return redirect('client_list')
    else:
        form = ClientForm(instance=client)
    return render(request, 'core/clients/form.html', {'form': form, 'title': 'Редактирование заказчика', 'client': client})


@role_required('repair_manager')
def client_delete(request, pk):
    client = get_object_or_404(Client, pk=pk)
    if request.method == 'POST':
        client.delete()
        messages.success(request, 'Заказчик удалён')
        return redirect('client_list')
    return render(request, 'core/clients/delete.html', {'client': client})


# ==================== ОБОРУДОВАНИЕ ====================

@login_required
def equipment_list(request):
    search = request.GET.get('q', '')
    equipment = Equipment.objects.select_related('model', 'current_client')
    if search:
        equipment = equipment.filter(Q(serial_number__icontains=search) | Q(model__name__icontains=search))
    paginator = Paginator(equipment.order_by('serial_number'), 25)
    page = request.GET.get('page')
    return render(request, 'core/equipment/list.html', {
        'equipment': paginator.get_page(page),
        'search': search
    })


@login_required
def equipment_create(request):
    if request.method == 'POST':
        form = EquipmentForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, 'Оборудование добавлено')
            return redirect('equipment_list')
    else:
        form = EquipmentForm()
    return render(request, 'core/equipment/form.html', {'form': form, 'title': 'Новое оборудование'})


@login_required
def equipment_edit(request, pk):
    eq = get_object_or_404(Equipment, pk=pk)
    if request.method == 'POST':
        form = EquipmentForm(request.POST, instance=eq)
        if form.is_valid():
            form.save()
            messages.success(request, 'Оборудование обновлено')
            return redirect('equipment_list')
    else:
        form = EquipmentForm(instance=eq)
    return render(request, 'core/equipment/form.html', {'form': form, 'title': 'Редактирование оборудования', 'equipment': eq})


@role_required('repair_manager', 'warehouse')
def equipment_delete(request, pk):
    eq = get_object_or_404(Equipment, pk=pk)
    if request.method == 'POST':
        eq.delete()
        messages.success(request, 'Оборудование удалено')
        return redirect('equipment_list')
    return render(request, 'core/equipment/delete.html', {'equipment': eq})


# ==================== МОДЕЛИ ОБОРУДОВАНИЯ ====================

@login_required
def equipment_model_list(request):
    models = EquipmentModel.objects.all().order_by('name')
    return render(request, 'core/equipment/model_list.html', {'models': models})


@login_required
def equipment_model_create(request):
    if request.method == 'POST':
        form = EquipmentModelForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, 'Модель добавлена')
            return redirect('equipment_model_list')
    else:
        form = EquipmentModelForm()
    return render(request, 'core/equipment/model_form.html', {'form': form, 'title': 'Новая модель оборудования'})


@login_required
def equipment_model_edit(request, pk):
    model = get_object_or_404(EquipmentModel, pk=pk)
    if request.method == 'POST':
        form = EquipmentModelForm(request.POST, instance=model)
        if form.is_valid():
            form.save()
            messages.success(request, 'Модель обновлена')
            return redirect('equipment_model_list')
    else:
        form = EquipmentModelForm(instance=model)
    return render(request, 'core/equipment/model_form.html', {'form': form, 'title': 'Редактирование модели', 'model': model})


@role_required('repair_manager', 'warehouse')
def equipment_model_delete(request, pk):
    model = get_object_or_404(EquipmentModel, pk=pk)
    if request.method == 'POST':
        model.delete()
        messages.success(request, 'Модель удалена')
        return redirect('equipment_model_list')
    return render(request, 'core/equipment/delete.html', {'equipment': model, 'is_model': True})


# ==================== ЗАКАЗЫ НА РЕМОНТ ====================

@login_required
def repair_order_list(request):
    search = request.GET.get('q', '')
    status_filter = request.GET.get('status', '')
    orders = RepairOrder.objects.select_related('client').prefetch_related('order_equipments__equipment__model')
    if search:
        orders = orders.filter(
            Q(order_number__icontains=search) |
            Q(client__name__icontains=search) |
            Q(order_equipments__equipment__serial_number__icontains=search)
        ).distinct()
    if status_filter:
        orders = orders.filter(status=status_filter)
    paginator = Paginator(orders.order_by('-date_received'), 25)
    page = request.GET.get('page')
    return render(request, 'core/repair_orders/list.html', {
        'orders': paginator.get_page(page),
        'search': search,
        'status_filter': status_filter,
        'status_choices': RepairOrder.STATUS_CHOICES,
    })


@login_required
def repair_order_create(request):
    if request.method == 'POST':
        form = RepairOrderForm(request.POST)
        formset = RepairOrderEquipmentFormSet(request.POST, prefix='equipments')
        if form.is_valid() and formset.is_valid():
            order = form.save()
            formset.instance = order
            formset.save()
            messages.success(request, f'Заказ {order.order_number} создан')
            return redirect('repair_order_detail', pk=order.pk)
        # Без этого сообщения неудачное сохранение выглядело как успешное:
        # страница просто перезагружалась, а ошибки полей, которые шаблон
        # не выводил, оставались невидимыми — заказ молча не создавался
        messages.error(request, 'Заказ не сохранён: проверьте отмеченные поля')
    else:
        form = RepairOrderForm()
        formset = RepairOrderEquipmentFormSet(prefix='equipments')
    return render(request, 'core/repair_orders/form.html', {
        'form': form,
        'formset': formset,
        'title': 'Новый заказ на ремонт'
    })


@login_required
def repair_order_detail(request, pk):
    order = get_object_or_404(
        RepairOrder.objects.prefetch_related('order_equipments__equipment__model', 'client'),
        pk=pk
    )
    details = order.details.select_related('part')
    history = order.status_history.select_related('changed_by').order_by('-changed_at')
    order_equipments = order.order_equipments.select_related('equipment__model').order_by('id')
    detail_form = RepairOrderDetailForm()
    status_form = StatusChangeForm()
    return render(request, 'core/repair_orders/detail.html', {
        'order': order,
        'details': details,
        'history': history,
        'order_equipments': order_equipments,
        'detail_form': detail_form,
        'status_form': status_form,
    })


@login_required
def repair_order_edit(request, pk):
    order = get_object_or_404(RepairOrder, pk=pk)
    old_payment_status = order.payment_status
    if request.method == 'POST':
        form = RepairOrderForm(request.POST, instance=order)
        formset = RepairOrderEquipmentFormSet(request.POST, instance=order, prefix='equipments')
        if form.is_valid() and formset.is_valid():
            saved_order = form.save()
            formset.save()
            # Логируем изменение статуса оплаты, если он изменился
            if saved_order.payment_status != old_payment_status:
                OrderStatusHistory.objects.create(
                    order=saved_order,
                    payment_status=saved_order.payment_status,
                    changed_by=request.user,
                    notes=f'Статус оплаты изменён с "{dict(RepairOrder.PAYMENT_STATUS_CHOICES).get(old_payment_status)}" при редактировании заказа'
                )
            messages.success(request, 'Заказ обновлён')
            return redirect('repair_order_detail', pk=order.pk)
        messages.error(request, 'Изменения не сохранены: проверьте отмеченные поля')
    else:
        form = RepairOrderForm(instance=order)
        formset = RepairOrderEquipmentFormSet(instance=order, prefix='equipments')
    return render(request, 'core/repair_orders/form.html', {
        'form': form,
        'formset': formset,
        'title': f'Редактирование заказа {order.order_number}',
        'order': order
    })


@login_required
@require_POST
def repair_order_add_detail(request, pk):
    """Добавление детали в заказ со списанием со склада (явная транзакция)."""
    order = get_object_or_404(RepairOrder, pk=pk)
    form = RepairOrderDetailForm(request.POST)
    if form.is_valid():
        detail = form.save(commit=False)
        detail.repair_order = order
        part = detail.part

        if part.current_stock < detail.quantity_used:
            messages.warning(request,
                f'Внимание: недостаточно {part.name} на складе! Текущий остаток: {part.current_stock}. '
                f'Будет списано с отрицательным остатком.')

        with transaction.atomic():
            detail.save()
            # Явное списание со склада
            part.current_stock -= detail.quantity_used
            part.save(update_fields=['current_stock'])
            # Создание записи движения
            StockMovement.objects.create(
                part=part,
                quantity=detail.quantity_used,
                movement_type='outgoing',
                repair_order=order,
                notes=f'Списано по заказу {order.order_number}',
                created_by=request.user
            )
            # Создание записи истории статуса
            OrderStatusHistory.objects.create(
                order=order,
                status=order.status,
                changed_by=request.user,
                notes=f'Добавлена деталь {part.name} x{detail.quantity_used}'
            )

        messages.success(request, f'Деталь {part.name} добавлена в заказ')
    else:
        messages.error(request, 'Ошибка при добавлении детали')
    return redirect('repair_order_detail', pk=pk)


@login_required
@require_POST
def repair_order_change_status(request, pk):
    """Изменение статуса заказа с логированием."""
    order = get_object_or_404(RepairOrder, pk=pk)
    form = StatusChangeForm(request.POST)
    if form.is_valid():
        new_status = form.cleaned_data['new_status']
        notes = form.cleaned_data.get('notes', '')
        old_status = order.status

        if old_status == new_status:
            messages.info(request, 'Статус не изменился')
            return redirect('repair_order_detail', pk=pk)

        order.status = new_status
        if new_status == 'shipped':
            order.shipping_date = timezone.now()
            order.date_completed = timezone.now()
        else:
            # Сбрасываем даты, если заказ вернули из отгруженного
            order.date_completed = None
            if old_status == 'shipped':
                order.shipping_date = None
        order.save()

        # Создаём новую запись истории (а не обновляем старую)
        OrderStatusHistory.objects.create(
            order=order,
            status=new_status,
            changed_by=request.user,
            notes=notes or f'Статус изменён с "{dict(RepairOrder.STATUS_CHOICES).get(old_status)}"'
        )

        messages.success(request, f'Статус изменён на «{order.get_status_display()}»')
    return redirect('repair_order_detail', pk=pk)


@role_required('repair_manager', 'accountant')
@require_POST
def repair_order_change_payment_status(request, pk):
    """Изменение статуса оплаты заказа с логированием."""
    order = get_object_or_404(RepairOrder, pk=pk)
    new_payment_status = request.POST.get('payment_status')
    notes = request.POST.get('notes', '')

    if new_payment_status not in dict(RepairOrder.PAYMENT_STATUS_CHOICES):
        messages.error(request, 'Недопустимый статус оплаты')
        return redirect('repair_order_detail', pk=pk)

    old_payment_status = order.payment_status
    if old_payment_status == new_payment_status:
        messages.info(request, 'Статус оплаты не изменился')
        return redirect('repair_order_detail', pk=pk)

    order.payment_status = new_payment_status
    order.save()

    # Логируем изменение статуса оплаты
    OrderStatusHistory.objects.create(
        order=order,
        payment_status=new_payment_status,
        changed_by=request.user,
        notes=notes or f'Статус оплаты изменён с "{dict(RepairOrder.PAYMENT_STATUS_CHOICES).get(old_payment_status)}"'
    )

    messages.success(request, f'Статус оплаты изменён на «{order.get_payment_status_display()}»')
    return redirect('repair_order_detail', pk=pk)


@role_required('repair_manager')
def repair_order_delete(request, pk):
    order = get_object_or_404(RepairOrder, pk=pk)
    if request.method == 'POST':
        order.delete()
        messages.success(request, 'Заказ удалён')
        return redirect('repair_order_list')
    return render(request, 'core/repair_orders/delete.html', {'order': order})


# ==================== ДЕТАЛИ / ЗАПЧАСТИ ====================

def _filter_parts(request):
    """Общая фильтрация деталей по параметрам GET-запроса (используется списком и экспортом)."""
    search = request.GET.get('q', '')
    component_type = request.GET.get('component_type', '')

    voltage_from = request.GET.get('voltage_from', '')
    voltage_to = request.GET.get('voltage_to', '')
    current_from = request.GET.get('current_from', '')
    current_to = request.GET.get('current_to', '')
    resistance_from = request.GET.get('resistance_from', '')
    resistance_to = request.GET.get('resistance_to', '')
    capacitance_from = request.GET.get('capacitance_from', '')
    capacitance_to = request.GET.get('capacitance_to', '')

    stock_from = request.GET.get('stock_from', '')
    stock_to = request.GET.get('stock_to', '')
    below_min = request.GET.get('below_min', '')

    parts = SparePart.objects.all()
    if search:
        parts = parts.filter(
            Q(part_number__icontains=search) |
            Q(name__icontains=search) |
            Q(component_type__icontains=search) |
            Q(description__icontains=search)
        )
    if component_type:
        parts = parts.filter(component_type=component_type)

    if voltage_from:
        parts = parts.filter(voltage__gte=float(voltage_from))
    if voltage_to:
        parts = parts.filter(voltage__lte=float(voltage_to))
    if current_from:
        parts = parts.filter(current__gte=float(current_from))
    if current_to:
        parts = parts.filter(current__lte=float(current_to))
    if resistance_from:
        parts = parts.filter(resistance__gte=float(resistance_from))
    if resistance_to:
        parts = parts.filter(resistance__lte=float(resistance_to))
    if capacitance_from:
        parts = parts.filter(capacitance__gte=float(capacitance_from))
    if capacitance_to:
        parts = parts.filter(capacitance__lte=float(capacitance_to))

    if stock_from:
        parts = parts.filter(current_stock__gte=int(stock_from))
    if stock_to:
        parts = parts.filter(current_stock__lte=int(stock_to))
    if below_min:
        parts = parts.filter(current_stock__lt=F('min_stock'))

    return parts, {
        'search': search,
        'component_type': component_type,
        'voltage_from': voltage_from,
        'voltage_to': voltage_to,
        'current_from': current_from,
        'current_to': current_to,
        'resistance_from': resistance_from,
        'resistance_to': resistance_to,
        'capacitance_from': capacitance_from,
        'capacitance_to': capacitance_to,
        'stock_from': stock_from,
        'stock_to': stock_to,
        'below_min': below_min,
    }


@login_required
def part_list(request):
    parts, filter_context = _filter_parts(request)

    component_types = SparePart.objects.exclude(component_type='').values_list('component_type', flat=True).distinct().order_by('component_type')

    paginator = Paginator(parts.order_by('part_number'), 25)
    page = request.GET.get('page')
    return render(request, 'core/parts/list.html', {
        'parts': paginator.get_page(page),
        'component_types': component_types,
        **filter_context,
    })


@login_required
def part_export(request):
    """Экспорт радиодеталей в Excel (с учётом текущих фильтров списка)."""
    parts, _ = _filter_parts(request)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Радиодетали'

    headers = [
        'part_number', 'name', 'component_type',
        'resistance', 'resistance_unit',
        'power', 'power_unit',
        'voltage', 'voltage_unit',
        'current', 'current_unit',
        'capacitance', 'capacitance_unit',
        'min_stock', 'current_stock', 'lead_time_days',
        'preferred_supplier', 'description',
    ]
    ws.append(headers)

    for part in parts.order_by('part_number'):
        ws.append([getattr(part, field) for field in headers])

    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = 'attachment; filename="spare_parts.xlsx"'
    wb.save(response)
    return response


@login_required
def part_detail(request, pk):
    part = get_object_or_404(SparePart.objects.prefetch_related('movements'), pk=pk)
    movements = part.movements.order_by('-movement_date')[:50]
    # Все ячейки, кроме уже назначенной этой детали — деталь можно добавить
    # и в занятую другой деталью ячейку (в одной ячейке может быть несколько деталей)
    available_cells = StorageCell.objects.prefetch_related('parts').exclude(
        parts=part
    ).order_by('cabinet_number', 'row_number', 'cell_row')
    stock_form = StockMovementForm()
    return render(request, 'core/parts/detail.html', {
        'part': part,
        'movements': movements,
        'available_cells': available_cells,
        'stock_form': stock_form,
    })


@login_required
def part_create(request):
    if request.method == 'POST':
        form = SparePartForm(request.POST)
        if form.is_valid():
            part = form.save()
            messages.success(request, f'Деталь {part.part_number} добавлена')
            return redirect('part_detail', pk=part.pk)
    else:
        form = SparePartForm()
    return render(request, 'core/parts/form.html', {'form': form, 'title': 'Новая деталь'})


@login_required
def part_edit(request, pk):
    part = get_object_or_404(SparePart, pk=pk)
    if request.method == 'POST':
        form = SparePartForm(request.POST, instance=part)
        if form.is_valid():
            form.save()
            messages.success(request, 'Деталь обновлена')
            return redirect('part_detail', pk=part.pk)
    else:
        form = SparePartForm(instance=part)
    return render(request, 'core/parts/form.html', {'form': form, 'title': 'Редактирование детали', 'part': part})


@role_required('warehouse')
def part_delete(request, pk):
    part = get_object_or_404(SparePart, pk=pk)
    if request.method == 'POST':
        part.delete()
        messages.success(request, 'Деталь удалена')
        return redirect('part_list')
    return render(request, 'core/parts/delete.html', {'part': part})


@login_required
@require_POST
def part_stock_incoming(request, pk):
    """Приход детали на склад."""
    part = get_object_or_404(SparePart, pk=pk)
    form = StockMovementForm(request.POST)
    if form.is_valid():
        with transaction.atomic():
            movement = form.save(commit=False)
            movement.part = part
            movement.movement_type = 'incoming'
            movement.created_by = request.user
            part.current_stock += movement.quantity
            part.save(update_fields=['current_stock'])
            movement.save()
        messages.success(request, f'Приход +{movement.quantity} {part.name} оформлен')
    else:
        messages.error(request, 'Ошибка при оформлении прихода')
    return redirect('part_detail', pk=pk)


@login_required
@require_POST
def part_stock_outgoing(request, pk):
    """Ручное списание деталей (фактический расход / инвентаризация)."""
    part = get_object_or_404(SparePart, pk=pk)
    form = StockOutgoingForm(request.POST)
    if form.is_valid():
        qty = form.cleaned_data['quantity']
        reason = form.cleaned_data['reason']
        notes = form.cleaned_data.get('notes', '')
        doc_num = form.cleaned_data.get('document_number', '')

        if part.current_stock < qty:
            messages.warning(request,
                f'Внимание: списываем {qty} {part.name}, но на складе только {part.current_stock}!')

        with transaction.atomic():
            part.current_stock -= qty
            part.save(update_fields=['current_stock'])
            StockMovement.objects.create(
                part=part,
                quantity=qty,
                movement_type='outgoing',
                document_number=doc_num,
                notes=f'{notes} (причина: {dict(form.fields["reason"].choices).get(reason)})',
                created_by=request.user
            )
        messages.success(request, f'Списано {qty} {part.name} ({reason})')
    else:
        messages.error(request, 'Ошибка при оформлении списания')
    return redirect('part_detail', pk=pk)


@login_required
@require_POST
def part_assign_cell(request, pk):
    """Назначение детали на ячейку хранения (ячейка может содержать несколько деталей)."""
    part = get_object_or_404(SparePart, pk=pk)
    cell_id = request.POST.get('cell_id')
    if cell_id:
        cell = get_object_or_404(StorageCell, pk=cell_id)
        with transaction.atomic():
            # У детали может быть только одна ячейка — снимаем с предыдущей, если была
            for old_cell in part.storage_cells.exclude(pk=cell.pk):
                old_cell.parts.remove(part)
            cell.parts.add(part)
        messages.success(request, f'Деталь назначена на ячейку {cell.address}')
    return redirect('part_detail', pk=pk)


@login_required
def part_import(request):
    """Импорт радиодеталей из Excel."""
    if request.method == 'POST':
        form = PartImportForm(request.POST, request.FILES)
        if form.is_valid():
            file = request.FILES['file']
            update_existing = form.cleaned_data['update_existing']
            try:
                wb = openpyxl.load_workbook(file)
                ws = wb.active
                headers = [str(cell.value).strip() if cell.value else '' for cell in ws[1]]

                required = ['part_number', 'name', 'component_type']
                missing = [h for h in required if h not in headers]
                if missing:
                    messages.error(request, f'Отсутствуют обязательные колонки: {", ".join(missing)}')
                    return redirect('part_import')

                created_count = 0
                updated_count = 0
                error_count = 0
                error_details = []

                for row in ws.iter_rows(min_row=2, values_only=True):
                    data = dict(zip(headers, row))
                    part_number = str(data.get('part_number', '')).strip()
                    if not part_number:
                        continue

                    # --- Вспомогательная функция для парсинга чисел ---
                    def _parse_decimal(val):
                        """Парсит значение в Decimal или None."""
                        if val is None or val == '':
                            return None
                        try:
                            # Если это уже число (int/float из Excel)
                            if isinstance(val, (int, float)):
                                return val
                            # Если строка — чистим и парсим
                            s = str(val).strip().replace(',', '.')
                            # Убираем единицы измерения из строки если они есть
                            s = re.sub(r'[^0-9.\-]', '', s)
                            if s == '' or s == '.':
                                return None
                            return float(s)
                        except (ValueError, TypeError):
                            return None

                    def _parse_int(val, default):
                        """Парсит значение в int или возвращает default."""
                        if val is None or val == '':
                            return default
                        try:
                            if isinstance(val, (int, float)):
                                return int(val)
                            return int(str(val).strip())
                        except (ValueError, TypeError):
                            return default

                    def _get_str(val):
                        """Возвращает строку или пустую строку."""
                        if val is None:
                            return ''
                        return str(val).strip()

                    # --- Формируем defaults ---
                    defaults = {
                        'name': _get_str(data.get('name')) or part_number,
                        'component_type': _get_str(data.get('component_type')),
                        'voltage': _parse_decimal(data.get('voltage')),
                        'voltage_unit': _get_str(data.get('voltage_unit')) or 'В',
                        'current': _parse_decimal(data.get('current')),
                        'current_unit': _get_str(data.get('current_unit')) or 'А',
                        'power': _parse_decimal(data.get('power')),
                        'power_unit': _get_str(data.get('power_unit')) or 'Вт',
                        'resistance': _parse_decimal(data.get('resistance')),
                        'resistance_unit': _get_str(data.get('resistance_unit')) or 'Ом',
                        'capacitance': _parse_decimal(data.get('capacitance')),
                        'capacitance_unit': _get_str(data.get('capacitance_unit')) or 'Ф',
                        'min_stock': _parse_int(data.get('min_stock'), 5),
                        'current_stock': _parse_int(data.get('current_stock'), 0),
                        'lead_time_days': _parse_int(data.get('lead_time_days'), 14),
                        'description': _get_str(data.get('description')),
                    }

                    # Убираем unit-поля из defaults если они пустые (чтобы не перезаписать существующие)
                    for unit_field in ['voltage_unit', 'current_unit', 'power_unit', 'resistance_unit', 'capacitance_unit']:
                        if not defaults[unit_field]:
                            del defaults[unit_field]

                    try:
                        if update_existing:
                            part, created = SparePart.objects.update_or_create(
                                part_number=part_number,
                                defaults=defaults
                            )
                        else:
                            part, created = SparePart.objects.get_or_create(
                                part_number=part_number,
                                defaults=defaults
                            )
                            if not created:
                                continue
                        if created:
                            created_count += 1
                        else:
                            updated_count += 1
                    except Exception as e:
                        error_count += 1
                        error_details.append(f'{part_number}: {str(e)[:100]}')
                        if error_count <= 5:
                            import traceback
                            traceback.print_exc()

                msg = f'Импорт завершён: создано {created_count}, обновлено {updated_count}, ошибок {error_count}'
                if error_details and error_count <= 10:
                    msg += ' | ' + '; '.join(error_details[:5])
                messages.success(request, msg)
                return redirect('part_list')
            except Exception as e:
                messages.error(request, f'Ошибка импорта: {e}')
    else:
        form = PartImportForm()
    return render(request, 'core/parts/import.html', {'form': form})

@login_required
def storage_cell_grid(request):
    """Визуальная сетка кассетниц. Одна ячейка может содержать несколько деталей."""
    cabinet = int(request.GET.get('cabinet', 1))
    selected_part_id = request.GET.get('selected_part', '')
    move_from = request.GET.get('move_from', '')
    selected_part = None
    if selected_part_id:
        selected_part = get_object_or_404(SparePart, pk=selected_part_id)

    cells = StorageCell.objects.filter(cabinet_number=cabinet).prefetch_related('parts')
    cell_map = {}
    cells_data = {}
    for cell in cells:
        cell_map[(cell.row_number, cell.cell_row)] = cell
        cells_data[cell.pk] = {
            'address': cell.address,
            'parts': [
                {
                    'id': p.pk,
                    'part_number': p.part_number,
                    'name': p.name,
                    'component_type': p.component_type,
                    'stock': p.current_stock,
                    'min_stock': p.min_stock,
                }
                for p in cell.parts.all()
            ],
        }

    grid = []
    for row in range(1, 9):
        grid_row = []
        for col in range(1, 9):
            cell = cell_map.get((row, col))
            if cell:
                status = cell.get_status()
                cell_parts = cells_data[cell.pk]['parts']
                if selected_part and any(p['id'] == selected_part.pk for p in cell_parts):
                    status = 'selected'
                grid_row.append({
                    'exists': True,
                    'cell': cell,
                    'status': status,
                    'part_count': len(cell_parts),
                })
            else:
                grid_row.append({'exists': False})
        grid.append(grid_row)

    parts = SparePart.objects.all().order_by('part_number')
    all_parts_data = [
        {'id': p.pk, 'label': f'{p.part_number} — {p.name}'}
        for p in parts
    ]

    return render(request, 'core/storage_cells/grid.html', {
        'grid': grid,
        'cabinet': cabinet,
        'cabinet_range': range(1, 13),
        'row_range': range(1, 9),
        'col_range': range(1, 9),
        'selected_part': selected_part,
        'parts': parts,
        'move_from': move_from,
        'cells_data_json': json.dumps(cells_data, ensure_ascii=False),
        'all_parts_json': json.dumps(all_parts_data, ensure_ascii=False),
    })


@login_required
@require_POST
def storage_cell_move(request):
    """Перемещение конкретной детали между ячейками (AJAX)."""
    from_cell_id = request.POST.get('from_cell')
    to_cell_id = request.POST.get('to_cell')
    part_id = request.POST.get('part_id')

    if not part_id:
        return JsonResponse({'error': 'Не указана деталь'}, status=400)

    try:
        with transaction.atomic():
            part = SparePart.objects.get(pk=part_id)

            if from_cell_id:
                from_cell = StorageCell.objects.select_for_update().get(pk=from_cell_id)
                from_cell.parts.remove(part)

            to_cell = None
            if to_cell_id:
                to_cell = StorageCell.objects.select_for_update().get(pk=to_cell_id)
                to_cell.parts.add(part)

            _send_stock_update(part)

            return JsonResponse({'success': True, 'message': 'Перемещение выполнено', 'address': to_cell.address if to_cell else None})
    except (SparePart.DoesNotExist, StorageCell.DoesNotExist):
        return JsonResponse({'error': 'Деталь или ячейка не найдена'}, status=400)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)


@login_required
@require_POST
def storage_cell_add_part(request, pk):
    """Добавление детали в ячейку (одна из нескольких деталей, которые может хранить ячейка)."""
    cell = get_object_or_404(StorageCell, pk=pk)
    part_id = request.POST.get('part_id')
    part = get_object_or_404(SparePart, pk=part_id)

    with transaction.atomic():
        # У детали может быть только одна ячейка — снимаем с предыдущей, если была
        for old_cell in part.storage_cells.exclude(pk=cell.pk):
            old_cell.parts.remove(part)
        cell.parts.add(part)

    return JsonResponse({
        'success': True,
        'message': f'Деталь {part.part_number} добавлена в ячейку {cell.address}',
        'part': {
            'id': part.pk,
            'part_number': part.part_number,
            'name': part.name,
            'component_type': part.component_type,
            'stock': part.current_stock,
            'min_stock': part.min_stock,
        },
    })


@login_required
@require_POST
def storage_cell_remove_part(request, pk):
    """Удаление детали из ячейки (без удаления самой детали)."""
    cell = get_object_or_404(StorageCell, pk=pk)
    part_id = request.POST.get('part_id')
    part = get_object_or_404(SparePart, pk=part_id)
    cell.parts.remove(part)
    return JsonResponse({'success': True, 'message': f'Деталь {part.part_number} удалена из ячейки {cell.address}'})


@login_required
def storage_cell_label(request, pk):
    """Печать этикетки ячейки.
    Если все детали в ячейке одного типа (набор номиналов одного типа —
    резисторы, конденсаторы и т.д.), тип указывается один раз, а значения
    характеристик перечисляются списком. Иначе — построчно название + характеристика."""
    cell = get_object_or_404(StorageCell.objects.prefetch_related('parts'), pk=pk)
    parts = list(cell.parts.all())
    qr_img = generate_qr_image(cell.qr_data)

    component_types = {p.component_type for p in parts if p.component_type}
    grouped = len(parts) > 1 and len(component_types) == 1
    group_type = None
    group_values = ''
    if grouped:
        group_type = parts[0].component_type
        group_values = ', '.join(p.specs_display for p in parts if p.specs_display)

    return render(request, 'core/storage_cells/label.html', {
        'cell': cell,
        'cell_parts': parts,
        'qr_img': qr_img,
        'grouped': grouped,
        'group_type': group_type,
        'group_values': group_values,
    })


@login_required
def equipment_label(request, pk):
    """Печать этикетки единицы оборудования (43x25 мм)."""
    equipment = get_object_or_404(Equipment.objects.select_related('model'), pk=pk)

    # Данные для QR-кода
    qr_data_dict = {
        'id': equipment.id,
        'model': equipment.model.name,
        'serial': equipment.serial_number,
    }
    import json
    qr_data = json.dumps(qr_data_dict, ensure_ascii=False)

    barcode_img = generate_barcode_image(equipment.serial_number)
    qr_img = generate_qr_image(qr_data)

    return render(request, 'core/equipment/label.html', {
        'equipment': equipment,
        'barcode_img': barcode_img,
        'qr_img': qr_img,
    })


@login_required
def repair_order_equipment_label(request, order_pk, roe_pk):
    """Печать этикетки оборудования в контексте заказа (43x25 мм).
    Номер позиции — порядковый номер оборудования в списке заказа."""
    order = get_object_or_404(RepairOrder, pk=order_pk)
    roe_list = list(
        order.order_equipments.select_related('equipment__model').order_by('id')
    )
    roe = get_object_or_404(RepairOrderEquipment, pk=roe_pk, repair_order=order)
    position = next(i for i, r in enumerate(roe_list, start=1) if r.pk == roe.pk)

    # Компактная строка вместо JSON: только заглавные, цифры и '-', '/'.
    # QR кодирует такой набор вдвое плотнее, чем произвольный текст, и код
    # укладывается в минимальный размер 21x21 модуль — при печати 9 мм это
    # даёт крупные модули, что решает, считается ли этикетка после месяца
    # на складе. Заодно строка читается человеком, если QR не сканируется.
    qr_img = generate_qr_image(f'{order.order_number}/{position}')

    return render(request, 'core/repair_orders/equipment_label.html', {
        'order': order,
        'roe': roe,
        'position': position,
        'total': len(roe_list),
        'qr_img': qr_img,
    })


# ==================== ОТЧЁТЫ ====================

@login_required
def reports(request):
    return render(request, 'core/reports/index.html')


@login_required
def report_purchase_plan(request):
    """План закупок — детали ниже минимального остатка."""
    parts = SparePart.objects.filter(current_stock__lt=F('min_stock')).order_by('part_number')
    return render(request, 'core/reports/purchase_plan.html', {'parts': parts})


@login_required
def report_stock_movements(request):
    """Журнал движений запчастей."""
    movements = StockMovement.objects.select_related('part', 'repair_order').order_by('-movement_date')

    part_id = request.GET.get('part')
    movement_type = request.GET.get('type')
    date_from = request.GET.get('date_from')
    date_to = request.GET.get('date_to')

    if part_id:
        movements = movements.filter(part_id=part_id)
    if movement_type:
        movements = movements.filter(movement_type=movement_type)
    if date_from:
        movements = movements.filter(movement_date__date__gte=date_from)
    if date_to:
        movements = movements.filter(movement_date__date__lte=date_to)

    paginator = Paginator(movements, 50)
    page = request.GET.get('page')

    parts_list = SparePart.objects.all().order_by('part_number')
    return render(request, 'core/reports/stock_movements.html', {
        'movements': paginator.get_page(page),
        'parts_list': parts_list,
    })


@login_required
def report_debtors(request):
    """Задолженности по заказам."""
    orders = RepairOrder.objects.filter(
        payment_status__in=['unpaid', 'partially_paid']
    ).select_related('client').prefetch_related('order_equipments').order_by('-date_received')
    total_debt = sum(order.total_repair_cost for order in orders)
    return render(request, 'core/reports/debtors.html', {
        'orders': orders,
        'total_debt': total_debt,
    })


# ==================== AJAX: Создание из формы заказа ====================

@login_required
def ajax_equipment_model_list(request):
    """AJAX-получение списка моделей оборудования (JSON)."""
    models = EquipmentModel.objects.all().order_by('name').values('id', 'name')
    return JsonResponse({'success': True, 'models': list(models)})


@login_required
@require_POST
def ajax_equipment_model_create(request):
    """AJAX-создание модели оборудования из формы заказа."""
    name = request.POST.get('name', '').strip()
    if not name:
        return JsonResponse({'success': False, 'error': 'Название модели обязательно'})

    model, created = EquipmentModel.objects.get_or_create(name=name)
    if not created:
        return JsonResponse({'success': False, 'error': 'Модель с таким названием уже существует'})

    return JsonResponse({
        'success': True,
        'id': model.id,
        'name': model.name,
        'message': f'Модель "{model.name}" создана'
    })


@login_required
@require_POST
def ajax_equipment_create(request):
    """AJAX-создание оборудования из формы заказа."""
    model_id = request.POST.get('model_id')
    serial_number = request.POST.get('serial_number', '').strip()

    if not model_id:
        return JsonResponse({'success': False, 'error': 'Выберите модель'})
    if not serial_number:
        return JsonResponse({'success': False, 'error': 'Укажите серийный номер'})

    try:
        model = EquipmentModel.objects.get(pk=model_id)
    except EquipmentModel.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Модель не найдена'})

    if Equipment.objects.filter(serial_number=serial_number).exists():
        return JsonResponse({'success': False, 'error': 'Оборудование с таким серийным номером уже существует'})

    equipment = Equipment.objects.create(model=model, serial_number=serial_number)

    return JsonResponse({
        'success': True,
        'id': equipment.id,
        'name': str(equipment),
        'message': f'Оборудование "{equipment}" создано'
    })


@login_required
@require_POST
def ajax_client_create(request):
    """AJAX-создание заказчика из формы заказа."""
    name = request.POST.get('name', '').strip()
    inn = request.POST.get('inn', '').strip()
    phone = request.POST.get('phone', '').strip()
    email = request.POST.get('email', '').strip()
    contact_person = request.POST.get('contact_person', '').strip()

    if not name:
        return JsonResponse({'success': False, 'error': 'Название заказчика обязательно'})

    if Client.objects.filter(name=name).exists():
        return JsonResponse({'success': False, 'error': 'Заказчик с таким названием уже существует'})

    client = Client.objects.create(
        name=name,
        inn=inn,
        phone=phone,
        email=email,
        contact_person=contact_person
    )

    return JsonResponse({
        'success': True,
        'id': client.id,
        'name': client.name,
        'message': f'Заказчик "{client.name}" создан'
    })


# ==================== АДМИНИСТРИРОВАНИЕ ====================

@role_required('admin')
def admin_users(request):
    users = Employee.objects.all().order_by('full_name')
    return render(request, 'core/admin/users.html', {'users': users})


@role_required('admin')
def admin_user_create(request):
    if request.method == 'POST':
        form = EmployeeForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, 'Пользователь создан')
            return redirect('admin_users')
    else:
        form = EmployeeForm()
    return render(request, 'core/admin/user_form.html', {'form': form, 'title': 'Новый пользователь'})


@role_required('admin')
def admin_user_edit(request, pk):
    user = get_object_or_404(Employee, pk=pk)
    if request.method == 'POST':
        form = EmployeeForm(request.POST, instance=user)
        if form.is_valid():
            form.save()
            messages.success(request, 'Пользователь обновлён')
            return redirect('admin_users')
    else:
        form = EmployeeForm(instance=user)
    return render(request, 'core/admin/user_form.html', {'form': form, 'title': 'Редактирование пользователя', 'user_obj': user})




