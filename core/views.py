"""
Views для LiftTeam v2.48.0.
CRUD операции, дашборд, отчёты, визуальная сетка кассетниц, печать этикеток,
импорт радиодеталей из Excel.
"""
import re
import json
from collections import Counter, defaultdict
from datetime import datetime, time, timedelta
from decimal import Decimal
import openpyxl
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib.auth import authenticate, login, logout
from django.conf import settings
from django.contrib import messages
from django.db.models import (
    Count, DecimalField, F, Max, OuterRef, Prefetch, Q, Subquery, Sum, Value,
)
from django.db.models.functions import Coalesce
from django.http import JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.core.paginator import Paginator
from django.urls import reverse
from django.views.decorators.http import require_POST
from django.db import transaction
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from .models import (
    Client, EquipmentModel, Equipment, FaultType, FaultTypePart, RepairOrder, RepairOrderEquipment,
    SparePart, StorageCell, StockMovement, StockAllocation, OrderCost, RepairOrderDetail,
    OrderStatusHistory, Employee,
    Notification, Payment, Organization, BankOperation, Cabinet,
    InventorySession, InventorySessionLine,
    add_months, warranty_cutoff, warranty_months, plural_genitive,
)
from .forms import (
    LoginForm, ClientForm, EquipmentModelForm, EquipmentForm,
    RepairOrderForm, RepairOrderDetailForm, SparePartForm,
    StockMovementForm, StockOutgoingForm, EmployeeForm, StatusChangeForm,
    RepairOrderEquipmentFormSet, PartImportForm, PaymentForm, OrganizationForm,
    DefectActForm, InvoiceSendForm, QuoteForm, QuoteLineFormSet,
    CabinetForm, MyNotificationsForm, FaultTypeForm, FaultTypePartFormSet,
)
from .utils import (
    generate_qr_image,
    build_workbook, add_sheet, xlsx_response, excel_datetime,
)
from .decorators import role_required
from . import messengers, notifications, tbank, updater


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


@login_required
def my_notifications(request):
    """Личный выбор канала внутренних оповещений — своя страница,
    без параметра в URL: всегда про текущего пользователя, доступна любой
    роли. Меняет только эти три поля — за роль и доступ отвечает
    администратор через карточку пользователя."""
    if request.method == 'POST':
        form = MyNotificationsForm(request.POST, instance=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, 'Настройки оповещений сохранены')
            return redirect('my_notifications')
    else:
        form = MyNotificationsForm(instance=request.user)
    return render(request, 'core/my_notifications.html', {'form': form})


@login_required
def presence(request):
    """Кто из сотрудников сейчас на связи.

    Видно всем вошедшим, роль не проверяем: это не надзор за подчинёнными,
    а способ не идти через лабораторию к пустому терминалу. Тем же
    декоратором закрыты склад и кассетницы; role_required оставлен
    страницам с деньгами.

    Список рисуется сразу из базы, а не ждёт сокета: отметки хранятся
    в Employee.last_seen и переживают перезапуск сервера. Дальше страницу
    обновляет ws/presence/.
    """
    employees = Employee.objects.filter(is_active=True)
    return render(request, 'core/presence.html', {
        'employees': employees,
        'timeout_seconds': settings.PRESENCE_TIMEOUT_SECONDS,
    })


# ==================== ДАШБОРД ====================

