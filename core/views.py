"""
Views для LiftTeam v2.22.0.
CRUD операции, дашборд, отчёты, визуальная сетка кассетниц, печать этикеток,
импорт радиодеталей из Excel.
"""
import re
import json
import openpyxl
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib.auth import authenticate, login, logout
from django.conf import settings
from django.contrib import messages
from django.db.models import Q, Sum, Count, F
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
    Client, EquipmentModel, Equipment, RepairOrder, RepairOrderEquipment,
    SparePart, StorageCell, StockMovement, RepairOrderDetail, OrderStatusHistory, Employee,
    Notification, warranty_cutoff,
)
from .forms import (
    LoginForm, ClientForm, EquipmentModelForm, EquipmentForm,
    RepairOrderForm, RepairOrderDetailForm, SparePartForm,
    StockMovementForm, StockOutgoingForm, EmployeeForm, StatusChangeForm,
    RepairOrderEquipmentFormSet, PartImportForm
)
from .utils import (
    generate_barcode_image, generate_qr_image,
    build_workbook, xlsx_response, excel_datetime,
)
from .decorators import role_required
from . import notifications, updater


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
    debtors = RepairOrder.objects.filter(
        payment_status__in=['unpaid', 'partially_paid']
    ).select_related('client')
    total_debt = _total_debt(debtors)

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

        # Оповещение заказчику — в очередь, не отправкой на месте: SMTP через
        # домашний канал может думать секундами, а страница ждать не должна
        notifications.notify_order_status(order, changed_by=request.user)

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
            Q(description__icontains=search)
        )
    if component_type:
        parts = parts.filter(component_type=component_type)

    context = {
        'search': search,
        'component_type': component_type,
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

    component_types = SparePart.objects.exclude(component_type='').values_list('component_type', flat=True).distinct().order_by('component_type')

    paginator = Paginator(parts.order_by('part_number'), 25)
    page = request.GET.get('page')
    return render(request, 'core/parts/list.html', {
        'parts': paginator.get_page(page),
        'component_types': component_types,
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
        'part_number', 'name', 'component_type',
        'resistance', 'resistance_unit',
        'power', 'power_unit',
        'voltage', 'voltage_unit',
        'current', 'current_unit',
        'capacitance', 'capacitance_unit',
        'min_stock', 'current_stock', 'lead_time_days',
        'preferred_supplier', 'description',
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
    ).order_by('cabinet_number', 'row_number', 'cell_row')
    stock_form = StockMovementForm()
    return render(request, 'core/parts/detail.html', {
        'part': part,
        'movements': movements,
        'available_cells': available_cells,
        'stock_form': stock_form,
    })


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


def _cell_label(cell, base_url):
    """Данные одной этикетки ячейки.

    Если все детали в ячейке одного типа (набор номиналов одного типа —
    резисторы, конденсаторы и т.д.), тип указывается один раз, а значения
    характеристик перечисляются списком. Иначе — построчно название
    и характеристика.
    """
    parts = list(cell.parts.all())
    component_types = {p.component_type for p in parts if p.component_type}
    grouped = len(parts) > 1 and len(component_types) == 1

    return {
        'cell': cell,
        'cell_parts': parts,
        # Ссылка вместо простого адреса: сканирование сразу открывает
        # содержимое, а не показывает строку, которую потом ищут вручную
        'qr_img': generate_qr_image(f'{base_url}/c/{cell.pk}/'),
        'grouped': grouped,
        'group_type': parts[0].component_type if grouped else None,
        'group_values': (
            ', '.join(p.specs_display for p in parts if p.specs_display) if grouped else ''
        ),
    }


@login_required
def storage_cell_label(request, pk):
    """Печать этикетки одной ячейки."""
    cell = get_object_or_404(StorageCell.objects.prefetch_related('parts'), pk=pk)
    return render(
        request, 'core/storage_cells/label.html', _cell_label(cell, label_base_url(request))
    )


@login_required
def equipment_label(request, pk):
    """Печать этикетки единицы оборудования (43x25 мм)."""
    equipment = get_object_or_404(Equipment.objects.select_related('model'), pk=pk)

    # Ссылка вместо JSON. Раньше в код клали {"id":…,"model":"БУАД",…}:
    # сканирование давало строку с фигурными скобками, которую человек всё
    # равно шёл искать руками, а кириллица внутри — худший случай по плотности
    # кода (QR переключается в побайтовый режим и распухает). Короткая ссылка
    # укладывается в 29 модулей и сразу открывает историю этой единицы.
    qr_img = generate_qr_image(f"{label_base_url(request)}/e/{equipment.pk}/")

    # Штрихкод оставлен: он кодирует сам серийный номер и читается сканером
    # с клавиатурным вводом, которым QR не заменить
    barcode_img = generate_barcode_image(equipment.serial_number)

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

    # Ссылка на заказ вместо текста «LT-2026-08-001/1». Текст читался
    # человеком, но сканирование им ничего не давало: заказ всё равно искали
    # руками. Плата за ссылку — код вырастает с 21 до 25–29 модулей
    # (сколько именно, зависит от длины LABEL_BASE_URL), поэтому под него
    # освобождено место: у логотипа убран внутренний круг, а сам он увеличен.
    qr_img = generate_qr_image(f'{label_base_url(request)}/o/{order.pk}/')

    return render(request, 'core/repair_orders/equipment_label.html', {
        'order': order,
        'roe': roe,
        'position': position,
        'qr_img': qr_img,
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
    return render(request, 'core/reports/purchase_plan.html', {
        'parts': _purchase_plan_parts(),
    })


@login_required
def report_purchase_plan_export(request):
    """План закупок в Excel — файл отправляют поставщику."""
    headers = [
        'Артикул', 'Название', 'Тип', 'Характеристики',
        'Остаток', 'Мин. остаток', 'Не хватает',
        'Срок поставки, дней', 'Поставщик', 'Ячейка',
    ]
    rows = [
        [
            part.part_number, part.name, part.component_type, part.specs_display,
            part.current_stock, part.min_stock, part.stock_deficit,
            part.lead_time_days, part.preferred_supplier,
            part.current_cell.address if part.current_cell else '',
        ]
        for part in _purchase_plan_parts()
    ]
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
        'Документ', 'Заказ', 'Сотрудник', 'Примечания',
    ]
    rows = [
        [
            excel_datetime(m.movement_date),
            m.part.part_number, m.part.name,
            m.get_movement_type_display(), m.quantity,
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
    """Неоплаченные и частично оплаченные заказы — общее для отчёта и выгрузки."""
    return (
        RepairOrder.objects
        .filter(payment_status__in=['unpaid', 'partially_paid'])
        .select_related('client')
        .prefetch_related('order_equipments__equipment__model')
        .order_by('-date_received')
    )


def _total_debt(orders):
    """Сумма долга одним запросом.

    Складываем стоимости всех единиц оборудования в отобранных заказах: обход
    заказов в цикле давал бы отдельный запрос на каждый, а должников
    в конце месяца бывает много.
    """
    return orders.aggregate(total=Sum('order_equipments__repair_cost'))['total'] or 0


@login_required
def report_debtors(request):
    """Задолженности по заказам."""
    orders = _debtor_orders()
    return render(request, 'core/reports/debtors.html', {
        'orders': orders,
        'total_debt': _total_debt(orders),
    })


@login_required
def report_debtors_export(request):
    """Задолженности в Excel — для сверки с бухгалтерией."""
    orders = _debtor_orders()

    headers = [
        '№ заказа', 'Дата приёма', 'Заказчик', 'ИНН', 'Оборудование',
        'Статус ремонта', 'Статус оплаты', '№ счёта', 'Дата счёта', 'Сумма, ₽',
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
        ]
        for order in orders
    ]
    # Итог в самой книге: иначе бухгалтеру пришлось бы складывать столбец
    # вручную, а именно эта цифра из отчёта и нужна
    if rows:
        rows.append(['Итого'] + [''] * (len(headers) - 2) + [_total_debt(orders)])

    wb = build_workbook('Задолженности', headers, rows)
    return xlsx_response(wb, f'Задолженности {timezone.localdate():%Y-%m-%d}.xlsx')


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
    return redirect(f"{reverse('storage_cell_grid')}?cabinet={cell.cabinet_number}&open_cell={cell.pk}")


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

    По умолчанию берётся адрес, по которому открыта страница печати: этикетка,
    напечатанная из офисной сети, получит локальный адрес. Если задать
    LABEL_BASE_URL в .env, используется он — это нужно, когда печатают
    через Tailscale, а сканировать будут в офисе.
    """
    configured = getattr(settings, 'LABEL_BASE_URL', '')
    return configured.rstrip('/') if configured else request.build_absolute_uri('/').rstrip('/')


def _part_label(part, base_url):
    """Данные одной этикетки детали."""
    return {
        'part': part,
        'cell': part.current_cell,
        'qr_img': generate_qr_image(f'{base_url}/p/{part.pk}/'),
    }


@login_required
def part_label(request, pk):
    """Этикетка детали — для наклейки на пакет."""
    part = get_object_or_404(SparePart, pk=pk)
    return render(request, 'core/parts/label.html', _part_label(part, label_base_url(request)))


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

    return render(request, 'core/parts/labels_batch.html', {
        'labels': [_part_label(part, base_url) for part in parts],
        'layout': _batch_layout(request),
        'limit': MAX_LABELS_PER_BATCH,
    })


@login_required
def storage_cell_labels_batch(request):
    """Пачка этикеток ячеек: по отмеченным, либо по кассетнице целиком.

    Кассетница целиком — обычный случай: этикетки клеят на все ячейки сразу,
    когда собирают новую или переклеивают старые.
    """
    ids = _selected_ids(request)
    cells = StorageCell.objects.prefetch_related('parts')

    if ids:
        cells = cells.filter(pk__in=ids)
    else:
        cabinet = request.GET.get('cabinet', '')
        cells = cells.filter(cabinet_number=int(cabinet)) if cabinet.isdigit() else cells.none()

    # Пустые ячейки обычно подписывать незачем: адрес и так виден в сетке,
    # а лента тратится
    only_filled = request.GET.get('only_filled') == '1'
    cells = list(cells.order_by('cabinet_number', 'row_number', 'cell_row')[:MAX_LABELS_PER_BATCH])
    if only_filled:
        cells = [cell for cell in cells if cell.parts.all()]

    base_url = label_base_url(request)

    return render(request, 'core/storage_cells/labels_batch.html', {
        'labels': [_cell_label(cell, base_url) for cell in cells],
        'layout': _batch_layout(request),
        'only_filled': only_filled,
        'cabinet': request.GET.get('cabinet', ''),
        'limit': MAX_LABELS_PER_BATCH,
    })