@login_required
def dashboard(request):
    """Главная страница — статистика и алерты."""
    total_orders = RepairOrder.objects.count()
    # «Завершённых» статусов два: отгружен и признан неремонтопригодным —
    # оба означают, что по заказу больше ничего не ждут
    active_orders = RepairOrder.objects.exclude(
        status__in=['shipped', 'unrepairable']
    ).count()

    # Дефицит и «ровно на минимуме» — разные вещи: первое уже в плане закупок,
    # второе означает, что следующее списание уведёт деталь в минус
    low_stock_parts = SparePart.objects.below_minimum().order_by('part_number')
    low_stock_count = low_stock_parts.count()
    at_minimum_parts = SparePart.objects.at_minimum().order_by('part_number')
    at_minimum_count = at_minimum_parts.count()
    recent_orders = RepairOrder.objects.select_related('client').prefetch_related('order_equipments__equipment__model').order_by('-date_received')[:10]

    # Разбивка по статусам — с нулями для тех, которых сейчас нет: пустая
    # колонка «Готов к отгрузке» тоже сведение, а прыгающий набор ячеек
    # читать труднее, чем постоянный
    counts = {
        item['status']: item['count']
        for item in RepairOrder.objects.values('status').annotate(count=Count('id'))
    }
    status_stats = [
        {'code': code, 'label': label, 'count': counts.get(code, 0)}
        for code, label in RepairOrder.STATUS_CHOICES
    ]

    # Должники (не оплаченные заказы)
    debtors = RepairOrder.objects.with_debt().select_related('client')
    total_debt = _total_debt(debtors)

    # Неразнесённые поступления показываем только тем, кто их разносит:
    # мастеру эта цифра ничего не говорит, а место на дашборде занимает
    unapplied_operations = (
        BankOperation.objects.filter(status='new').count()
        if request.user.role in ('accountant', 'admin') else 0
    )

    context = {
        'total_orders': total_orders,
        'active_orders': active_orders,
        'low_stock_count': low_stock_count,
        'low_stock_parts': low_stock_parts[:20],
        'at_minimum_count': at_minimum_count,
        'at_minimum_parts': at_minimum_parts[:10],
        'recent_orders': recent_orders,
        'status_stats': status_stats,
        'debtors': debtors[:10],
        'total_debt': total_debt,
        'unapplied_operations': unapplied_operations,
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
def client_export(request):
    """Список заказчиков в Excel — с числом заказов и суммой долга."""
    search = request.GET.get('q', '').strip()
    clients = Client.objects.all()
    if search:
        clients = clients.filter(Q(name__icontains=search) | Q(inn__icontains=search))
    clients = clients.order_by('name')

    orders = dict(
        RepairOrder.objects
        .filter(client__in=clients)
        .values_list('client_id')
        .annotate(count=Count('id'))
    )
    debts = dict(
        RepairOrderEquipment.objects
        .filter(
            repair_order__client__in=clients,
            repair_order__payment_status__in=['unpaid', 'partially_paid'],
        )
        .exclude(repair_order__status='unrepairable')
        .values_list('repair_order__client_id')
        .annotate(total=Sum('repair_cost'))
    )

    headers = [
        'Название', 'ИНН', 'КПП', 'Контактное лицо', 'Телефон', 'Email',
        'Заказов', 'Долг, ₽',
    ]
    rows = [
        [
            client.name, client.inn, client.kpp, client.contact_person,
            client.phone, client.email,
            orders.get(client.pk, 0), debts.get(client.pk) or 0,
        ]
        for client in clients
    ]

    wb = build_workbook('Заказчики', headers, rows)
    return xlsx_response(wb, f'Заказчики {timezone.localdate():%Y-%m-%d}.xlsx')


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

def _filter_equipment(request):
    """Отбор оборудования по параметрам GET-запроса."""
    search = request.GET.get('q', '').strip()
    warranty_filter = request.GET.get('warranty', '')

    equipment = Equipment.objects.select_related('model', 'current_client')

    if search:
        # Ищем и по нормализованному номеру: запрос «буад 1234» должен найти
        # «БУАД-1234», иначе сотрудник не увидит уже заведённую единицу
        # и заведёт её второй раз под другим написанием.
        normalized = Equipment.normalize_serial(search)
        condition = (
            Q(serial_number__icontains=search) |
            Q(model__name__icontains=search) |
            Q(current_client__name__icontains=search)
        )
        if normalized:
            condition |= Q(serial_normalized__contains=normalized)
        equipment = equipment.filter(condition)

    # Гарантия не хранится, поэтому отбираем по её признаку: заказ с этой
    # единицей завершён не раньше, чем срок гарантии назад
    cutoff = warranty_cutoff()
    if cutoff is not None and warranty_filter in ('active', 'expired'):
        covered = Q(repairorderequipment__repair_order__date_completed__gte=cutoff)
        if warranty_filter == 'active':
            equipment = equipment.filter(covered)
        else:
            equipment = equipment.exclude(covered)

    return equipment.distinct().order_by('serial_number'), {
        'search': search,
        'warranty_filter': warranty_filter,
    }


@login_required
def equipment_list(request):
    equipment, filter_context = _filter_equipment(request)

    paginator = Paginator(equipment, 25)
    page_obj = paginator.get_page(request.GET.get('page'))

    # Гарантия считается сразу по всей странице: поштучная проверка
    # означала бы запрос на каждую строку списка
    warranty_map = Equipment.warranty_map(list(page_obj))
    for item in page_obj:
        item.warranty = warranty_map.get(item.pk)

    return render(request, 'core/equipment/list.html', {
        'equipment': page_obj,
        'found_count': paginator.count,
        'filters_active': any(filter_context.values()),
        **filter_context,
    })


@login_required
def equipment_export(request):
    """Список оборудования в Excel — с учётом поиска и фильтра гарантии."""
    equipment, _ = _filter_equipment(request)
    equipment = list(equipment)
    warranty_map = Equipment.warranty_map(equipment)

    headers = [
        'Модель', 'Серийный номер', 'Текущий заказчик',
        'Гарантия до', 'Заказ по гарантии', 'Всего ремонтов',
    ]
    # Число ремонтов по всем единицам одним запросом: свойство на каждую
    # строку означало бы запрос на строку
    repairs = dict(
        RepairOrderEquipment.objects
        .filter(equipment__in=equipment)
        .values_list('equipment_id')
        .annotate(count=Count('id'))
    )

    rows = []
    for item in equipment:
        warranty = warranty_map.get(item.pk)
        rows.append([
            item.model.name,
            item.serial_number,
            item.current_client.name if item.current_client else '',
            # Только дата: час окончания гарантии никому не нужен
            excel_datetime(warranty.warranty_until).date() if warranty else '',
            warranty.repair_order.order_number if warranty else '',
            repairs.get(item.pk, 0),
        ])

    wb = build_workbook('Оборудование', headers, rows)
    return xlsx_response(wb, f'Оборудование {timezone.localdate():%Y-%m-%d}.xlsx')


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


@login_required
def equipment_history(request, pk):
    """История ремонтов одной физической единицы оборудования.

    Оборудование уже выделено в отдельную сущность с уникальным серийным
    номером, поэтому история собирается обычной связью, без поиска по строке.
    """
    equipment = get_object_or_404(
        Equipment.objects.select_related('model', 'current_client'), pk=pk
    )
    visits = list(equipment.repair_history())

    # Детали в проекте списываются на заказ целиком, а не на конкретную
    # единицу оборудования в нём. Когда в заказе одна единица — список
    # деталей относится именно к ней; когда несколько, точнее сказать
    # нельзя, и это помечается в шаблоне, а не выдаётся за точные данные.
    visit_rows = []
    for visit in visits:
        order = visit.repair_order
        units_in_order = order.order_equipments.count()
        visit_rows.append({
            'roe': visit,
            'order': order,
            'parts': order.details.select_related('part'),
            'parts_are_exact': units_in_order == 1,
            'units_in_order': units_in_order,
        })

    similar = Equipment.find_similar(equipment.serial_number, exclude_pk=equipment.pk)

    return render(request, 'core/equipment/history.html', {
        'equipment': equipment,
        'visit_rows': visit_rows,
        'visits_count': len(visit_rows),
        'similar': similar,
        'warranty': equipment.active_warranty(),
    })


@login_required
def equipment_history_export(request, pk):
    """История ремонтов одной единицы в Excel — приложить к акту или письму."""
    equipment = get_object_or_404(Equipment.objects.select_related('model'), pk=pk)
    visits = equipment.repair_history()

    headers = [
        '№ заказа', 'Дата приёма', 'Дата завершения', 'Заказчик',
        'Неисправность', 'Начальное состояние', 'Номера пломб',
        'Стоимость, ₽', 'Гарантия до', 'Детали',
    ]
    rows = []
    for visit in visits:
        order = visit.repair_order
        # Детали списываются на заказ целиком. Когда единица в заказе одна,
        # список относится к ней; иначе честно помечаем, что он общий
        units = order.order_equipments.count()
        parts = ', '.join(
            f'{detail.part.name} x{detail.quantity_used}'
            for detail in order.details.select_related('part')
        )
        if parts and units > 1:
            parts = f'{parts} (на весь заказ, единиц в нём: {units})'

        warranty_until = visit.warranty_until
        rows.append([
            order.order_number,
            excel_datetime(order.date_received),
            excel_datetime(order.date_completed),
            order.client.name,
            visit.fault_description or order.fault_description,
            visit.initial_condition,
            visit.seal_numbers,
            visit.repair_cost if visit.repair_cost is not None else '',
            excel_datetime(warranty_until).date() if warranty_until else '',
            parts,
        ])

    wb = build_workbook('История ремонтов', headers, rows)
    return xlsx_response(
        wb, f'История {equipment.serial_number} {timezone.localdate():%Y-%m-%d}.xlsx'
    )


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


def _equipment_kinds():
    """Родовые названия, которые уже вводили — подсказка в форме модели."""
    return list(
        EquipmentModel.objects.exclude(kind='')
        .order_by('kind').values_list('kind', flat=True).distinct()
    )


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
    return render(request, 'core/equipment/model_form.html', {
        'form': form, 'title': 'Новая модель оборудования', 'kinds': _equipment_kinds(),
    })


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
    return render(request, 'core/equipment/model_form.html', {
        'form': form, 'title': 'Редактирование модели', 'model': model,
        'kinds': _equipment_kinds(),
    })


@role_required('repair_manager', 'warehouse')
def equipment_model_delete(request, pk):
    model = get_object_or_404(EquipmentModel, pk=pk)
    if request.method == 'POST':
        model.delete()
        messages.success(request, 'Модель удалена')
        return redirect('equipment_model_list')
    return render(request, 'core/equipment/delete.html', {'equipment': model, 'is_model': True})


# ==================== ТИПОВЫЕ НЕИСПРАВНОСТИ ====================
# Права — как у EquipmentModel, ближайшего по смыслу справочника:
# создание и редактирование открыты любому авторизованному, удаление —
# только складу и мастеру (роль admin проходит везде через role_required).

@login_required
def fault_type_list(request):
    fault_types = (
        FaultType.objects.select_related('equipment_model')
        .prefetch_related('parts__part')
        .order_by('equipment_model__name', 'name')
    )
    return render(request, 'core/faults/list.html', {'fault_types': fault_types})


@login_required
def fault_type_create(request):
    if request.method == 'POST':
        form = FaultTypeForm(request.POST)
        formset = FaultTypePartFormSet(request.POST, prefix='parts')
        if form.is_valid() and formset.is_valid():
            fault_type = form.save()
            formset.instance = fault_type
            formset.save()
            messages.success(request, 'Типовая неисправность добавлена')
            return redirect('fault_type_list')
        messages.error(request, 'Неисправность не сохранена: проверьте отмеченные поля')
    else:
        form = FaultTypeForm()
        formset = FaultTypePartFormSet(prefix='parts')
    return render(request, 'core/faults/form.html', {
        'form': form, 'formset': formset, 'title': 'Новая типовая неисправность',
    })


@login_required
def fault_type_edit(request, pk):
    fault_type = get_object_or_404(FaultType, pk=pk)
    if request.method == 'POST':
        form = FaultTypeForm(request.POST, instance=fault_type)
        formset = FaultTypePartFormSet(request.POST, instance=fault_type, prefix='parts')
        if form.is_valid() and formset.is_valid():
            form.save()
            formset.save()
            messages.success(request, 'Типовая неисправность обновлена')
            return redirect('fault_type_list')
        messages.error(request, 'Неисправность не сохранена: проверьте отмеченные поля')
    else:
        form = FaultTypeForm(instance=fault_type)
        formset = FaultTypePartFormSet(instance=fault_type, prefix='parts')
    return render(request, 'core/faults/form.html', {
        'form': form, 'formset': formset, 'title': 'Редактирование типовой неисправности',
        'fault_type': fault_type,
    })


@role_required('repair_manager', 'warehouse')
def fault_type_delete(request, pk):
    fault_type = get_object_or_404(FaultType, pk=pk)
    if request.method == 'POST':
        fault_type.delete()
        messages.success(request, 'Типовая неисправность удалена')
        return redirect('fault_type_list')
    return render(request, 'core/faults/delete.html', {'fault_type': fault_type})


# ==================== ЗАКАЗЫ НА РЕМОНТ ====================

def _filter_orders(request):
    """Отбор заказов по параметрам GET-запроса.

    Поиск идёт по всему, что сотрудник может помнить о заказе: номеру,
    заказчику, серийнику любой единицы в заказе, номеру счёта, трек-номеру
    и описанию неисправности — как в самом заказе, так и по единицам.
    Искать «где-то это было» по одному полю за раз бессмысленно: обычно
    помнят обрывок, но не помнят, какое это было поле.
    """
    search = request.GET.get('q', '').strip()
    status = request.GET.get('status', '')
    payment_status = request.GET.get('payment_status', '')
    client_id = request.GET.get('client', '')
    model_id = request.GET.get('model', '')
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')

    orders = RepairOrder.objects.select_related('client').prefetch_related(
        'order_equipments__equipment__model'
    )

    if search:
        condition = (
            Q(order_number__icontains=search) |
            Q(client__name__icontains=search) |
            Q(client__inn__icontains=search) |
            Q(invoice_number__icontains=search) |
            Q(tracking_number__icontains=search) |
            Q(fault_description__icontains=search) |
            Q(order_equipments__fault_description__icontains=search) |
            Q(order_equipments__equipment__serial_number__icontains=search) |
            Q(order_equipments__equipment__model__name__icontains=search)
        )
        # Серийник ищем и в нормализованном виде — «буад 1234» должен найти
        # заказ с «БУАД-1234», так же как это уже работает в оборудовании
        normalized = Equipment.normalize_serial(search)
        if normalized:
            condition |= Q(order_equipments__equipment__serial_normalized__contains=normalized)
        orders = orders.filter(condition)

    if status in dict(RepairOrder.STATUS_CHOICES):
        orders = orders.filter(status=status)
    if payment_status in dict(RepairOrder.PAYMENT_STATUS_CHOICES):
        orders = orders.filter(payment_status=payment_status)
    if client_id.isdigit():
        orders = orders.filter(client_id=int(client_id))
    if model_id.isdigit():
        orders = orders.filter(order_equipments__equipment__model_id=int(model_id))
    # Даты приходят из адресной строки и правятся руками — неразобранное
    # значение не применяется, иначе страница падала бы с ошибкой базы
    if parse_date(date_from):
        orders = orders.filter(date_received__date__gte=date_from)
    if parse_date(date_to):
        orders = orders.filter(date_received__date__lte=date_to)

    # distinct нужен из-за связи с оборудованием: заказ с тремя единицами
    # иначе попал бы в результат трижды
    return orders.distinct().order_by('-date_received'), {
        'search': search,
        'status_filter': status,
        'payment_status_filter': payment_status,
        'client_filter': client_id,
        'model_filter': model_id,
        'date_from': date_from,
        'date_to': date_to,
    }


@login_required
def repair_order_list(request):
    orders, filter_context = _filter_orders(request)

    paginator = Paginator(orders, 25)
    page = request.GET.get('page')
    page_obj = paginator.get_page(page)

    # Признак «фильтры выставлены» — чтобы показать, сколько всего нашлось,
    # и дать кнопку сброса: иначе пустой список выглядит как пустая база
    filters_active = any(
        value for key, value in filter_context.items() if key != 'search'
    ) or bool(filter_context['search'])

    return render(request, 'core/repair_orders/list.html', {
        'orders': page_obj,
        'status_choices': RepairOrder.STATUS_CHOICES,
        'payment_status_choices': RepairOrder.PAYMENT_STATUS_CHOICES,
        'clients': Client.objects.order_by('name'),
        'equipment_models': EquipmentModel.objects.order_by('name'),
        'filters_active': filters_active,
        'found_count': paginator.count,
        **filter_context,
    })


@login_required
def repair_order_export(request):
    """Список заказов в Excel — с теми же условиями, что выставлены на странице."""
    orders, _ = _filter_orders(request)

    # Стоимости одним запросом. Считать их свойством заказа нельзя: это запрос
    # на строку, а выгружают обычно как раз длинные списки. Отдельный запрос
    # вместо annotate — потому что фильтры уже соединяют таблицу с
    # оборудованием, и сумма по такому соединению задвоилась бы.
    costs = dict(
        RepairOrderEquipment.objects
        .filter(repair_order__in=orders)
        .values_list('repair_order_id')
        .annotate(total=Sum('repair_cost'))
    )

    headers = [
        '№ заказа', 'Дата приёма', 'Заказчик', 'ИНН', 'Оборудование',
        'Статус ремонта', 'Статус оплаты', '№ счёта', 'Дата счёта',
        'Дата завершения', 'Трек-номер', 'Сумма, ₽',
    ]
    rows = []
    total_sum = 0
    for order in orders:
        cost = costs.get(order.pk) or 0
        total_sum += cost
        rows.append([
            order.order_number,
            excel_datetime(order.date_received),
            order.client.name,
            order.client.inn,
            ', '.join(str(roe.equipment) for roe in order.order_equipments.all()),
            order.get_status_display(),
            order.get_payment_status_display(),
            order.invoice_number,
            order.invoice_date,
            excel_datetime(order.date_completed),
            order.tracking_number,
            cost,
        ])
    if rows:
        rows.append(['Итого'] + [''] * (len(headers) - 2) + [total_sum])

    wb = build_workbook('Заказы', headers, rows)
    return xlsx_response(wb, f'Заказы {timezone.localdate():%Y-%m-%d}.xlsx')


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
    order_equipments = list(order.order_equipments.select_related('equipment__model').order_by('id'))

    # Гарантия по прошлым ремонтам: текущий заказ исключён, иначе сразу после
    # его завершения он же и попадал бы в «повторное обращение по гарантии»
    previous_warranty = Equipment.warranty_map(
        [oe.equipment for oe in order_equipments], exclude_order_id=order.pk
    )
    for oe in order_equipments:
        oe.previous_warranty = previous_warranty.get(oe.equipment_id)

    detail_form = RepairOrderDetailForm()
    status_form = StatusChangeForm()
    # Себестоимость деталей: детали без цены в сумму не входят, поэтому
    # складываем то, что известно, а не подставляем ноль
    details_cost = sum(
        detail.cost for detail in details if detail.cost is not None
    )
    return render(request, 'core/repair_orders/detail.html', {
        'order': order,
        'details': details,
        'history': history,
        'order_equipments': order_equipments,
        'details_cost': details_cost,
        'payments': order.payments.select_related('created_by'),
        'payment_form': PaymentForm(),
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


def _cost_from_allocations(movement):
    """Себестоимость исходящего движения `movement` по фактически задействованным
    партиям (`StockAllocation`), уже созданным для него.

    None — сумма неизвестна целиком: либо часть количества осталась без
    партии (нехватка остатка на момент списания), либо хотя бы у одной
    задействованной партии не заполнена цена. Частичная сумма без учёта
    неизвестного остатка не возвращается — она вводила бы в заблуждение,
    будто себестоимость посчитана полностью.
    """
    allocations = list(movement.allocations.select_related('incoming'))
    covered = sum(a.quantity for a in allocations)
    if covered < movement.quantity:
        return None
    if any(a.incoming.unit_price is None for a in allocations):
        return None
    return sum((a.quantity * a.incoming.unit_price for a in allocations), Decimal('0'))


def _use_repair_order_part(order, part, quantity, employee, history_note):
    """Списывает деталь со склада и создаёт запись использованной в заказе
    детали — общая часть добавления детали вручную и применения шаблона
    неисправности.

    Не форма и не отдельная транзакция: вызывающая сторона решает, сколько
    таких вызовов войдёт в одну атомарную операцию — единственную деталь
    (`repair_order_add_detail`) или пачку по шаблону
    (`apply_fault_templates`), где частичный сбой посреди применения не
    должен оставить заказ с половиной добавленных деталей.

    Списание распределяется по партиям прихода (FIFO — см.
    `StockAllocation.allocate`), и по нему заводится запись затраты
    (`OrderCost`, category='parts') — этим и отличается от ручного
    списания (`part_stock_outgoing`), не привязанного к заказу.

    Возвращает True, если остатка не хватило и он ушёл в минус.
    """
    shortage = part.current_stock < quantity
    RepairOrderDetail.objects.create(repair_order=order, part=part, quantity_used=quantity)
    part.current_stock -= quantity
    part.save(update_fields=['current_stock'])
    movement = StockMovement.objects.create(
        part=part,
        quantity=quantity,
        movement_type='outgoing',
        repair_order=order,
        notes=f'Списано по заказу {order.order_number}',
        created_by=employee
    )
    StockAllocation.allocate(movement)
    OrderCost.objects.create(
        repair_order=order, category='parts', amount=_cost_from_allocations(movement)
    )
    OrderStatusHistory.objects.create(
        order=order,
        status=order.status,
        changed_by=employee,
        notes=history_note
    )
    return shortage


def apply_fault_templates(order, fault_types, employee):
    """Строит объединённый рецепт деталей по выбранным неисправностям и
    списывает его одной атомарной транзакцией на все позиции сразу.

    Одна и та же деталь в рецептах нескольких выбранных неисправностей даёт
    одну позицию с суммарным количеством — а не несколько строк с частями
    этого количества. С уже имеющимися в заказе деталями (введёнными вручную
    или добавленными прошлым применением шаблона) слияния нет: шаблон
    дополняет список, а не пересчитывает его целиком.

    Возвращает (added, shortages): added — список (SparePart, количество)
    добавленных позиций в порядке первого появления детали среди рецептов;
    shortages — те из них, на которые не хватило остатка на складе.
    """
    merged_qty = {}
    merged_part = {}
    order_of_appearance = []
    for fault in fault_types:
        for line in fault.parts.select_related('part'):
            if line.part_id not in merged_qty:
                merged_qty[line.part_id] = 0
                merged_part[line.part_id] = line.part
                order_of_appearance.append(line.part_id)
            merged_qty[line.part_id] += line.quantity

    added = []
    shortages = []
    with transaction.atomic():
        for part_id in order_of_appearance:
            part = merged_part[part_id]
            quantity = merged_qty[part_id]
            shortage = _use_repair_order_part(
                order, part, quantity, employee,
                f'Деталь {part.name} x{quantity} добавлена по шаблону неисправности'
            )
            added.append((part, quantity))
            if shortage:
                shortages.append(part)
    return added, shortages


@login_required
@require_POST
def repair_order_add_detail(request, pk):
    """Добавление детали в заказ со списанием со склада (явная транзакция)."""
    order = get_object_or_404(RepairOrder, pk=pk)
    form = RepairOrderDetailForm(request.POST)
    if form.is_valid():
        part = form.cleaned_data['part']
        quantity = form.cleaned_data['quantity_used']

        if part.current_stock < quantity:
            messages.warning(request,
                f'Внимание: недостаточно {part.name} на складе! Текущий остаток: {part.current_stock}. '
                f'Будет списано с отрицательным остатком.')

        with transaction.atomic():
            _use_repair_order_part(
                order, part, quantity, request.user,
                f'Добавлена деталь {part.name} x{quantity}'
            )

        messages.success(request, f'Деталь {part.name} добавлена в заказ')
    else:
        messages.error(request, 'Ошибка при добавлении детали')
    return redirect('repair_order_detail', pk=pk)


@login_required
@require_POST
def repair_order_apply_fault_template(request, pk):
    """Применение рецептов деталей выбранных типовых неисправностей.

    Дополняет список использованных в заказе деталей, ничего в нём не
    заменяя — та же логика списания, что и у ручного добавления детали
    (см. `_use_repair_order_part`), но одним вызовом на каждую позицию уже
    слитого по всем выбранным неисправностям словаря и одной транзакцией
    на всю пачку сразу.
    """
    order = get_object_or_404(RepairOrder, pk=pk)
    fault_ids = [v for v in request.POST.getlist('fault_ids') if str(v).isdigit()]
    fault_types = list(FaultType.objects.filter(pk__in=fault_ids).prefetch_related('parts__part'))
    if not fault_types:
        return JsonResponse({
            'success': False,
            'error': 'Выберите хотя бы одну неисправность из списка — «Другое» своего рецепта не имеет.'
        })

    added, shortages = apply_fault_templates(order, fault_types, request.user)
    if not added:
        return JsonResponse({
            'success': False,
            'error': 'В рецепте выбранных неисправностей деталей не указано.'
        })

    lines = ', '.join(f'{part.name} ×{quantity}' for part, quantity in added)
    message = f'Шаблон применён, в заказ добавлено: {lines}.'
    if shortages:
        names = ', '.join(sorted({part.name for part in shortages}))
        message += f' Внимание: не хватило на складе — {names}, списано с отрицательным остатком.'

    return JsonResponse({'success': True, 'message': message, 'count': len(added)})


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

        # Оповещение заказчику — в очередь, не отправкой на месте: SMTP через
        # домашний канал может думать секундами, а страница ждать не должна
        notifications.notify_order_status(order, changed_by=request.user)

        messages.success(request, f'Статус изменён на «{order.get_status_display()}»')
    return redirect('repair_order_detail', pk=pk)


# Единственный переход, разрешённый массовой сменой статуса. Курьер обычно
# забирает сразу пачку готовых заказов — это одно реальное событие, и ошибка
# «применил не к тому заказу» здесь маловероятна. У остальных переходов
# («в ремонт», «готов к отгрузке» и т.д.) риск выше: часть выбранных заказов
# может незаметно не подойти, и решение о них лучше принимать по одному.
BULK_STATUS_FROM = 'ready_for_shipment'
BULK_STATUS_TO = 'shipped'


@login_required
def repair_order_bulk_status(request):
    """Массовая отгрузка отмеченных заказов.

    GET — только показ: что применится, что будет пропущено и почему,
    без единого изменения в базе (страница подтверждения). POST выполняет
    переход для тех заказов, что на этот момент действительно «Готовы
    к отгрузке» — остальные пропускаются с явной причиной, а не молча.
    """
    ids = request.POST.getlist('ids') if request.method == 'POST' else _selected_ids(request)
    orders = list(
        RepairOrder.objects
        .filter(pk__in=[value for value in ids if str(value).isdigit()])
        .select_related('client')
        .prefetch_related('order_equipments__equipment__model')
        .order_by('pk')
    )

    ineligible = [
        {'order': order, 'reason': f'Статус «{order.get_status_display()}», а не «Готов к отгрузке»'}
        for order in orders if order.status != BULK_STATUS_FROM
    ]
    eligible = [order for order in orders if order.status == BULK_STATUS_FROM]

    if request.method == 'POST':
        if not orders:
            messages.warning(request, 'Отгружать нечего: ни один заказ не отмечен')
            return redirect('repair_order_list')

        now = timezone.now()
        with transaction.atomic():
            for order in eligible:
                order.status = BULK_STATUS_TO
                order.shipping_date = now
                order.date_completed = now
                order.save()
                OrderStatusHistory.objects.create(
                    order=order,
                    status=BULK_STATUS_TO,
                    changed_by=request.user,
                    notes='Массовая отгрузка отмеченных заказов',
                )

        # Оповещения — после того, как переходы записаны: письмо не должно
        # мешать основной операции, и по той же причине, что и у одиночной
        # смены статуса, кладём их в очередь, а не отправляем на месте
        for order in eligible:
            notifications.notify_order_status(order, changed_by=request.user)

        if eligible:
            messages.success(request, f'Отгружено заказов: {len(eligible)}')
        if ineligible:
            messages.warning(
                request,
                f'Пропущено без изменений: {len(ineligible)} — '
                'не в статусе «Готов к отгрузке»'
            )

        return render(request, 'core/repair_orders/bulk_status.html', {
            'done': True,
            'applied': eligible,
            'skipped': ineligible,
        })

    return render(request, 'core/repair_orders/bulk_status.html', {
        'done': False,
        'orders': orders,
        'eligible': eligible,
        'ineligible': ineligible,
        'from_status': dict(RepairOrder.STATUS_CHOICES).get(BULK_STATUS_FROM),
        'to_status': dict(RepairOrder.STATUS_CHOICES).get(BULK_STATUS_TO),
    })


@role_required('accountant')
@require_POST
def repair_order_add_payment(request, pk):
    """Внести поступившие деньги по заказу.

    Статус оплаты после этого пересчитывается сам (см. core/signals.py):
    вручную выставленный «частично оплачен» после последнего платежа
    оставлял бы заказ в должниках навсегда.
    """
    order = get_object_or_404(RepairOrder, pk=pk)
    form = PaymentForm(request.POST)
    if not form.is_valid():
        error = next(iter(form.errors.values()))[0]
        messages.error(request, f'Оплата не внесена: {error}')
        return redirect('repair_order_detail', pk=pk)

    payment = form.save(commit=False)
    payment.repair_order = order
    payment.created_by = request.user
    payment.save()

    order.refresh_from_db()
    OrderStatusHistory.objects.create(
        order=order,
        payment_status=order.payment_status,
        changed_by=request.user,
        notes=f'Внесена оплата {payment.amount} ₽'
              + (f' — {payment.note}' if payment.note else ''),
    )

    remaining = order.debt
    messages.success(
        request,
        f'Оплата {payment.amount} ₽ внесена. '
        + (f'Остаток: {remaining} ₽' if remaining else 'Заказ оплачен полностью')
    )
    return redirect('repair_order_detail', pk=pk)


@role_required('accountant')
@require_POST
def repair_order_delete_payment(request, pk, payment_pk):
    """Убрать ошибочно внесённую оплату.

    Суммы вбивают руками, и опечатка в разряде — обычное дело; без этой
    кнопки чинить пришлось бы через административную часть Django.
    """
    order = get_object_or_404(RepairOrder, pk=pk)
    payment = get_object_or_404(Payment, pk=payment_pk, repair_order=order)
    amount = payment.amount
    payment.delete()

    order.refresh_from_db()
    OrderStatusHistory.objects.create(
        order=order,
        payment_status=order.payment_status,
        changed_by=request.user,
        notes=f'Удалена оплата {amount} ₽',
    )
    messages.success(request, f'Оплата {amount} ₽ удалена')
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

def _as_number(value):
    """Число из строки запроса или None.

    Значения приходят из адресной строки, и её правят руками. Раньше здесь
    стоял голый float(), и «5 В» вместо «5» роняло страницу целиком.
    """
    try:
        return float(str(value).replace(',', '.'))
    except (TypeError, ValueError):
        return None


def _filter_parts(request):
    """Общая фильтрация деталей по параметрам GET-запроса (используется списком и экспортом)."""
    search = request.GET.get('q', '')
    component_type = request.GET.get('component_type', '')
    package = request.GET.get('package', '')

    # Диапазоны характеристик: поле в модели -> префикс параметра
    ranges = {
        'voltage': 'voltage',
        'current': 'current',
        'resistance': 'resistance',
        'capacitance': 'capacitance',
        'power': 'power',
    }
    stock_state = request.GET.get('stock_state', '')
    # Старая ссылка вида ?below_min=1 продолжает работать
    if not stock_state and request.GET.get('below_min'):
        stock_state = 'below'

    stock_from = request.GET.get('stock_from', '')
    stock_to = request.GET.get('stock_to', '')

    parts = SparePart.objects.all()
    if search:
        parts = parts.filter(
            Q(part_number__icontains=search) |
            Q(name__icontains=search) |
            Q(component_type__icontains=search) |
            Q(package__icontains=search) |
            Q(description__icontains=search)
        )
    if component_type:
        parts = parts.filter(component_type=component_type)
    if package:
        parts = parts.filter(package=package)

    context = {
        'search': search,
        'component_type': component_type,
        'package': package,
        'stock_from': stock_from,
        'stock_to': stock_to,
        'stock_state': stock_state,
    }

    for field, prefix in ranges.items():
        for bound, lookup in (('from', 'gte'), ('to', 'lte')):
            raw = request.GET.get(f'{prefix}_{bound}', '')
            context[f'{prefix}_{bound}'] = raw
            value = _as_number(raw)
            if value is not None:
                parts = parts.filter(**{f'{field}__{lookup}': value})

    stock_low = _as_number(stock_from)
    stock_high = _as_number(stock_to)
    if stock_low is not None:
        parts = parts.filter(current_stock__gte=int(stock_low))
    if stock_high is not None:
        parts = parts.filter(current_stock__lte=int(stock_high))

    if stock_state == 'below':
        parts = parts.below_minimum()
    elif stock_state == 'at_minimum':
        parts = parts.at_minimum()
    elif stock_state == 'attention':
        # Дефицит и «ровно на минимуме» вместе — то, на что стоит смотреть
        parts = parts.filter(
            Q(current_stock__lt=F('min_stock')) |
            Q(min_stock__gt=0, current_stock=F('min_stock'))
        )

    return parts, context


@login_required
def part_list(request):
    parts, filter_context = _filter_parts(request)

    paginator = Paginator(parts.order_by('part_number'), 25)
    page = request.GET.get('page')
    return render(request, 'core/parts/list.html', {
        'parts': paginator.get_page(page),
        **_part_choices(),
        'found_count': paginator.count,
        'filters_active': any(filter_context.values()),
        **filter_context,
    })


@login_required
def part_export(request):
    """Экспорт радиодеталей в Excel (с учётом текущих фильтров списка)."""
    parts, _ = _filter_parts(request)

    # Заголовки — имена полей, а не русские подписи: этот же файл принимает
    # импорт, и переименование колонок разорвало бы выгрузку и загрузку
    headers = [
        'part_number', 'name', 'component_type', 'package',
        'resistance', 'resistance_unit',
        'power', 'power_unit',
        'voltage', 'voltage_unit',
        'current', 'current_unit',
        'capacitance', 'capacitance_unit',
        'min_stock', 'current_stock', 'lead_time_days',
        'price', 'preferred_supplier', 'application', 'description',
    ]
    rows = [
        [getattr(part, field) for field in headers]
        for part in parts.order_by('part_number')
    ]
    wb = build_workbook('Радиодетали', headers, rows)
    return xlsx_response(wb, 'spare_parts.xlsx')


@login_required
def part_detail(request, pk):
    part = get_object_or_404(SparePart.objects.prefetch_related('movements'), pk=pk)
    movements = part.movements.order_by('-movement_date')[:50]
    # Все ячейки, кроме уже назначенной этой детали — деталь можно добавить
    # и в занятую другой деталью ячейку (в одной ячейке может быть несколько деталей)
    available_cells = StorageCell.objects.prefetch_related('parts').exclude(
        parts=part
    ).order_by('cabinet__number', 'row_number', 'cell_row')
    stock_form = StockMovementForm()
    return render(request, 'core/parts/detail.html', {
        'part': part,
        'movements': movements,
        'available_cells': available_cells,
        'stock_form': stock_form,
    })


def _part_choices():
    """Уже встречавшиеся типы компонентов и корпуса — для подсказок в форме
    и отбора в списке. Оба поля заполняются руками, и без готового списка
    одно и то же обозначение расходится на «TO-220» и «то220»."""
    return {
        'component_types': list(
            SparePart.objects.exclude(component_type='')
            .values_list('component_type', flat=True).distinct().order_by('component_type')
        ),
        'packages': list(
            SparePart.objects.exclude(package='')
            .values_list('package', flat=True).distinct().order_by('package')
        ),
        'applications': list(
            SparePart.objects.exclude(application='')
            .values_list('application', flat=True).distinct().order_by('application')
        ),
    }


def _measurement_pairs(form):
    """Пары «значение — единица измерения» для вывода в одной строке формы."""
    return [
        (form[value], form[unit])
        for value, unit in (
            ('resistance', 'resistance_unit'),
            ('power', 'power_unit'),
            ('voltage', 'voltage_unit'),
            ('current', 'current_unit'),
            ('capacitance', 'capacitance_unit'),
        )
    ]


@login_required
def part_create(request):
    if request.method == 'POST':
        form = SparePartForm(request.POST)
        if form.is_valid():
            part = form.save()
            messages.success(request, f'Деталь {part.part_number} добавлена')
            return redirect('part_detail', pk=part.pk)
        messages.error(request, 'Деталь не сохранена: проверьте отмеченные поля')
    else:
        form = SparePartForm()
    return render(request, 'core/parts/form.html', {
        'form': form,
        'title': 'Новая деталь',
        'measurement_pairs': _measurement_pairs(form),
        **_part_choices(),
    })


@login_required
def part_edit(request, pk):
    part = get_object_or_404(SparePart, pk=pk)
    if request.method == 'POST':
        form = SparePartForm(request.POST, instance=part)
        if form.is_valid():
            form.save()
            messages.success(request, 'Деталь обновлена')
            return redirect('part_detail', pk=part.pk)
        messages.error(request, 'Изменения не сохранены: проверьте отмеченные поля')
    else:
        form = SparePartForm(instance=part)
    return render(request, 'core/parts/form.html', {
        'form': form,
        'title': 'Редактирование детали',
        'part': part,
        'measurement_pairs': _measurement_pairs(form),
        **_part_choices(),
    })


@role_required('warehouse')
def part_bulk_delete(request):
    """Удаление отмеченных деталей — списком, а не по одной.

    После загрузки каталога из файла лишние позиции удаляют десятками,
    и открывать на каждую отдельную страницу подтверждения бессмысленно.

    Страница подтверждения показывает, что уйдёт вместе с деталями:
    удаление уносит за собой историю движений и записи о том, что деталь
    ставили в заказ. Восстановить это можно только из резервной копии,
    поэтому сказано об этом прямо, а не мелким шрифтом.
    """
    ids = request.POST.getlist('ids') if request.method == 'POST' else _selected_ids(request)
    parts = (
        SparePart.objects
        .filter(pk__in=[value for value in ids if str(value).isdigit()])
        .annotate(
            movement_count=Count('movements', distinct=True),
            order_count=Count('repairorderdetail', distinct=True),
        )
        .prefetch_related('storage_cells')
        .order_by('part_number')
    )

    parts = list(parts)

    if request.method == 'POST':
        if not parts:
            messages.warning(request, 'Удалять нечего: ни одна деталь не отмечена')
            return redirect('part_list')
        numbers = [part.part_number for part in parts]
        SparePart.objects.filter(pk__in=[part.pk for part in parts]).delete()
        messages.success(request, (
            f'Удалено деталей: {len(numbers)}'
            if len(numbers) > 1 else f'Деталь {numbers[0]} удалена'
        ))
        return redirect('part_list')

    return render(request, 'core/parts/bulk_delete.html', {
        'parts': parts,
        'in_stock': [part for part in parts if part.current_stock],
        'in_orders': [part for part in parts if part.order_count],
    })


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
            updated = ['current_stock']
            # Цена детали идёт следом за последней поставкой: вводить её
            # дважды — в приходе и в карточке — никто не будет, а расходится
            # она молча
            if movement.unit_price is not None:
                part.price = movement.unit_price
                updated.append('price')
            part.save(update_fields=updated)
            movement.save()
        message = f'Приход +{movement.quantity} {part.name} оформлен'
        if movement.unit_price is not None:
            message += f'. Цена детали обновлена: {movement.unit_price} ₽'
        messages.success(request, message)
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
            movement = StockMovement.objects.create(
                part=part,
                quantity=qty,
                movement_type='outgoing',
                document_number=doc_num,
                notes=f'{notes} (причина: {dict(form.fields["reason"].choices).get(reason)})',
                created_by=request.user
            )
            # Не привязано к заказу — OrderCost для ручного списания не
            # заводится (нет заказа, на который отнести затрату), только
            # распределение по партиям для будущей себестоимости
            StockAllocation.allocate(movement)
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


# Пары «значение — единица измерения» характеристик детали
SPEC_FIELDS = (
    ('voltage', 'voltage_unit'),
    ('current', 'current_unit'),
    ('power', 'power_unit'),
    ('resistance', 'resistance_unit'),
    ('capacitance', 'capacitance_unit'),
)


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
                        'package': _get_str(data.get('package')),
                        'voltage': _parse_decimal(data.get('voltage')),
                        'voltage_unit': _get_str(data.get('voltage_unit')),
                        'current': _parse_decimal(data.get('current')),
                        'current_unit': _get_str(data.get('current_unit')),
                        'power': _parse_decimal(data.get('power')),
                        'power_unit': _get_str(data.get('power_unit')),
                        'resistance': _parse_decimal(data.get('resistance')),
                        'resistance_unit': _get_str(data.get('resistance_unit')),
                        'capacitance': _parse_decimal(data.get('capacitance')),
                        'capacitance_unit': _get_str(data.get('capacitance_unit')),
                        'min_stock': _parse_int(data.get('min_stock'), 5),
                        'current_stock': _parse_int(data.get('current_stock'), 0),
                        'lead_time_days': _parse_int(data.get('lead_time_days'), 14),
                        'price': _parse_decimal(data.get('price')),
                        'application': _get_str(data.get('application'))[:100],
                        'description': _get_str(data.get('description')),
                    }

                    # Единица без значения — мусор: «Ом» у диода не значит
                    # ничего, а в форме и в выгрузке выглядит как заполненное
                    # поле. В присланных файлах единицы обычно проставлены
                    # во всех строках подряд, независимо от значений.
                    # Пустую единицу при заполненном значении, наоборот,
                    # не переносим: она затёрла бы уже введённую руками.
                    for value_field, unit_field in SPEC_FIELDS:
                        if defaults[value_field] is None:
                            defaults[unit_field] = ''
                        elif not defaults[unit_field]:
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
    """Визуальная сетка кассетниц. Одна ячейка может содержать несколько деталей.

    Ряды рисуются по фактическим ячейкам, а не по восьмёркам: в ряду из
    четырёх ячеек каждая занимает четверть ширины и выглядит крупной —
    ровно как крупные ящики внизу настоящего органайзера.
    """
    cabinets = list(Cabinet.objects.all())
    if not cabinets:
        return render(request, 'core/storage_cells/grid.html', {
            'cabinets': [], 'cabinet': None, 'rows': [],
        })

    requested = request.GET.get('cabinet', '')
    cabinet = next(
        (item for item in cabinets if str(item.number) == requested), cabinets[0]
    )

    selected_part_id = request.GET.get('selected_part', '')
    move_from = request.GET.get('move_from', '')
    selected_part = None
    if selected_part_id:
        selected_part = get_object_or_404(SparePart, pk=selected_part_id)

    cells = cabinet.cells.prefetch_related('parts').order_by('row_number', 'cell_row')
    rows_map = {}
    cells_data = {}
    for cell in cells:
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

        status = cell.get_status()
        cell_parts = cells_data[cell.pk]['parts']
        if selected_part and any(p['id'] == selected_part.pk for p in cell_parts):
            status = 'selected'
        rows_map.setdefault(cell.row_number, []).append({
            'cell': cell,
            'status': status,
            'part_count': len(cell_parts),
        })

    # Ширина ячейки в процентах, чтобы ряд из четырёх выглядел вчетверо
    # крупнее ряда из шестнадцати. Считаем здесь, а не в шаблоне: там
    # деления нет, а подгонять классами Bootstrap под любое число нельзя.
    #
    # Строкой, а не числом: локаль ru-ru печатает дробь через запятую,
    # и «calc(12,5% - 4px)» — невалидный CSS. Ячейки тогда остаются
    # без ширины, а на экране это выглядит как развалившаяся вёрстка
    rows = [
        {
            'number': number,
            'cells': rows_map[number],
            'width': f'{100 / len(rows_map[number]):.4f}',
        }
        for number in sorted(rows_map)
    ]

    parts = SparePart.objects.all().order_by('part_number')
    all_parts_data = [
        {'id': p.pk, 'label': f'{p.part_number} — {p.name}'}
        for p in parts
    ]

    return render(request, 'core/storage_cells/grid.html', {
        'rows': rows,
        'cabinet': cabinet,
        'cabinets': cabinets,
        'selected_part': selected_part,
        'parts': parts,
        'move_from': move_from,
        'cells_data_json': json.dumps(cells_data, ensure_ascii=False),
        'all_parts_json': json.dumps(all_parts_data, ensure_ascii=False),
    })


# ==================== КАССЕТНИЦЫ ====================

@role_required('warehouse')
def cabinet_list(request):
    """Список кассетниц с их раскладкой."""
    cabinets = Cabinet.objects.prefetch_related('cells')
    rows = [
        {
            'cabinet': cabinet,
            'layout': cabinet.layout_text,
            'cell_count': cabinet.cell_count,
            'occupied': sum(1 for cell in cabinet.cells.all() if cell.parts.exists()),
        }
        for cabinet in cabinets
    ]
    return render(request, 'core/storage_cells/cabinets.html', {'rows': rows})


@role_required('warehouse')
def cabinet_create(request):
    """Новая кассетница: номер, название и раскладка по рядам."""
    if request.method == 'POST':
        form = CabinetForm(request.POST)
        if form.is_valid():
            cabinet = form.save()
            added, _ = cabinet.apply_layout(form.cleaned_data['layout'])
            messages.success(
                request, f'Кассетница {cabinet.number} создана, ячеек: {added}')
            return redirect('cabinet_list')
        messages.error(request, 'Кассетница не создана: проверьте отмеченные поля')
    else:
        form = CabinetForm()
    return render(request, 'core/storage_cells/cabinet_form.html', {
        'form': form, 'title': 'Новая кассетница',
    })


@role_required('warehouse')
def cabinet_edit(request, pk):
    """Правка кассетницы, в том числе раскладки.

    Ячейки, оказавшиеся за пределами новой раскладки, удаляются — но
    только пустые: занятые форма не пропускает.
    """
    cabinet = get_object_or_404(Cabinet, pk=pk)
    if request.method == 'POST':
        form = CabinetForm(request.POST, instance=cabinet)
        if form.is_valid():
            cabinet = form.save()
            added, removed = cabinet.apply_layout(form.cleaned_data['layout'])
            messages.success(request, (
                f'Кассетница {cabinet.number} сохранена. '
                f'Добавлено ячеек: {added}, удалено: {removed}'
            ))
            return redirect('cabinet_list')
        messages.error(request, 'Изменения не сохранены: проверьте отмеченные поля')
    else:
        form = CabinetForm(instance=cabinet)
    return render(request, 'core/storage_cells/cabinet_form.html', {
        'form': form, 'cabinet': cabinet,
        'title': f'Кассетница {cabinet.number}',
    })


@role_required('warehouse')
def cabinet_delete(request, pk):
    """Удаление кассетницы вместе с её ячейками.

    Занятые ячейки удалять не даём: сведения о том, где лежит деталь,
    восстановить будет неоткуда.
    """
    cabinet = get_object_or_404(Cabinet, pk=pk)
    occupied = [cell for cell in cabinet.cells.prefetch_related('parts') if cell.parts.exists()]

    if request.method == 'POST':
        if occupied:
            messages.error(request, (
                f'Кассетница {cabinet.number} не удалена: в её ячейках лежат '
                f'детали ({len(occupied)} шт.). Сначала переложите их.'
            ))
            return redirect('cabinet_list')
        number = cabinet.number
        cabinet.delete()
        messages.success(request, f'Кассетница {number} удалена')
        return redirect('cabinet_list')

    return render(request, 'core/storage_cells/cabinet_delete.html', {
        'cabinet': cabinet,
        'occupied': occupied,
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


# ==================== ИНВЕНТАРИЗАЦИЯ ====================

def _apply_inventory_discrepancy(line, discrepancy, employee):
    """Применяет расхождение по одной строке инвентаризации.

    `discrepancy` — фактически посчитанное минус ЖИВОЙ остаток детали на
    момент применения; вызывающая сторона (`inventory_confirm`) уже
    пересчитала его против `line.part.current_stock`, здесь это не
    делается повторно, чтобы несколько строк в одной сессии применялись
    против согласованного набора чисел, а не каждая против своего момента.

    Расхождение создаёт обычный `StockMovement` — не отдельную сущность
    корректировки — и для недостачи распределяется по партиям прихода
    (FIFO) точно так же, как любое другое списание в проекте (см.
    `_use_repair_order_part`, `part_stock_outgoing`,
    `StockAllocation.allocate`): решение задачи сознательно не даёт
    вручную выбирать партию под недостачу — причина расхождения
    (пересортица, утеря, ошибка ввода) ни с одной конкретной партией
    не связана сильнее прочих.

    Избыток заводится приходом без цены (находка — не покупка, угадывать
    её нельзя) и поэтому не должен обновлять `SparePart.price` — тот же
    аккуратный флажок, что уже есть в `part_stock_incoming`.

    После применения `part.current_stock` равен ровно
    `line.counted_quantity`, что бы ни случилось с остатком раньше.
    """
    part = line.part
    session = line.session
    where = line.cell.address if line.cell else part.part_number

    if discrepancy > 0:
        movement = StockMovement.objects.create(
            part=part,
            quantity=discrepancy,
            movement_type='incoming',
            unit_price=None,
            notes=f'{line.comment} (причина: Инвентаризация) — сессия №{session.pk}, {where}',
            created_by=employee,
        )
        part.current_stock += discrepancy
        part.save(update_fields=['current_stock'])
        # unit_price не заполнен — цену детали не трогаем, см. part_stock_incoming
    else:
        qty = -discrepancy
        movement = StockMovement.objects.create(
            part=part,
            quantity=qty,
            movement_type='outgoing',
            notes=f'Недостача при инвентаризации (причина: Инвентаризация) — сессия №{session.pk}, {where}',
            created_by=employee,
        )
        part.current_stock -= qty
        part.save(update_fields=['current_stock'])
        StockAllocation.allocate(movement)

    line.movement = movement
    line.save(update_fields=['movement', 'comment'])
    return movement


@login_required
@require_POST
def inventory_start(request, pk):
    """Начинает инвентаризацию кассетницы `pk`.

    Если по ней уже идёт незавершённая сессия — не открываем вторую
    параллельно, а ведём к уже идущей. Строки заводятся по деталям,
    у которых сейчас есть ячейка внутри этой кассетницы: у пустых ячеек
    сверять нечего.
    """
    cabinet = get_object_or_404(Cabinet, pk=pk)
    existing = InventorySession.objects.filter(
        cabinet=cabinet, status=InventorySession.STATUS_IN_PROGRESS
    ).first()
    if existing:
        messages.info(request, f'По кассетнице {cabinet.number} уже идёт инвентаризация — продолжаем её')
        return redirect('inventory_count', pk=existing.pk)

    with transaction.atomic():
        session = InventorySession.objects.create(cabinet=cabinet, started_by=request.user)
        cells = cabinet.cells.prefetch_related('parts').order_by('row_number', 'cell_row')
        new_lines = [
            InventorySessionLine(session=session, part=part, cell=cell, expected_quantity=part.current_stock)
            for cell in cells
            for part in cell.parts.all()
        ]
        InventorySessionLine.objects.bulk_create(new_lines)

    if not new_lines:
        messages.warning(
            request, f'В кассетнице {cabinet.number} нет ни одной размещённой детали — сверять нечего')
    else:
        messages.success(
            request,
            f'Инвентаризация кассетницы {cabinet.number} начата, деталей к пересчёту: {len(new_lines)}'
        )
    return redirect('inventory_count', pk=session.pk)


@login_required
def inventory_count(request, pk):
    """Ввод фактически посчитанного количества по каждой строке сессии.

    Учётное количество (снимок на начало сессии) показывается для
    справки; сохранение не пересчитывает и не применяет ничего — это
    делает следующий шаг, `inventory_confirm`.
    """
    session = get_object_or_404(
        InventorySession.objects.select_related('cabinet'), pk=pk)
    if not session.is_in_progress:
        return redirect('inventory_detail', pk=session.pk)

    lines = session.lines.select_related('part', 'cell').order_by(
        'cell__row_number', 'cell__cell_row', 'part__part_number')

    if request.method == 'POST':
        with transaction.atomic():
            for line in lines:
                raw = request.POST.get(f'counted_{line.pk}', '').strip()
                if raw == '':
                    continue
                try:
                    value = int(raw)
                except ValueError:
                    continue
                if value < 0:
                    continue
                line.counted_quantity = value
                line.save(update_fields=['counted_quantity'])
        messages.success(request, 'Количества сохранены')
        return redirect('inventory_confirm', pk=session.pk)

    return render(request, 'core/inventory/count.html', {
        'session': session,
        'lines': lines,
    })


@login_required
def inventory_confirm(request, pk):
    """Показывает расхождения, посчитанные против ЖИВОГО остатка (не
    против учтённого на начало сессии — см. docstring
    `InventorySessionLine`), и по подтверждению применяет их.

    Избыток без заполненного комментария блокирует применение целиком —
    ни одно движение не создаётся, пока причина не указана по каждой
    строке с превышением.
    """
    session = get_object_or_404(
        InventorySession.objects.select_related('cabinet'), pk=pk)
    if not session.is_in_progress:
        return redirect('inventory_detail', pk=session.pk)

    counted_lines = list(
        session.lines.select_related('part', 'cell')
        .filter(counted_quantity__isnull=False)
        .order_by('cell__row_number', 'cell__cell_row', 'part__part_number')
    )
    uncounted = session.lines.filter(counted_quantity__isnull=True).count()

    rows = [
        {
            'line': line,
            'live_stock': line.part.current_stock,
            'discrepancy': line.counted_quantity - line.part.current_stock,
            'drifted': line.part.current_stock != line.expected_quantity,
        }
        for line in counted_lines
    ]

    if request.method == 'POST':
        comment_values = {
            row['line'].pk: request.POST.get(f"comment_{row['line'].pk}", '').strip()
            for row in rows if row['discrepancy'] > 0
        }
        missing_comment_ids = {pk_ for pk_, text in comment_values.items() if not text}

        if missing_comment_ids:
            for row in rows:
                row['comment_value'] = comment_values.get(row['line'].pk, '')
            messages.error(
                request,
                'Укажите причину избытка по каждой отмеченной строке — без неё заявка не применена'
            )
            return render(request, 'core/inventory/confirm.html', {
                'session': session, 'rows': rows, 'uncounted': uncounted,
                'missing_comment_ids': missing_comment_ids,
            })

        to_apply = [(row['line'], row['discrepancy']) for row in rows if row['discrepancy'] != 0]
        for line, _ in to_apply:
            if line.pk in comment_values:
                line.comment = comment_values[line.pk]

        with transaction.atomic():
            for line, discrepancy in to_apply:
                _apply_inventory_discrepancy(line, discrepancy, request.user)
            session.status = InventorySession.STATUS_COMPLETED
            session.completed_at = timezone.now()
            session.completed_by = request.user
            session.save(update_fields=['status', 'completed_at', 'completed_by'])

        if to_apply:
            messages.success(request, f'Инвентаризация завершена. Применено расхождений: {len(to_apply)}')
        else:
            messages.success(request, 'Инвентаризация завершена. Расхождений не найдено')
        return redirect('inventory_detail', pk=session.pk)

    return render(request, 'core/inventory/confirm.html', {
        'session': session, 'rows': rows, 'uncounted': uncounted,
    })


@login_required
def inventory_list(request):
    """История сессий инвентаризации по всем кассетницам — для аудита."""
    sessions = (
        InventorySession.objects
        .select_related('cabinet', 'started_by', 'completed_by')
        .annotate(
            surplus_count=Count(
                'lines', filter=Q(lines__movement__movement_type='incoming'), distinct=True),
            deficit_count=Count(
                'lines', filter=Q(lines__movement__movement_type='outgoing'), distinct=True),
        )
    )
    return render(request, 'core/inventory/list.html', {
        'sessions': sessions,
        'cabinets': Cabinet.objects.all(),
    })


@login_required
def inventory_detail(request, pk):
    """Одна сессия инвентаризации целиком — для истории/аудита, и как
    экран продолжения незавершённой сессии."""
    session = get_object_or_404(
        InventorySession.objects.select_related('cabinet', 'started_by', 'completed_by'), pk=pk)
    lines = session.lines.select_related('part', 'cell', 'movement').order_by(
        'cell__row_number', 'cell__cell_row', 'part__part_number')
    return render(request, 'core/inventory/detail.html', {
        'session': session,
        'lines': lines,
    })


@login_required
def inventory_delete(request, pk):
    """Удаление черновика инвентаризации: только пока сессия в процессе
    и по ней ещё ничего не применено — иначе это уже аудиторский след."""
    session = get_object_or_404(InventorySession.objects.select_related('cabinet'), pk=pk)
    if not session.can_be_deleted:
        messages.error(request, 'Эту сессию удалить нельзя: по ней уже применены движения склада')
        return redirect('inventory_detail', pk=session.pk)

    if request.method == 'POST':
        cabinet_number = session.cabinet.number
        session.delete()
        messages.success(request, f'Черновик инвентаризации кассетницы {cabinet_number} удалён')
        return redirect('inventory_list')

    return render(request, 'core/inventory/delete.html', {'session': session})


def _label_specs(part):
    """Строка характеристик для этикетки: тип и номиналы через точку."""
    return ' · '.join(value for value in (part.component_type, part.specs_display) if value)


def _grouped_specs(parts):
    """Что у набора общее и чем детали в нём различаются.

    Повторять «0.125Вт» у каждого номинала незачем — в 26 мм ширины из-за
    этого не помещаются сами номиналы. Общее выносим в строку рядом
    с заголовком, под заголовком оставляем только различия — списком,
    по элементу на деталь.
    """
    values = [[value for value in part.specs_display.split(', ') if value] for part in parts]
    common = [value for value in values[0] if all(value in row for row in values[1:])]
    distinct = [
        ', '.join(value for value in row if value not in common) or part.part_number
        for part, row in zip(parts, values)
    ]
    # Если после вычитания общего номиналы совпадают, различить детали по ним
    # нельзя: так вышло с россыпью резисторов, где заполнена только мощность
    # («2Вт, 2Вт, 2Вт»), а номинал живёт в артикуле. Тогда перечисляем артикулы
    if len(set(distinct)) < len(distinct):
        distinct = [part.part_number for part in parts]
    return ', '.join(common), distinct


def _cell_label(cell, base_url):
    """Данные одной этикетки ячейки.

    Поля те же, что и у этикетки детали, и печатаются они тем же шаблоном:
    на пакете и на ячейке одна и та же деталь должна выглядеть одинаково,
    иначе кладовщик каждый раз ищет глазами, где что написано.

    Если все детали в ячейке одного типа (набор номиналов одного типа —
    резисторы, конденсаторы и т.д.), сверху пишется «Набор резисторов»,
    рядом — то, что у них общее, а ниже столбцами номиналы. Разнотипную
    ячейку подписываем перечнем артикулов: общего имени у неё нет.

    Перечисление возвращается списком, а не строкой: на этикетке он рисуется
    сеткой в два столбца. Сплошная строка переносилась посреди номинала —
    «10кОм, 4.7к» и «Ом» на следующей строке, — и понять, где кончается
    одна деталь и начинается другая, было нельзя.
    """
    parts = list(cell.parts.all())
    component_types = {p.component_type for p in parts if p.component_type}
    packages = {p.package for p in parts if p.package}
    applications = {p.application for p in parts if p.application}
    grouped = len(parts) > 1 and len(component_types) == 1
    items = []
    # Применимость на ячейку — только если она у всех одна: «Otis» на ячейке,
    # где половина деталей от ABB, вводит в заблуждение
    application = applications.pop() if len(applications) == 1 else ''

    if not parts:
        title, specs, description, package = '', '', 'Ячейка пуста', ''
    elif len(parts) == 1:
        part = parts[0]
        title, specs = part.part_number, _label_specs(part)
        description, package = part.label_text, part.package
    elif grouped:
        title = f'Набор {plural_genitive(component_types.copy().pop()).lower()}'
        # Номинал — то, чем детали набора различаются; если его не заполнили,
        # различить их можно только по артикулу
        specs, items = _grouped_specs(parts)
        description = ''
        # Корпус общий на всю ячейку — только если он у всех один: «0805»
        # на ячейке, где половина деталей в 1206, хуже, чем ничего
        package = packages.pop() if len(packages) == 1 else ''
    else:
        title = 'Разные детали'
        specs = ''
        description = ''
        items = [p.part_number for p in parts]
        package = packages.pop() if len(packages) == 1 else ''

    link = qr_url(base_url, 'c', cell.pk)
    return {
        'cell': cell,
        'cell_parts': parts,
        'title': title,
        'specs': specs,
        'description': description,
        'items': items,
        'package': package,
        'application': application,
        'address': cell.address,
        # Ссылка вместо простого адреса: сканирование сразу открывает
        # содержимое, а не показывает строку, которую потом ищут вручную
        'qr_url': link,
        'qr_img': generate_qr_image(link),
    }


@login_required
def storage_cell_label(request, pk):
    """Печать этикетки одной ячейки."""
    cell = get_object_or_404(
        StorageCell.objects.select_related('cabinet').prefetch_related('parts'), pk=pk)
    base_url = label_base_url(request)
    context = _cell_label(cell, base_url)
    context['qr_base'] = base_url
    context['qr_warning'] = qr_length_warning([context['qr_url']])
    return render(request, 'core/storage_cells/label.html', context)


def _order_equipment_label(order, roe, position, base_url):
    """Данные одной этикетки оборудования в заказе.

    Общее для одиночной печати и для пачки (`repair_order_labels_batch`) —
    те же поля, тот же QR, чтобы одна и та же единица оборудования не могла
    напечататься по-разному в зависимости от того, откуда её печатали.

    Ссылка на заказ вместо текста «LT-2026-08-001/1». Текст читался
    человеком, но сканирование им ничего не давало: заказ всё равно искали
    руками. Плата за ссылку — код вырастает с 21 до 25–29 модулей
    (сколько именно, зависит от длины LABEL_BASE_URL), поэтому место под
    него освобождено: эмблема с этикетки убрана, код печатается сам по себе.
    """
    link = qr_url(base_url, 'o', order.pk)
    return {
        'order': order,
        'roe': roe,
        'position': position,
        'qr_img': generate_qr_image(link),
        'qr_url': link,
    }


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

    base_url = label_base_url(request)
    context = _order_equipment_label(order, roe, position, base_url)
    context['qr_base'] = base_url
    context['qr_warning'] = qr_length_warning([context['qr_url']])
    return render(request, 'core/repair_orders/equipment_label.html', context)


@login_required
def repair_order_labels_batch(request):
    """Пачка этикеток оборудования: по отмеченным заказам или по всему
    текущему отбору списка. Этикетка печатается не на заказ, а на единицу
    оборудования внутри него — заказ с тремя единицами даёт три этикетки."""
    ids = _selected_ids(request)
    if ids:
        orders = RepairOrder.objects.filter(pk__in=ids)
    else:
        orders, _ = _filter_orders(request)

    # prefetch_related(None) снимает набор из _filter_orders (он тянет
    # оборудование только для поиска по серийнику) — иначе Django ругается
    # на два разных queryset для одного и того же related-пути
    orders = list(
        orders.prefetch_related(None).prefetch_related(
            Prefetch(
                'order_equipments',
                queryset=RepairOrderEquipment.objects.select_related('equipment__model').order_by('id'),
            )
        ).order_by('-date_received')
    )

    base_url = label_base_url(request)
    labels = []
    for order in orders:
        for position, roe in enumerate(order.order_equipments.all(), start=1):
            if len(labels) >= MAX_LABELS_PER_BATCH:
                break
            labels.append(_order_equipment_label(order, roe, position, base_url))
        if len(labels) >= MAX_LABELS_PER_BATCH:
            break

    return render(request, 'core/repair_orders/labels_batch.html', {
        'labels': labels,
        'layout': _batch_layout(request),
        'limit': MAX_LABELS_PER_BATCH,
        'qr_base': base_url,
        'qr_warning': qr_length_warning(label['qr_url'] for label in labels),
    })


# ==================== ОТЧЁТЫ ====================

@login_required
def reports(request):
    return render(request, 'core/reports/index.html')


def _purchase_plan_parts():
    """Детали ниже минимального остатка — общее для отчёта и его выгрузки."""
    return (
        SparePart.objects
        .below_minimum()
        .prefetch_related('storage_cells')
        .order_by('part_number')
    )


@login_required
def report_purchase_plan(request):
    """План закупок — детали ниже минимального остатка."""
    parts = list(_purchase_plan_parts())
    return render(request, 'core/reports/purchase_plan.html', {
        'parts': parts,
        **_purchase_plan_totals(parts),
    })


def _purchase_plan_totals(parts):
    """Во что обойдётся закупка и по скольким деталям цена неизвестна.

    Считаем в Python, а не запросом: список короткий, а цена может быть
    пустой, и складывать её с нулём нельзя — «ноль» и «неизвестно» это
    разные вещи, и вторую надо назвать вслух.
    """
    priced = [part for part in parts if part.price is not None]
    return {
        'plan_total': sum(part.purchase_cost for part in priced),
        'without_price': len(parts) - len(priced),
    }


@login_required
def report_purchase_plan_export(request):
    """План закупок в Excel — файл отправляют поставщику."""
    headers = [
        'Артикул', 'Название', 'Тип', 'Характеристики',
        'Остаток', 'Мин. остаток', 'Не хватает',
        'Цена, ₽', 'Сумма, ₽',
        'Срок поставки, дней', 'Поставщик', 'Ячейка',
    ]
    parts = list(_purchase_plan_parts())
    rows = [
        [
            part.part_number, part.name, part.component_type, part.specs_display,
            part.current_stock, part.min_stock, part.stock_deficit,
            part.price, part.purchase_cost,
            part.lead_time_days, part.preferred_supplier,
            part.current_cell.address if part.current_cell else '',
        ]
        for part in parts
    ]
    # Итог в самой книге: файл уходит поставщику и начальству, и сумму
    # из него достают в первую очередь
    if rows:
        totals = _purchase_plan_totals(parts)
        rows.append(['Итого'] + [''] * 7 + [totals['plan_total']] + [''] * 3)
    wb = build_workbook('План закупок', headers, rows)
    return xlsx_response(wb, f'План закупок {timezone.localdate():%Y-%m-%d}.xlsx')


def _filter_movements(request):
    """Фильтрация движений склада (общая для журнала и его выгрузки)."""
    part_id = request.GET.get('part', '')
    movement_type = request.GET.get('type', '')
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')

    movements = StockMovement.objects.select_related('part', 'repair_order', 'created_by')

    # Значения приходят из адресной строки, и её правят руками: нечисловой
    # номер детали или дата вроде «вчера» роняли бы страницу с ошибкой базы,
    # поэтому неразобранное значение просто не применяется как фильтр.
    if part_id.isdigit():
        movements = movements.filter(part_id=int(part_id))
    if movement_type in dict(StockMovement.MOVEMENT_TYPE_CHOICES):
        movements = movements.filter(movement_type=movement_type)
    if parse_date(date_from):
        movements = movements.filter(movement_date__date__gte=date_from)
    if parse_date(date_to):
        movements = movements.filter(movement_date__date__lte=date_to)

    return movements.order_by('-movement_date'), {
        'part_id': part_id,
        'movement_type': movement_type,
        'date_from': date_from,
        'date_to': date_to,
    }


@login_required
def report_stock_movements(request):
    """Журнал движений запчастей."""
    movements, filter_context = _filter_movements(request)

    paginator = Paginator(movements, 50)
    page = request.GET.get('page')

    parts_list = SparePart.objects.all().order_by('part_number')
    return render(request, 'core/reports/stock_movements.html', {
        'movements': paginator.get_page(page),
        'parts_list': parts_list,
        'movement_types': StockMovement.MOVEMENT_TYPE_CHOICES,
        **filter_context,
    })


@login_required
def report_stock_movements_export(request):
    """Журнал движений в Excel — с учётом фильтров, выставленных на странице."""
    movements, _ = _filter_movements(request)

    headers = [
        'Дата', 'Артикул', 'Название', 'Тип', 'Количество',
        'Цена, ₽', 'Сумма, ₽',
        'Документ', 'Заказ', 'Сотрудник', 'Примечания',
    ]
    rows = [
        [
            excel_datetime(m.movement_date),
            m.part.part_number, m.part.name,
            m.get_movement_type_display(), m.quantity,
            m.unit_price, m.total_price,
            m.document_number,
            m.repair_order.order_number if m.repair_order else '',
            m.created_by.full_name if m.created_by else '',
            m.notes,
        ]
        for m in movements
    ]
    wb = build_workbook('Движения', headers, rows)
    return xlsx_response(wb, f'Журнал движений {timezone.localdate():%Y-%m-%d}.xlsx')


def _debtor_orders():
    """Неоплаченные и частично оплаченные заказы — общее для отчёта и выгрузки.

    Условие «должник» живёт в `RepairOrderQuerySet`: то же самое нужно
    напоминаниям, и расходиться эти определения не должны.
    """
    return (
        RepairOrder.objects.with_debt()
        .select_related('client')
        .prefetch_related('order_equipments__equipment__model')
        .order_by('-date_received')
    )


def _total_debt(orders):
    """Сумма долга одним запросом.

    Складываем не стоимости, а остатки: часть денег заказчик мог уже внести,
    и итог из полных стоимостей завышал бы задолженность.

    Стоимости и оплаты берутся подзапросами, а не двумя Sum по соединениям:
    соединение размножило бы строки, и заказ с тремя платежами попал бы
    в итог трижды.
    """
    totals = orders.aggregate(
        cost=Sum(_order_cost_subquery()),
        paid=Sum(_order_paid_subquery()),
    )
    return (totals['cost'] or 0) - (totals['paid'] or 0)


def _order_cost_subquery():
    return Coalesce(
        Subquery(
            RepairOrderEquipment.objects
            .filter(repair_order=OuterRef('pk'))
            .values('repair_order').annotate(total=Sum('repair_cost')).values('total')[:1]
        ),
        Value(Decimal('0')),
        output_field=DecimalField(max_digits=12, decimal_places=2),
    )


def _order_paid_subquery():
    return Coalesce(
        Subquery(
            Payment.objects
            .filter(repair_order=OuterRef('pk'))
            .values('repair_order').annotate(total=Sum('amount')).values('total')[:1]
        ),
        Value(Decimal('0')),
        output_field=DecimalField(max_digits=12, decimal_places=2),
    )


@login_required
def report_debtors(request):
    """Задолженности по заказам."""
    orders = _debtor_orders()
    total_debt = _total_debt(orders)

    # Когда заказчику в последний раз напоминали. Без этой колонки на вопрос
    # «мы им вообще писали?» отвечать нечем, кроме как листать очередь
    orders = orders.annotate(
        last_reminder=Max(
            'notifications__created_at',
            filter=Q(notifications__event='debt_reminder'),
        )
    )

    return render(request, 'core/reports/debtors.html', {
        'orders': orders,
        'total_debt': total_debt,
        'overdue_days': getattr(settings, 'DEBT_OVERDUE_DAYS', 14),
    })


@login_required
def report_debtors_export(request):
    """Задолженности в Excel — для сверки с бухгалтерией."""
    orders = _debtor_orders()

    headers = [
        '№ заказа', 'Дата приёма', 'Заказчик', 'ИНН', 'Оборудование',
        'Статус ремонта', 'Статус оплаты', '№ счёта', 'Дата счёта',
        'Сумма, ₽', 'Оплачено, ₽', 'Остаток, ₽',
    ]
    rows = [
        [
            order.order_number,
            excel_datetime(order.date_received),
            order.client.name,
            order.client.inn,
            ', '.join(str(roe.equipment) for roe in order.order_equipments.all()),
            order.get_status_display(),
            order.get_payment_status_display(),
            order.invoice_number,
            order.invoice_date,
            order.total_repair_cost,
            order.paid_amount,
            order.debt,
        ]
        for order in orders
    ]
    # Итог в самой книге: иначе бухгалтеру пришлось бы складывать столбец
    # вручную, а именно эта цифра из отчёта и нужна
    if rows:
        rows.append(['Итого'] + [''] * (len(headers) - 2) + [_total_debt(orders)])

    wb = build_workbook('Задолженности', headers, rows)
    return xlsx_response(wb, f'Задолженности {timezone.localdate():%Y-%m-%d}.xlsx')


# ==================== Аналитика по срокам и загрузке ремонта ====================
#
# У заказа нет поля «ответственный инженер» — есть только автор смены
# статуса (`OrderStatusHistory.changed_by`). Везде ниже «инженер» — это
# суррогат по этому автору, а не отдельно назначенный человек. Если
# в модель когда-нибудь добавят настоящее поле ответственного, всю эту
# группу функций нужно будет заменить на использование поля напрямую,
# а не достраивать поверх суррогата.

TERMINAL_STATUSES = ('shipped', 'unrepairable')


def _repair_analytics_period(request):
    """Диапазон дат отчёта — по умолчанию последние 30 дней."""
    default_to = timezone.localdate()
    default_from = default_to - timedelta(days=30)
    date_from = parse_date(request.GET.get('date_from', '')) or default_from
    date_to = parse_date(request.GET.get('date_to', '')) or default_to
    return date_from, date_to


def _completed_orders_in_period(date_from, date_to):
    """Заказы, завершённые (отгружены или признаны неремонтопригодными)
    в периоде — {id заказа: запись истории о завершении}.

    Момент завершения берём из истории статусов, а не из полей заказа
    (`date_completed` / `shipping_date`): они не всегда заполнены
    единообразно для обоих терминальных статусов, а история переходов —
    общий надёжный источник для обоих случаев. Если заказ побывал
    в терминальном статусе несколько раз, берётся самая свежая запись.
    """
    start = timezone.make_aware(datetime.combine(date_from, time.min))
    end = timezone.make_aware(datetime.combine(date_to, time.max))
    entries = (
        OrderStatusHistory.objects
        .filter(status__in=TERMINAL_STATUSES, changed_at__gte=start, changed_at__lte=end)
        .order_by('order_id', '-changed_at')
    )
    latest = {}
    for entry in entries:
        latest.setdefault(entry.order_id, entry)
    return latest


def _order_assignees(order_ids):
    """Сотрудник-суррогат «ответственного инженера» для каждого заказа
    из `order_ids` — {id заказа: id сотрудника}, без записи для заказов
    без суррогата.

    Правило (см. заголовок раздела): тот, кто последним перевёл заказ
    в статус «Ремонт»; если такого перехода не было — тот, кто последним
    перевёл в «Диагностика» (ремонт признан невозможным без попытки
    чинить). Если нет и такого перехода — у заказа нет персонального
    инженера в этой статистике, но он участвует в общих средних без
    разбивки по человеку.
    """
    if not order_ids:
        return {}
    entries = (
        OrderStatusHistory.objects
        .filter(order_id__in=order_ids, status__in=('repair', 'diagnostic'),
                changed_by__isnull=False)
        .order_by('changed_at')
        .values('order_id', 'status', 'changed_by_id')
    )
    repair_by_order = {}
    diagnostic_by_order = {}
    for entry in entries:
        # Записи идут по возрастанию времени, так что более позднее
        # присвоение в словарь всегда затирает более раннее — в итоге
        # остаётся именно последний переход в каждый из двух статусов
        target = repair_by_order if entry['status'] == 'repair' else diagnostic_by_order
        target[entry['order_id']] = entry['changed_by_id']
    return {
        order_id: repair_by_order[order_id] if order_id in repair_by_order
        else diagnostic_by_order[order_id]
        for order_id in order_ids
        if order_id in repair_by_order or order_id in diagnostic_by_order
    }


def _order_fault_types(order_ids):
    """{id заказа: {id типовой неисправности: название}} — только для
    единиц оборудования, где неисправность выбрана из справочника
    (Этап 3, `RepairOrderEquipment.faults`). Заказ, где указан только
    свободный текст («Другое»), в разбивку по типу не попадает."""
    if not order_ids:
        return {}
    result = defaultdict(dict)
    roe_qs = (
        RepairOrderEquipment.objects
        .filter(repair_order_id__in=order_ids)
        .prefetch_related('faults')
    )
    for roe in roe_qs:
        for fault in roe.faults.all():
            result[roe.repair_order_id][fault.id] = str(fault)
    return result


def _repair_analytics_data(date_from, date_to, viewer=None):
    """Показатели отчёта за период.

    `viewer=None` — полный отчёт по всем сотрудникам (только для роли
    `admin`). `viewer=<Employee>` — все показатели ограничены заказами,
    где этот сотрудник — инженер-суррогат (см. `_order_assignees`);
    у остальных ролей своих коллег в отчёте не видно вовсе.
    """
    completions = _completed_orders_in_period(date_from, date_to)
    order_ids = list(completions.keys())
    orders = {
        o.pk: o for o in
        RepairOrder.objects.filter(pk__in=order_ids).only('pk', 'date_received')
    }
    assignees = _order_assignees(order_ids)

    if viewer is not None:
        order_ids = [oid for oid in order_ids if assignees.get(oid) == viewer.pk]

    fault_types = _order_fault_types(order_ids)

    durations = []
    by_employee = defaultdict(list)
    by_fault = defaultdict(list)
    fault_names = {}

    for order_id in order_ids:
        order = orders.get(order_id)
        if order is None or order.date_received is None:
            continue
        completion = completions[order_id]
        duration = (completion.changed_at - order.date_received).total_seconds() / 86400
        durations.append(duration)

        assignee_id = assignees.get(order_id)
        if assignee_id:
            by_employee[assignee_id].append(duration)

        for fault_id, name in fault_types.get(order_id, {}).items():
            by_fault[fault_id].append(duration)
            fault_names[fault_id] = name

    def _avg(values):
        return round(sum(values) / len(values), 1) if values else None

    employees = {e.pk: e for e in Employee.objects.filter(pk__in=by_employee.keys())}
    by_employee_rows = sorted(
        (
            {'employee': employees[emp_id], 'orders': len(vals), 'avg_days': _avg(vals)}
            for emp_id, vals in by_employee.items() if emp_id in employees
        ),
        key=lambda row: row['employee'].full_name,
    )
    by_fault_rows = sorted(
        (
            {'fault_type': fault_names[fault_id], 'orders': len(vals), 'avg_days': _avg(vals)}
            for fault_id, vals in by_fault.items()
        ),
        key=lambda row: row['fault_type'],
    )

    return {
        'total_orders': len(durations),
        'avg_days': _avg(durations),
        'by_employee': by_employee_rows,
        'by_fault_type': by_fault_rows,
    }


def _current_load():
    """Текущая загрузка — {id сотрудника: число открытых заказов сейчас}.

    «Открытый» — статус из `RepairOrder.OPEN_STATUSES`. Заказ считается
    закреплённым за сотрудником, который последним оставил запись в его
    истории статусов, независимо от того, что именно эта запись меняла
    (статус ремонта, статус оплаты или ничего из этого): кто последним
    трогал заказ, тот сейчас его и ведёт. Тот же суррогат «ответственного
    инженера», что и у остальных функций этого раздела, только по
    другому правилу — здесь неважно, в какой статус был переход.
    """
    open_ids = list(RepairOrder.objects.open().values_list('pk', flat=True))
    if not open_ids:
        return Counter()
    entries = (
        OrderStatusHistory.objects
        .filter(order_id__in=open_ids)
        .order_by('order_id', '-changed_at')
        .values('order_id', 'changed_by_id')
    )
    latest_author = {}
    for entry in entries:
        latest_author.setdefault(entry['order_id'], entry['changed_by_id'])
    counts = Counter(latest_author.values())
    counts.pop(None, None)
    return counts


@login_required
def report_repair_analytics(request):
    """Аналитика по срокам ремонта и загрузке инженеров.

    Роль `admin` видит показатели по всем сотрудникам; любая другая роль —
    только свои собственные (не отчёт целиком и не показатели коллег).
    «Инженер» здесь — суррогат, см. докстринг раздела выше.
    """
    date_from, date_to = _repair_analytics_period(request)
    is_admin = request.user.role == 'admin'
    viewer = None if is_admin else request.user

    data = _repair_analytics_data(date_from, date_to, viewer=viewer)

    load_counts = _current_load()
    if is_admin:
        load_rows = sorted(
            (
                {'employee': e, 'count': load_counts.get(e.pk, 0)}
                for e in Employee.objects.filter(is_active=True)
            ),
            key=lambda row: (-row['count'], row['employee'].full_name),
        )
    else:
        load_rows = [{'employee': request.user, 'count': load_counts.get(request.user.pk, 0)}]

    return render(request, 'core/reports/repair_analytics.html', {
        'date_from': date_from.isoformat(),
        'date_to': date_to.isoformat(),
        'is_admin': is_admin,
        'total_orders': data['total_orders'],
        'avg_days': data['avg_days'],
        'by_employee': data['by_employee'],
        'by_fault_type': data['by_fault_type'],
        'load_rows': load_rows,
    })


@login_required
def report_repair_analytics_export(request):
    """Та же аналитика в Excel — тремя листами (по инженерам, по типу
    неисправности, по текущей загрузке). Права доступа те же, что
    и у страницы отчёта."""
    date_from, date_to = _repair_analytics_period(request)
    is_admin = request.user.role == 'admin'
    viewer = None if is_admin else request.user

    data = _repair_analytics_data(date_from, date_to, viewer=viewer)

    load_counts = _current_load()
    if is_admin:
        load_rows = [
            (e.full_name, load_counts.get(e.pk, 0))
            for e in Employee.objects.filter(is_active=True).order_by('full_name')
        ]
    else:
        load_rows = [(request.user.full_name, load_counts.get(request.user.pk, 0))]

    wb = build_workbook(
        'По инженерам',
        ['Сотрудник', 'Заказов', 'Среднее время ремонта, дней'],
        [
            [row['employee'].full_name, row['orders'], row['avg_days']]
            for row in data['by_employee']
        ],
    )
    add_sheet(
        wb, 'По типу неисправности',
        ['Тип неисправности', 'Заказов', 'Среднее время ремонта, дней'],
        [
            [row['fault_type'], row['orders'], row['avg_days']]
            for row in data['by_fault_type']
        ],
    )
    add_sheet(
        wb, 'Текущая загрузка',
        ['Сотрудник', 'Открытых заказов'],
        [[name, count] for name, count in load_rows],
    )

    return xlsx_response(wb, f'Аналитика ремонта {timezone.localdate():%Y-%m-%d}.xlsx')


# ==================== ПРИБЫЛЬ ПО ЗАКАЗАМ ====================

def _profit_period(request):
    """Диапазон дат отчёта о прибыли — по умолчанию последние 30 дней,
    как и у аналитики ремонта (`_repair_analytics_period`)."""
    default_to = timezone.localdate()
    default_from = default_to - timedelta(days=30)
    date_from = parse_date(request.GET.get('date_from', '')) or default_from
    date_to = parse_date(request.GET.get('date_to', '')) or default_to
    return date_from, date_to


def _profit_data(date_from, date_to):
    """Выручка, себестоимость деталей и прибыль за период — итог и разбивка
    по заказчику.

    Выручка считается по дате поступления денег (`Payment.payment_date`),
    себестоимость деталей — по дате её записи (`OrderCost.created_at`,
    заводится в момент списания детали заказа, см. `_use_repair_order_part`).
    Это два независимых события одного заказа, и они не обязаны попасть
    в один период — платёж и списание детали по одному заказу обычно
    и происходят в разные дни; отчёт складывает то, что случилось
    в периоде, для каждого из событий по отдельности, а не подбирает
    себестоимость под период платежа или наоборот.

    Себестоимость известна не всегда (`OrderCost.amount` может быть пустым
    — см. модель). Известная и неизвестная часть считаются раздельно, как
    и в плане закупок (`_purchase_plan_totals`): сумма по известным записям
    плюс отдельно количество записей без суммы. Прибыль здесь — прибыль
    по известной себестоимости; если неизвестных записей в периоде нет,
    она точна, если есть — реальная прибыль ниже показанной на неизвестную
    часть, и это явно видно по count'у, а не скрыто молчаливым `None`
    на весь отчёт (в отличие от `RepairOrder.parts_cost` для одного заказа,
    где именно такое молчаливое скрытие и было бы вводящим в заблуждение).
    """
    payments = Payment.objects.filter(payment_date__gte=date_from, payment_date__lte=date_to)
    costs = OrderCost.objects.filter(
        category='parts', created_at__date__gte=date_from, created_at__date__lte=date_to,
    )

    revenue_total = payments.aggregate(total=Sum('amount'))['total'] or Decimal('0')
    known_cost_total = costs.filter(amount__isnull=False).aggregate(
        total=Sum('amount'))['total'] or Decimal('0')
    unknown_cost_count = costs.filter(amount__isnull=True).count()

    by_client = defaultdict(lambda: {
        'revenue': Decimal('0'), 'known_cost': Decimal('0'), 'unknown_cost_count': 0,
    })
    client_names = {}

    for payment in payments.select_related('repair_order__client'):
        client = payment.repair_order.client
        by_client[client.pk]['revenue'] += payment.amount
        client_names[client.pk] = client.name

    for cost in costs.select_related('repair_order__client'):
        client = cost.repair_order.client
        if cost.amount is None:
            by_client[client.pk]['unknown_cost_count'] += 1
        else:
            by_client[client.pk]['known_cost'] += cost.amount
        client_names[client.pk] = client.name

    rows = sorted(
        (
            {
                'client': client_names[client_id],
                'revenue': values['revenue'],
                'known_cost': values['known_cost'],
                'unknown_cost_count': values['unknown_cost_count'],
                'profit': values['revenue'] - values['known_cost'],
            }
            for client_id, values in by_client.items()
        ),
        key=lambda row: row['client'],
    )

    return {
        'revenue_total': revenue_total,
        'known_cost_total': known_cost_total,
        'unknown_cost_count': unknown_cost_count,
        'profit_total': revenue_total - known_cost_total,
        'rows': rows,
    }


@role_required('accountant')
def report_profit(request):
    """Прибыль по заказам за период: поступившие деньги минус себестоимость
    деталей, списанных по конкретным партиям прихода (не по средней
    и не по текущей цене детали), плюс разбивка по заказчику.

    Права — только `accountant` (`admin` проходит декоратором всегда), как
    у остальных финансовых разделов (`repair_order_add_payment`,
    `bank_operations`): это деньги заказа, складу и мастеру они не нужны.
    """
    date_from, date_to = _profit_period(request)
    data = _profit_data(date_from, date_to)
    return render(request, 'core/reports/profit.html', {
        'date_from': date_from.isoformat(),
        'date_to': date_to.isoformat(),
        **data,
    })


@role_required('accountant')
def report_profit_export(request):
    """Тот же отчёт в Excel — двумя листами (итог за период, разбивка
    по заказчику). Права доступа те же, что и у страницы отчёта."""
    date_from, date_to = _profit_period(request)
    data = _profit_data(date_from, date_to)

    wb = build_workbook(
        'Итог за период',
        ['Показатель', 'Значение'],
        [
            ['Выручка (поступившие платежи), ₽', data['revenue_total']],
            ['Себестоимость деталей, известная часть, ₽', data['known_cost_total']],
            ['Списаний деталей без известной себестоимости, шт.', data['unknown_cost_count']],
            ['Прибыль по известной себестоимости, ₽', data['profit_total']],
        ],
    )
    add_sheet(
        wb, 'По заказчикам',
        ['Заказчик', 'Выручка, ₽', 'Себестоимость (известная), ₽',
         'Списаний без цены, шт.', 'Прибыль (известная), ₽'],
        [
            [row['client'], row['revenue'], row['known_cost'],
             row['unknown_cost_count'], row['profit']]
            for row in data['rows']
        ],
    )
    return xlsx_response(wb, f'Прибыль {date_from:%Y-%m-%d} - {date_to:%Y-%m-%d}.xlsx')


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

    # Похожий серийник — не ошибка, а повод переспросить: «БУАД-1234» и
    # «буад 1234» это, скорее всего, одна и та же единица, набранная разными
    # людьми по-разному. Решает человек: если создать вторую запись молча,
    # история ремонтов разъедется на две и найти её будет нечем.
    if request.POST.get('confirmed') != '1':
        similar = Equipment.find_similar(serial_number)
        if similar.exists():
            return JsonResponse({
                'success': False,
                'similar': [
                    {
                        'id': eq.id,
                        'name': str(eq),
                        'serial_number': eq.serial_number,
                        'orders_count': eq.repair_orders.count(),
                        'history_url': reverse('equipment_history', args=[eq.id]),
                    }
                    for eq in similar[:5]
                ],
            })

    equipment = Equipment.objects.create(model=model, serial_number=serial_number)

    return JsonResponse({
        'success': True,
        'id': equipment.id,
        'name': str(equipment),
        'message': f'Оборудование "{equipment}" создано'
    })


@login_required
def ajax_equipment_history_summary(request, pk):
    """Краткая сводка прошлых ремонтов — для подсказки в форме заказа."""
    equipment = get_object_or_404(Equipment.objects.select_related('model'), pk=pk)
    visits = equipment.repair_history()
    count = visits.count()
    last = visits.first()

    # Гарантия — самое важное, что можно сказать при приёме: если единица
    # вернулась в её пределах, это повторное обращение по тому же ремонту,
    # и решать по оплате нужно до того, как заказ оформлен
    warranty = equipment.active_warranty()

    return JsonResponse({
        'equipment': str(equipment),
        'orders_count': count,
        'last_order_number': last.repair_order.order_number if last else '',
        'last_date': (
            timezone.localtime(last.repair_order.date_received).strftime('%d.%m.%Y')
            if last else ''
        ),
        'history_url': reverse('equipment_history', args=[equipment.pk]),
        'under_warranty': warranty is not None,
        'warranty_until': (
            timezone.localtime(warranty.warranty_until).strftime('%d.%m.%Y')
            if warranty else ''
        ),
        'warranty_order_number': warranty.repair_order.order_number if warranty else '',
        'warranty_order_url': (
            reverse('repair_order_detail', args=[warranty.repair_order_id])
            if warranty else ''
        ),
    })


@login_required
def ajax_equipment_faults(request, pk):
    """Типовые неисправности модели этой единицы оборудования — для выбора
    в строке заказа. Список зависит от того, какое оборудование выбрано
    в конкретной строке, и меняется вместе с ним, поэтому тянется отдельным
    запросом, а не единым списком по всем моделям сразу."""
    equipment = get_object_or_404(Equipment.objects.select_related('model'), pk=pk)
    faults = FaultType.objects.filter(equipment_model_id=equipment.model_id).order_by('name')
    return JsonResponse({
        'success': True,
        'faults': [{'id': fault.id, 'name': fault.name} for fault in faults],
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






@role_required('admin')
def admin_notifications(request):
    """Очередь оповещений: что ушло, что не ушло и почему.

    Без этой страницы на вопрос «почему заказчик не получил письмо» отвечать
    нечем: очередь живёт в базе, а журнал отправки — в логах systemd.
    """
    status = request.GET.get('status', '')
    queue = Notification.objects.select_related('repair_order', 'part')
    if status in dict(Notification.STATUS_CHOICES):
        queue = queue.filter(status=status)

    paginator = Paginator(queue.order_by('-created_at'), 50)
    page_obj = paginator.get_page(request.GET.get('page'))

    counts = {
        item['status']: item['count']
        for item in Notification.objects.values('status').annotate(count=Count('id'))
    }
    status_tabs = [
        {'code': code, 'label': label, 'count': counts.get(code, 0)}
        for code, label in Notification.STATUS_CHOICES
    ]

    return render(request, 'core/admin/notifications.html', {
        'notifications': page_obj,
        'status_filter': status,
        'status_tabs': status_tabs,
        'found_count': paginator.count,
        'sending_enabled': getattr(settings, 'NOTIFICATIONS_ENABLED', False),
        'clients_enabled': getattr(settings, 'NOTIFY_CLIENTS', False),
        'low_stock_enabled': getattr(settings, 'NOTIFY_LOW_STOCK', True),
        'channels': [
            {
                'name': 'MAX',
                'configured': messengers.max_is_configured(),
                'enabled': getattr(settings, 'NOTIFY_MAX', False)
                           and messengers.max_is_configured(),
            },
            {
                'name': 'Telegram',
                'configured': messengers.telegram_is_configured(),
                'enabled': getattr(settings, 'NOTIFY_TELEGRAM', False)
                           and messengers.telegram_is_configured(),
            },
        ],
    })


@role_required('admin')
@require_POST
def admin_notification_retry(request, pk):
    """Вернуть неудачное оповещение в очередь.

    Счётчик попыток обнуляется: обычно причина неудачи — не письмо, а почта
    (пароль приложения, отвалившийся канал), и после починки прошлые попытки
    ни о чём не говорят.
    """
    notification = get_object_or_404(Notification, pk=pk)
    notification.status = 'pending'
    notification.attempts = 0
    notification.last_error = ''
    notification.save(update_fields=['status', 'attempts', 'last_error'])

    messages.success(request, f'Оповещение для {notification.recipient} возвращено в очередь')
    return redirect('admin_notifications')


# ==================== ОБНОВЛЕНИЕ ПРИЛОЖЕНИЯ ====================

@role_required('admin')
def admin_update(request):
    """Страница обновления. Само обновление выполняет служба systemd от root —
    приложение лишь оставляет заявку (подробности в core/updater.py)."""
    if not updater.is_git_checkout():
        return render(request, 'core/admin/update.html', {'not_a_repo': True})

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'check':
            ok, output = updater.fetch_remote()
            if ok:
                count = len(updater.pending_updates())
                messages.success(
                    request,
                    f'Доступно обновлений: {count}' if count else 'Установлена последняя версия'
                )
            else:
                messages.error(request, f'Не удалось проверить обновления: {output}')

        elif action in ('update', 'rollback'):
            if updater.update_in_progress():
                messages.warning(request, 'Обновление уже выполняется')
            else:
                target = 'latest' if action == 'update' else request.POST.get('target', '')
                try:
                    updater.request_update(target, requested_by=request.user.username)
                except ValueError as exc:
                    messages.error(request, str(exc))
                else:
                    messages.info(request, 'Обновление запущено, приложение перезапустится')
        return redirect('admin_update')

    return render(request, 'core/admin/update.html', {
        'current': updater.current_version(),
        'pending': updater.pending_updates(),
        'history': updater.recent_history(),
        'status': updater.read_status(),
    })


@role_required('admin')
def admin_update_status(request):
    """Ход обновления для опроса со страницы."""
    return JsonResponse(updater.read_status() or {'state': 'idle', 'message': '', 'log': []})


# ==================== ПЕЧАТНЫЕ АКТЫ ====================

# Печать через браузер, а не сборка PDF на сервере: диалог печати сохраняет
# в PDF сам, а библиотека рендеринга на Raspberry Pi — это лишние зависимости
# и лишние мегабайты ради того, что уже умеет каждый браузер.

def _act_context(pk):
    """Общее для обоих актов: заказ, оборудование и шапка с реквизитами."""
    order = get_object_or_404(
        RepairOrder.objects.select_related('client'), pk=pk
    )
    return {
        'order': order,
        'organization': Organization.get_solo(),
        'order_equipments': list(
            order.order_equipments.select_related('equipment__model').order_by('id')
        ),
        'today': timezone.localdate(),
    }


@login_required
def repair_order_act_receive(request, pk):
    """Акт приёма оборудования в ремонт.

    Печатается, когда оборудование привезли: в нём пломбы и состояние
    на момент приёма — то, о чём потом спорят, если что-то не так.
    """
    return render(request, 'core/repair_orders/act_receive.html', _act_context(pk))


@login_required
def repair_order_act_complete(request, pk):
    """Акт выполненных работ.

    Печатается при выдаче: что сделали, сколько стоит, до какого числа
    действует гарантия.
    """
    context = _act_context(pk)
    order = context['order']
    context['details'] = list(order.details.select_related('part'))
    context['warranty_until'] = (
        add_months(order.date_completed, warranty_months())
        if order.date_completed and warranty_months() else None
    )
    return render(request, 'core/repair_orders/act_complete.html', context)


def _order_equipment(order_pk, roe_pk):
    """Единица оборудования в заказе — с проверкой, что она из этого заказа.

    Иначе по подобранному адресу открылась бы чужая позиция, и акт ушёл бы
    заказчику с серийником из другого заказа.
    """
    return get_object_or_404(
        RepairOrderEquipment.objects.select_related(
            'equipment__model', 'repair_order__client'
        ),
        pk=roe_pk, repair_order_id=order_pk,
    )


@login_required
def repair_order_defect_act_edit(request, order_pk, roe_pk):
    """Заполнение акта дефектации по одной единице оборудования."""
    order_equipment = _order_equipment(order_pk, roe_pk)
    if request.method == 'POST':
        form = DefectActForm(request.POST, instance=order_equipment)
        if form.is_valid():
            form.save()
            messages.success(request, 'Акт дефектации сохранён')
            return redirect('repair_order_act_defect', order_pk=order_pk, roe_pk=roe_pk)
        messages.error(request, 'Акт не сохранён: проверьте отмеченные поля')
    else:
        form = DefectActForm(instance=order_equipment)
    return render(request, 'core/repair_orders/defect_act_form.html', {
        'form': form,
        'order': order_equipment.repair_order,
        'order_equipment': order_equipment,
    })


@login_required
def repair_order_act_defect(request, order_pk, roe_pk):
    """Акт дефектации оборудования.

    Печатается по итогам диагностики: что нашли внутри, какие коды ошибок
    в памяти устройства, гарантийный ли случай и во сколько обойдётся
    ремонт. Один акт на одну единицу — заказчик по нему решает, чинить ли,
    а решает он по каждой единице отдельно.
    """
    order_equipment = _order_equipment(order_pk, roe_pk)
    return render(request, 'core/repair_orders/act_defect.html', {
        'order': order_equipment.repair_order,
        'order_equipment': order_equipment,
        'organization': Organization.get_solo(),
        'act_date': order_equipment.defect_act_date or timezone.localdate(),
    })


@login_required
def repair_order_quote_edit(request, pk):
    """Условия коммерческого предложения и строки по единицам."""
    order = get_object_or_404(RepairOrder.objects.select_related('client'), pk=pk)

    if request.method == 'POST':
        form = QuoteForm(request.POST, instance=order)
        formset = QuoteLineFormSet(request.POST, instance=order)
        if form.is_valid() and formset.is_valid():
            form.save()
            formset.save()
            messages.success(request, 'Коммерческое предложение сохранено')
            return redirect('repair_order_quote', pk=order.pk)
        messages.error(request, 'Предложение не сохранено: проверьте отмеченные поля')
    else:
        today = timezone.localdate()
        form = QuoteForm(instance=order, initial={
            'quote_date': order.quote_date or today,
            'quote_valid_until': order.quote_valid_until or today + timedelta(
                days=getattr(settings, 'QUOTE_VALID_DAYS', 14)),
        })
        formset = QuoteLineFormSet(instance=order)

    return render(request, 'core/repair_orders/quote_form.html', {
        'order': order,
        'form': form,
        'formset': formset,
    })


@login_required
def repair_order_quote(request, pk):
    """Коммерческое предложение на А4.

    Печатается по итогам дефектации: что предлагаем сделать, во сколько
    обойдётся, в какие сроки и на каких условиях.
    """
    order = get_object_or_404(RepairOrder.objects.select_related('client'), pk=pk)
    return render(request, 'core/repair_orders/quote.html', {
        'order': order,
        'organization': Organization.get_solo(),
        'rows': order.quote_rows(),
        'total': order.quote_total,
        'quote_date': order.quote_date or timezone.localdate(),
    })


# ==================== СЧЁТ ЧЕРЕЗ API Т-БАНКА ====================

# Единственное место в программе, которое что-то создаёт в банке. Поэтому:
# отправка только по нажатию человека, только со страницы, где он видит
# всё, что уйдёт, и только при включённом TBANK_INVOICE_ENABLED.
# По расписанию счета не выставляются нигде и никогда.

def _invoice_payload(order, form_data):
    """Тело запроса к банку — то самое, что показано на странице."""
    client = order.client
    return tbank.build_invoice(
        number=form_data['invoice_number'],
        items=order.invoice_items(),
        payer={'name': client.name, 'inn': client.inn, 'kpp': client.kpp},
        emails=form_data['emails'],
        invoice_date=form_data['invoice_date'],
        due_date=form_data['due_date'],
    )


@role_required('accountant')
def repair_order_invoice(request, pk):
    """Выставление счёта заказчику через API Т-Банка.

    GET показывает, что именно уйдёт в банк; POST отправляет. Разделение
    не формальность: счёт уходит заказчику, и «подтверждаю» должно означать
    согласие с тем, что человек видел, а не с тем, что программа придумала.
    """
    order = get_object_or_404(RepairOrder.objects.select_related('client'), pk=pk)
    items = order.invoice_items()
    default_emails = order.client.email

    if request.method == 'POST':
        form = InvoiceSendForm(request.POST)
        if form.is_valid():
            return _send_invoice(request, order, form)
    else:
        today = timezone.localdate()
        form = InvoiceSendForm(initial={
            'invoice_number': order.invoice_number or RepairOrder.next_invoice_number(),
            'invoice_date': today,
            'due_date': today + timedelta(
                days=getattr(settings, 'TBANK_INVOICE_DUE_DAYS', 14)),
            'emails': default_emails,
        })

    return render(request, 'core/bank/invoice.html', {
        'order': order,
        'form': form,
        'items': items,
        'items_total': sum(Decimal(str(item['price'])) for item in items),
        'enabled': tbank.invoice_enabled(),
        'configured': tbank.is_configured(),
    })


def _send_invoice(request, order, form):
    """Отправка счёта в банк и запись следов в заказе."""
    payload = _invoice_payload(order, form.cleaned_data)

    try:
        response = tbank.send_invoice(payload)
    except tbank.TBankError as exc:
        # Ошибку храним в заказе, а не только в сообщении на экране:
        # человек уйдёт со страницы, а причина отказа понадобится потом
        order.tbank_invoice_error = str(exc)[:500]
        order.save(update_fields=['tbank_invoice_error'])
        messages.error(request, f'Счёт не выставлен. {exc}')
        return redirect('repair_order_invoice', pk=order.pk)

    order.invoice_number = form.cleaned_data['invoice_number']
    order.invoice_date = form.cleaned_data['invoice_date']
    order.tbank_invoice_sent_at = timezone.now()
    order.tbank_invoice_pdf_url = tbank.invoice_pdf_url(response)
    order.tbank_invoice_error = ''
    order.save(update_fields=[
        'invoice_number', 'invoice_date', 'tbank_invoice_sent_at',
        'tbank_invoice_pdf_url', 'tbank_invoice_error',
    ])

    OrderStatusHistory.objects.create(
        order=order,
        changed_by=request.user,
        notes=f'Выставлен счёт № {order.invoice_number} через Т-Банк'
              + (f', отправлен: {", ".join(form.cleaned_data["emails"])}'
                 if form.cleaned_data['emails'] else ' (без отправки по почте)'),
    )

    messages.success(request, f'Счёт № {order.invoice_number} выставлен')
    return redirect('repair_order_detail', pk=order.pk)


# ==================== ПОСТУПЛЕНИЯ ИЗ БАНКА ====================

# Выписка только читается, деньги по заказам разносит человек. Автоматика
# здесь ограничена подсказкой: ошибочно разнесённое поступление ищут потом
# неделю, а нажать кнопку — секунда.

@role_required('accountant')
def bank_operations(request):
    """Поступления из выписки Т-Банка и подсказки, к каким они заказам."""
    status = request.GET.get('status', 'new')
    if status not in dict(BankOperation.STATUS_CHOICES):
        status = 'new'

    operations = list(
        BankOperation.objects.filter(status=status)
        .select_related('payment__repair_order', 'processed_by')[:200]
    )
    # Подсказки считаем только для неразнесённых: у разнесённых заказ уже
    # известен, и лишние запросы к базе там ни к чему
    rows = [
        {'operation': operation,
         'suggestions': operation.guess_orders()[:5] if status == 'new' else []}
        for operation in operations
    ]

    counts = dict(
        BankOperation.objects.values_list('status')
        .annotate(total=Count('id')).values_list('status', 'total')
    )

    return render(request, 'core/bank/operations.html', {
        'rows': rows,
        'status': status,
        'tabs': [
            {'value': value, 'label': label, 'count': counts.get(value, 0)}
            for value, label in BankOperation.STATUS_CHOICES
        ],
        'configured': tbank.is_configured(),
    })


@role_required('accountant')
@require_POST
def bank_operation_apply(request, pk):
    """Записать поступление оплатой по выбранному заказу."""
    operation = get_object_or_404(BankOperation, pk=pk)
    if operation.status == 'applied':
        messages.warning(request, 'Это поступление уже разнесено')
        return redirect('bank_operations')

    order = get_object_or_404(RepairOrder, pk=request.POST.get('order') or 0)

    with transaction.atomic():
        payment = Payment.objects.create(
            repair_order=order,
            amount=operation.amount,
            payment_date=operation.operation_date or timezone.localdate(),
            note=_bank_payment_note(operation),
            created_by=request.user,
        )
        operation.payment = payment
        operation.status = 'applied'
        operation.processed_by = request.user
        operation.processed_at = timezone.now()
        operation.save(update_fields=['payment', 'status', 'processed_by', 'processed_at'])

    order.refresh_from_db()
    OrderStatusHistory.objects.create(
        order=order,
        payment_status=order.payment_status,
        changed_by=request.user,
        notes=f'Разнесено поступление из банка {operation.amount_text} ₽'
              + (f' от {operation.counterparty}' if operation.counterparty else ''),
    )

    messages.success(
        request,
        f'{operation.amount_text} ₽ разнесены на {order.order_number}. '
        + (f'Остаток: {order.debt} ₽' if order.debt else 'Заказ оплачен полностью')
    )
    return redirect('bank_operations')


@role_required('accountant')
@require_POST
def bank_operation_skip(request, pk):
    """Пометить поступление как не относящееся к заказам.

    Возвраты, переводы между своими счетами, поступления не за ремонт —
    их не разносят, но и висеть в списке они не должны.
    """
    operation = get_object_or_404(BankOperation, pk=pk)
    if operation.status == 'applied':
        messages.error(request, 'Сначала отмените разнесение')
        return redirect('bank_operations')

    operation.status = 'skipped'
    operation.processed_by = request.user
    operation.processed_at = timezone.now()
    operation.save(update_fields=['status', 'processed_by', 'processed_at'])
    messages.success(request, 'Поступление убрано из списка')
    return redirect('bank_operations')


@role_required('accountant')
@require_POST
def bank_operation_reset(request, pk):
    """Вернуть поступление в неразнесённые.

    Созданная оплата при этом удаляется — иначе деньги остались бы
    по заказу и одновременно ждали разнесения.
    """
    operation = get_object_or_404(BankOperation, pk=pk)
    payment = operation.payment
    if payment is not None:
        # Оплата уходит — сигнал pre_delete сам вернёт поступление в «новые»
        payment.delete()
        messages.success(request, 'Разнесение отменено, оплата по заказу удалена')
    else:
        operation.status = 'new'
        operation.processed_by = None
        operation.processed_at = None
        operation.save(update_fields=['status', 'processed_by', 'processed_at'])
        messages.success(request, 'Поступление возвращено в список')
    return redirect('bank_operations')


def _bank_payment_note(operation):
    """Примечание к оплате: откуда деньги. Обрезано под длину поля."""
    parts = ['Выписка Т-Банка']
    if operation.document_number:
        parts.append(f'п/п {operation.document_number}')
    if operation.counterparty:
        parts.append(operation.counterparty)
    return ', '.join(parts)[:255]


@role_required('admin')
def admin_organization(request):
    """Реквизиты своей фирмы — шапка и подписи печатных актов."""
    organization = Organization.get_solo()
    if request.method == 'POST':
        form = OrganizationForm(request.POST, instance=organization)
        if form.is_valid():
            form.save()
            messages.success(request, 'Реквизиты сохранены')
            return redirect('admin_organization')
        messages.error(request, 'Реквизиты не сохранены: проверьте отмеченные поля')
    else:
        form = OrganizationForm(instance=organization)
    return render(request, 'core/admin/organization.html', {'form': form})


# ==================== КОРОТКИЕ АДРЕСА ДЛЯ QR-КОДОВ ====================

# Длина ссылки определяет размер QR: каждый лишний десяток символов —
# это следующая версия кода, больше модулей и мельче каждый из них.
# При печати 12 мм разница между 25 и 29 модулями означает 3,8 против
# 3,3 точек принтера на модуль, то есть уверенное чтение или пограничное.

@login_required
def short_part(request, pk):
    """Короткий адрес детали для QR: /p/<id>/"""
    get_object_or_404(SparePart, pk=pk)
    return redirect('part_detail', pk=pk)


@login_required
def short_cell(request, pk):
    """Короткий адрес ячейки для QR: /c/<id>/
    Открывает сетку на нужной кассетнице и сразу показывает содержимое ячейки."""
    cell = get_object_or_404(StorageCell, pk=pk)
    return redirect(f"{reverse('storage_cell_grid')}?cabinet={cell.cabinet.number}&open_cell={cell.pk}")


@login_required
def short_order(request, pk):
    """Короткий адрес заказа для QR: /o/<id>/

    Ведёт в карточку заказа. Номер позиции в ссылку не входит: он крупно
    напечатан на самой этикетке, а каждый лишний символ — это модули кода,
    которых на 43x25 мм и так впритык.
    """
    get_object_or_404(RepairOrder, pk=pk)
    return redirect('repair_order_detail', pk=pk)


@login_required
def short_equipment(request, pk):
    """Короткий адрес единицы оборудования для QR: /e/<id>/

    Ведёт в историю ремонтов, а не в карточку редактирования: этикетку
    сканируют, когда железка в руках и нужно вспомнить, что с ней уже
    делали и на гарантии ли она. Редактировать её в этот момент незачем.
    """
    get_object_or_404(Equipment, pk=pk)
    return redirect('equipment_history', pk=pk)


def label_base_url(request):
    """Основа для ссылок в QR-кодах.

    Берётся из LABEL_BASE_URL; на Raspberry Pi там прошит адрес в Tailscale.
    Смысл настройки в том, чтобы код не зависел от того, откуда открыли
    страницу печати: иначе две одинаковые с виду наклейки вели бы в разные
    места — напечатанная из офиса на локальный адрес, напечатанная снаружи
    на адрес Tailscale.

    Пустое значение возвращает прежнее поведение: адрес берётся из запроса.
    """
    configured = getattr(settings, 'LABEL_BASE_URL', '')
    return configured.rstrip('/') if configured else request.build_absolute_uri('/').rstrip('/')


def qr_url(base_url, prefix, pk):
    """Ссылка, которая уходит в QR-код.

    Без косой черты на конце — это ровно один символ, но на этикетке заказа
    он решает: QR там 9,6 мм, и 27-й символ переводит код с 25 модулей
    на 29, то есть с 2,8 точки принтера на модуль до 2,5, а 2,5 на практике
    уже не считывалось. Маршруты без черты заведены в urls.py.
    """
    return f'{base_url}/{prefix}/{pk}'


# Сколько символов помещается в QR, не переводя его на следующую версию.
# Границы при уровне коррекции M: до 26 символов — 25 модулей, 27–42 —
# 29 модулей, 43 и больше — 33. На этикетке заказа QR всего 9,57 мм, и
# 33 модуля дают 2,3 точки принтера на модуль — меньше, чем 2,5, которые
# на практике уже не считывались.
QR_MAX_CHARS = 42


def qr_length_warning(urls):
    """Предупреждение, если ссылка не помещается в QR нужного размера.

    Считается по факту, а не по одной настройке: длина складывается из
    LABEL_BASE_URL и номера записи, и вторая половина растёт сама собой.
    Сказать об этом надо до печати — после того как этикетки наклеены
    на сотню пакетов, чинить нечего.
    """
    longest = max((str(url) for url in urls), key=len, default='')
    if len(longest) <= QR_MAX_CHARS:
        return ''
    return (
        f'Ссылка в QR длиннее {QR_MAX_CHARS} символов ({len(longest)}): {longest}. '
        'Код станет мельче и может не читаться сканером — особенно на этикетке '
        'заказа, где он всего 9,6 мм. Укоротите LABEL_BASE_URL.'
    )


def _part_label(part, base_url):
    """Данные одной этикетки детали.

    Набор полей общий с этикеткой ячейки (`_cell_label`) — печатаются они
    одним шаблоном.
    """
    link = qr_url(base_url, 'p', part.pk)
    cell = part.current_cell
    return {
        'part': part,
        'cell': cell,
        'title': part.part_number,
        'specs': _label_specs(part),
        'description': part.label_text,
        'package': part.package,
        'application': part.application,
        'address': cell.address if cell else 'нет ячейки',
        'qr_url': link,
        'qr_img': generate_qr_image(link),
    }


@login_required
def part_label(request, pk):
    """Этикетка детали — для наклейки на пакет."""
    part = get_object_or_404(SparePart, pk=pk)
    base_url = label_base_url(request)
    context = _part_label(part, base_url)
    context['qr_base'] = base_url
    context['qr_warning'] = qr_length_warning([context['qr_url']])
    return render(request, 'core/parts/label.html', context)


# Верхняя граница пачки. Столько этикеток — уже полтора метра ленты; больше
# за один раз не печатают, а страница с тысячей QR-картинок открывалась бы
# на планшете минуту и могла не влезть в память.
MAX_LABELS_PER_BATCH = 200


def _batch_layout(request):
    """Раскладка пачки: рулон (по этикетке на страницу) или лист A4."""
    return 'a4' if request.GET.get('layout') == 'a4' else 'roll'


def _selected_ids(request):
    """Отмеченные записи из формы списка."""
    return [value for value in request.GET.getlist('ids') if value.isdigit()]


@login_required
def part_labels_batch(request):
    """Пачка этикеток деталей: по отмеченным или по всему текущему отбору."""
    ids = _selected_ids(request)
    if ids:
        parts = SparePart.objects.filter(pk__in=ids)
    else:
        parts, _ = _filter_parts(request)

    parts = list(parts.prefetch_related('storage_cells').order_by('part_number')[:MAX_LABELS_PER_BATCH])
    base_url = label_base_url(request)

    labels = [_part_label(part, base_url) for part in parts]

    return render(request, 'core/parts/labels_batch.html', {
        'labels': labels,
        'layout': _batch_layout(request),
        'limit': MAX_LABELS_PER_BATCH,
        'qr_base': base_url,
        'qr_warning': qr_length_warning(label['qr_url'] for label in labels),
    })


@login_required
def storage_cell_labels_batch(request):
    """Пачка этикеток ячеек: по отмеченным, либо по кассетнице целиком.

    Кассетница целиком — обычный случай: этикетки клеят на все ячейки сразу,
    когда собирают новую или переклеивают старые.
    """
    ids = _selected_ids(request)
    cells = StorageCell.objects.select_related('cabinet').prefetch_related('parts')

    if ids:
        cells = cells.filter(pk__in=ids)
    else:
        cabinet = request.GET.get('cabinet', '')
        cells = cells.filter(cabinet__number=int(cabinet)) if cabinet.isdigit() else cells.none()

    # Пустые ячейки обычно подписывать незачем: адрес и так виден в сетке,
    # а лента тратится
    only_filled = request.GET.get('only_filled') == '1'
    cells = list(cells.order_by('cabinet__number', 'row_number', 'cell_row')[:MAX_LABELS_PER_BATCH])
    if only_filled:
        cells = [cell for cell in cells if cell.parts.all()]

    base_url = label_base_url(request)

    labels = [_cell_label(cell, base_url) for cell in cells]

    return render(request, 'core/storage_cells/labels_batch.html', {
        'labels': labels,
        'qr_base': base_url,
        'qr_warning': qr_length_warning(label['qr_url'] for label in labels),
        'layout': _batch_layout(request),
        'only_filled': only_filled,
        'cabinet': request.GET.get('cabinet', ''),
        'limit': MAX_LABELS_PER_BATCH,
    })
