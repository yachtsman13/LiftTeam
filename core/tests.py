"""
Тесты для LiftTeam. Покрывают наиболее рискованную бизнес-логику:
генерацию номера заказа, движение склада, ячейки с несколькими деталями,
импорт/экспорт Excel, права доступа по ролям, маршрутизацию,
настройку SQLite и резервное копирование.
"""
import datetime
import io
from decimal import Decimal
import json
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch
from urllib import error as urllib_error

import openpyxl
from django.conf import settings
from django.core.management import call_command
from django.db import connection, transaction
from django.test import SimpleTestCase, TestCase, override_settings
from django.test import Client as TestClient
from django.test.utils import CaptureQueriesContext
from django.urls import resolve
from django.utils import timezone

from . import messengers, notifications, tbank, views
from .forms import RepairOrderEquipmentForm
from .models import (
    BankOperation, Cabinet, Client as ClientModel, Employee, Equipment, EquipmentModel,
    FaultType, FaultTypePart, InventorySession, InventorySessionLine, Notification, Organization,
    OrderCost, OrderStatusHistory, Payment,
    RepairOrder, RepairOrderDetail, RepairOrderEquipment, SparePart, StockAllocation, StockMovement,
    StorageCell, parse_layout, plural_genitive, format_spec,
)


class FakeChannelLayer:
    """Подменяет Redis/InMemory channel layer, чтобы посчитать реальное число
    отправленных WebSocket-уведомлений (регрессия дублирования, см. signals.py/views.py)."""
    def __init__(self):
        self.calls = []

    async def group_send(self, group, message):
        self.calls.append((group, message))


class OrderNumberGenerationTests(TestCase):
    def setUp(self):
        self.client_obj = ClientModel.objects.create(name='Заказчик 1')

    def test_format_and_uniqueness(self):
        order1 = RepairOrder.objects.create(client=self.client_obj)
        order2 = RepairOrder.objects.create(client=self.client_obj)

        self.assertRegex(order1.order_number, r'^LT-\d{4}-\d{2}-\d{3}$')
        self.assertNotEqual(order1.order_number, order2.order_number)

        seq1 = int(order1.order_number.rsplit('-', 1)[-1])
        seq2 = int(order2.order_number.rsplit('-', 1)[-1])
        self.assertEqual(seq2, seq1 + 1)


class StockMovementTests(TestCase):
    def setUp(self):
        self.user = Employee.objects.create_user(
            username='warehouse1', full_name='Кладовщик', password='pass', role='warehouse'
        )
        self.part = SparePart.objects.create(
            part_number='P-001', name='Тестовая деталь', current_stock=10, min_stock=2
        )
        self.client_http = TestClient()
        self.client_http.force_login(self.user)

    def test_incoming_updates_stock_and_sends_single_notification(self):
        fake_layer = FakeChannelLayer()
        with patch('core.views.get_channel_layer', return_value=fake_layer), \
             patch('core.signals.get_channel_layer', return_value=fake_layer):
            self.client_http.post(
                f'/parts/{self.part.pk}/stock-incoming/',
                {'quantity': 5, 'document_number': '', 'notes': ''}
            )

        self.part.refresh_from_db()
        self.assertEqual(self.part.current_stock, 15)
        self.assertEqual(self.part.movements.count(), 1)
        # Регрессия: раньше уведомление слалось дважды (сигнал + ручной вызов)
        self.assertEqual(len(fake_layer.calls), 1)

    def test_outgoing_updates_stock_and_sends_single_notification(self):
        fake_layer = FakeChannelLayer()
        with patch('core.views.get_channel_layer', return_value=fake_layer), \
             patch('core.signals.get_channel_layer', return_value=fake_layer):
            self.client_http.post(
                f'/parts/{self.part.pk}/stock-outgoing/',
                {'quantity': 3, 'reason': 'consumption', 'notes': '', 'document_number': ''}
            )

        self.part.refresh_from_db()
        self.assertEqual(self.part.current_stock, 7)
        self.assertEqual(len(fake_layer.calls), 1)


class RepairOrderAddDetailTests(TestCase):
    def setUp(self):
        self.user = Employee.objects.create_user(
            username='manager1', full_name='Менеджер', password='pass', role='repair_manager'
        )
        self.client_obj = ClientModel.objects.create(name='Заказчик 2')
        self.order = RepairOrder.objects.create(client=self.client_obj)
        self.part = SparePart.objects.create(part_number='P-002', name='Деталь 2', current_stock=10, min_stock=2)
        self.client_http = TestClient()
        self.client_http.force_login(self.user)

    def test_add_detail_decrements_stock_and_logs_history(self):
        self.client_http.post(
            f'/repair-orders/{self.order.pk}/add-detail/',
            {'part': self.part.pk, 'quantity_used': 4}
        )
        self.part.refresh_from_db()
        self.assertEqual(self.part.current_stock, 6)
        self.assertEqual(self.order.details.count(), 1)
        self.assertTrue(self.order.status_history.filter(notes__icontains='Добавлена деталь').exists())


class RepairOrderFormErrorTests(TestCase):
    """Регрессия: форма молча не сохранялась. Ошибки полей «Стоимость ремонта»
    и «Папка на Яндекс.Диске» не выводились нигде — страница перезагружалась
    без единого сообщения, и пользователь считал заказ созданным."""

    def setUp(self):
        self.user = Employee.objects.create_superuser(
            username='order_form', full_name='Тест', password='pass'
        )
        self.client_obj = ClientModel.objects.create(name='Заказчик формы')
        self.client_http = TestClient()
        self.client_http.force_login(self.user)

    def _post(self, **overrides):
        data = {
            'client': self.client_obj.pk,
            'fault_description': '',
            'invoice_number': '',
            'invoice_date': '',
            'payment_status': 'unpaid',
            'equipments-TOTAL_FORMS': '1',
            'equipments-INITIAL_FORMS': '0',
            'equipments-MIN_NUM_FORMS': '0',
            'equipments-MAX_NUM_FORMS': '1000',
            'equipments-0-equipment': '',
            'equipments-0-fault_description': '',
            'equipments-0-seal_numbers': '',
            'equipments-0-initial_condition': '',
            'equipments-0-repair_cost': '',
            'equipments-0-yandex_disk_folder': '',
        }
        data.update(overrides)
        return self.client_http.post('/repair-orders/create/', data)

    def test_valid_submission_creates_order(self):
        response = self._post()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(RepairOrder.objects.count(), 1)

    def test_invalid_repair_cost_reports_error_and_creates_nothing(self):
        response = self._post(**{'equipments-0-repair_cost': '15 000 руб'})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(RepairOrder.objects.count(), 0)
        self.assertIn('repair_cost', response.context['formset'].errors[0])

    def test_invalid_yandex_link_reports_error_and_creates_nothing(self):
        response = self._post(**{'equipments-0-yandex_disk_folder': 'папка на диске'})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(RepairOrder.objects.count(), 0)
        self.assertIn('yandex_disk_folder', response.context['formset'].errors[0])


class StorageCellMultiPartTests(TestCase):
    def setUp(self):
        self.admin = Employee.objects.create_superuser(username='admin_t', full_name='Админ', password='pass')
        self.part1 = SparePart.objects.create(part_number='CELL-1', name='Деталь A')
        self.part2 = SparePart.objects.create(part_number='CELL-2', name='Деталь B')
        self.cell1 = StorageCell.objects.create(cabinet=Cabinet.objects.get_or_create(number=1)[0], row_number=1, cell_row=1)
        self.cell2 = StorageCell.objects.create(cabinet=Cabinet.objects.get_or_create(number=1)[0], row_number=1, cell_row=2)
        self.client_http = TestClient()
        self.client_http.force_login(self.admin)

    def test_cell_holds_multiple_parts(self):
        self.cell1.parts.add(self.part1, self.part2)
        self.assertEqual(self.cell1.parts.count(), 2)

    def test_assign_cell_moves_part_from_previous_cell(self):
        self.cell1.parts.add(self.part1)
        self.client_http.post(f'/parts/{self.part1.pk}/assign-cell/', {'cell_id': self.cell2.pk})

        self.assertEqual(self.cell1.parts.count(), 0)
        self.assertEqual(list(self.cell2.parts.all()), [self.part1])

    def test_assign_cell_does_not_evict_other_parts_from_target(self):
        self.cell2.parts.add(self.part2)
        self.client_http.post(f'/parts/{self.part1.pk}/assign-cell/', {'cell_id': self.cell2.pk})

        self.assertEqual(self.cell2.parts.count(), 2)

    def test_add_part_endpoint(self):
        self.client_http.post(f'/storage-cells/{self.cell1.pk}/add-part/', {'part_id': self.part1.pk})
        self.client_http.post(f'/storage-cells/{self.cell1.pk}/add-part/', {'part_id': self.part2.pk})

        self.assertEqual(self.cell1.parts.count(), 2)

    def test_add_part_evicts_from_previous_cell(self):
        self.cell1.parts.add(self.part1)
        self.client_http.post(f'/storage-cells/{self.cell2.pk}/add-part/', {'part_id': self.part1.pk})

        self.assertEqual(self.cell1.parts.count(), 0)
        self.assertEqual(self.cell2.parts.count(), 1)

    def test_remove_part_endpoint(self):
        self.cell1.parts.add(self.part1)
        self.client_http.post(f'/storage-cells/{self.cell1.pk}/remove-part/', {'part_id': self.part1.pk})

        self.assertEqual(self.cell1.parts.count(), 0)

    def test_move_endpoint_allows_target_cell_with_other_parts(self):
        self.cell1.parts.add(self.part1)
        self.cell2.parts.add(self.part2)

        resp = self.client_http.post('/storage-cells/move/', {
            'from_cell': self.cell1.pk, 'to_cell': self.cell2.pk, 'part_id': self.part1.pk,
        })

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['success'], True)
        self.assertEqual(self.cell2.parts.count(), 2)
        self.assertEqual(self.cell1.parts.count(), 0)


class ExcelImportExportTests(TestCase):
    def setUp(self):
        self.admin = Employee.objects.create_superuser(username='admin_x', full_name='Админ', password='pass')
        self.client_http = TestClient()
        self.client_http.force_login(self.admin)

    def _make_xlsx(self, rows):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(['part_number', 'name', 'component_type', 'voltage'])
        for row in rows:
            ws.append(row)
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        buf.name = 'import.xlsx'
        return buf

    def test_import_parses_text_values_with_units(self):
        """Регрессия: строковое значение с единицей измерения ('5 В') раньше
        роняло весь импорт с NameError из-за отсутствующего `import re`."""
        f = self._make_xlsx([['IMP-1', 'Импортированная деталь', 'Резистор', '5 В']])
        resp = self.client_http.post('/parts/import/', {'file': f, 'update_existing': False}, follow=True)

        self.assertEqual(resp.status_code, 200)
        part = SparePart.objects.get(part_number='IMP-1')
        self.assertEqual(float(part.voltage), 5.0)

    def test_import_requires_mandatory_columns(self):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(['name'])
        ws.append(['Без артикула'])
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        buf.name = 'bad.xlsx'

        resp = self.client_http.post('/parts/import/', {'file': buf, 'update_existing': False})
        self.assertEqual(SparePart.objects.count(), 0)
        self.assertEqual(resp.status_code, 302)

    def test_export_returns_xlsx_with_header(self):
        SparePart.objects.create(part_number='EXP-1', name='Экспортируемая деталь', voltage=12)

        resp = self.client_http.get('/parts/export/')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('spreadsheetml', resp['Content-Type'])

        wb = openpyxl.load_workbook(io.BytesIO(resp.content))
        ws = wb.active
        self.assertEqual([c.value for c in ws[1]][:2], ['part_number', 'name'])
        self.assertEqual(ws.max_row, 2)

    def test_export_respects_search_filter(self):
        SparePart.objects.create(part_number='EXP-A', name='Совпадёт')
        SparePart.objects.create(part_number='EXP-B', name='Не совпадёт')

        resp = self.client_http.get('/parts/export/?q=EXP-A')
        wb = openpyxl.load_workbook(io.BytesIO(resp.content))
        self.assertEqual(wb.active.max_row, 2)  # заголовок + 1 деталь

    def test_package_survives_import_and_export(self):
        """Выгрузка принимается обратно импортом, поэтому новая колонка
        обязана быть в обеих."""
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(['part_number', 'name', 'component_type', 'package'])
        ws.append(['PKG-1', 'Стабилизатор', 'Микросхема', 'TO-220'])
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        buf.name = 'import.xlsx'

        self.client_http.post('/parts/import/', {'file': buf, 'update_existing': False})
        self.assertEqual(SparePart.objects.get(part_number='PKG-1').package, 'TO-220')

        resp = self.client_http.get('/parts/export/?q=PKG-1')
        exported = openpyxl.load_workbook(io.BytesIO(resp.content)).active
        row = dict(zip([c.value for c in exported[1]], [c.value for c in exported[2]]))
        self.assertEqual(row['package'], 'TO-220')

    def test_import_then_export_round_trip_preserves_values(self):
        f = self._make_xlsx([['RT-1', 'Round trip', 'Диод', '3,3']])
        self.client_http.post('/parts/import/', {'file': f, 'update_existing': False})

        resp = self.client_http.get('/parts/export/?q=RT-1')
        wb = openpyxl.load_workbook(io.BytesIO(resp.content))
        headers = [c.value for c in wb.active[1]]
        row = dict(zip(headers, [c.value for c in wb.active[2]]))
        self.assertEqual(row['part_number'], 'RT-1')
        self.assertEqual(float(row['voltage']), 3.3)


class RolePermissionTests(TestCase):
    def setUp(self):
        self.warehouse_user = Employee.objects.create_user(
            username='wh1', full_name='Кладовщик', password='pass', role='warehouse'
        )
        self.manager_user = Employee.objects.create_user(
            username='rm1', full_name='Менеджер', password='pass', role='repair_manager'
        )
        self.part = SparePart.objects.create(part_number='PERM-1', name='Деталь')

    def test_warehouse_can_delete_part(self):
        client = TestClient()
        client.force_login(self.warehouse_user)
        resp = client.get(f'/parts/{self.part.pk}/delete/')
        self.assertEqual(resp.status_code, 200)

    def test_repair_manager_cannot_delete_part(self):
        client = TestClient()
        client.force_login(self.manager_user)
        resp = client.get(f'/parts/{self.part.pk}/delete/', follow=True)
        self.assertRedirects(resp, '/')

    def test_non_admin_cannot_access_user_management(self):
        client = TestClient()
        client.force_login(self.warehouse_user)
        resp = client.get('/management/users/', follow=True)
        self.assertRedirects(resp, '/')


class EquipmentHistoryTests(TestCase):
    """История ремонтов одной физической единицы по серийному номеру.

    Оборудование уже было выделено в отдельную сущность с уникальным
    серийником, поэтому история собирается связью, а не поиском по строке.
    Открытым оставался разъезд истории из-за разного написания одного
    и того же номера разными сотрудниками."""

    def setUp(self):
        self.user = Employee.objects.create_superuser(
            username='hist_user', full_name='Тест', password='pass'
        )
        self.model = EquipmentModel.objects.create(name='Экодрайв')
        self.client_obj = ClientModel.objects.create(name='ООО Лифт')
        self.equipment = Equipment.objects.create(model=self.model, serial_number='БУАД-1234')
        self.client_http = TestClient()
        self.client_http.force_login(self.user)

    def _make_order(self, equipment=None):
        order = RepairOrder.objects.create(client=self.client_obj)
        RepairOrderEquipment.objects.create(
            repair_order=order, equipment=equipment or self.equipment
        )
        return order

    def test_normalization_ignores_case_and_separators(self):
        for written in ['буад 1234', 'БУАД_1234', ' БУАД-1234 ', 'бУаД1234']:
            self.assertEqual(
                Equipment.normalize_serial(written),
                self.equipment.serial_normalized,
                f'не сошлось на варианте {written!r}',
            )

    def test_normalized_is_filled_on_save(self):
        equipment = Equipment.objects.create(model=self.model, serial_number='ec-99 x')
        self.assertEqual(equipment.serial_normalized, 'EC99X')

    def test_saved_serial_keeps_original_spelling(self):
        """Нормализация нужна для поиска, но печатать надо то, что набрали."""
        equipment = Equipment.objects.create(model=self.model, serial_number='ЭД-77/2')
        equipment.refresh_from_db()
        self.assertEqual(equipment.serial_number, 'ЭД-77/2')

    def test_find_similar_matches_other_spelling(self):
        other = Equipment.objects.create(model=self.model, serial_number='буад 1234!')
        found = Equipment.find_similar('БУАД-1234')
        self.assertIn(other, found)
        self.assertIn(self.equipment, found)

    def test_find_similar_can_exclude_itself(self):
        found = Equipment.find_similar(self.equipment.serial_number, exclude_pk=self.equipment.pk)
        self.assertNotIn(self.equipment, found)

    def test_find_similar_ignores_empty_serial(self):
        self.assertEqual(list(Equipment.find_similar('   ')), [])

    def test_history_page_lists_all_visits(self):
        first = self._make_order()
        second = self._make_order()

        response = self.client_http.get(f'/equipment/{self.equipment.pk}/history/')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, first.order_number)
        self.assertContains(response, second.order_number)
        self.assertEqual(response.context['visits_count'], 2)

    def test_history_excludes_other_equipment_orders(self):
        other = Equipment.objects.create(model=self.model, serial_number='ДРУГОЙ-1')
        foreign_order = self._make_order(equipment=other)
        self._make_order()

        response = self.client_http.get(f'/equipment/{self.equipment.pk}/history/')

        self.assertNotContains(response, foreign_order.order_number)

    def test_history_marks_parts_as_order_wide_for_multi_unit_orders(self):
        """Детали списываются на заказ целиком. Когда единиц в заказе
        несколько, нельзя утверждать, что деталь ставили именно в эту."""
        order = self._make_order()
        second_unit = Equipment.objects.create(model=self.model, serial_number='ВТОРОЙ-1')
        RepairOrderEquipment.objects.create(repair_order=order, equipment=second_unit)

        response = self.client_http.get(f'/equipment/{self.equipment.pk}/history/')

        self.assertFalse(response.context['visit_rows'][0]['parts_are_exact'])

    def test_history_marks_parts_exact_for_single_unit_order(self):
        self._make_order()
        response = self.client_http.get(f'/equipment/{self.equipment.pk}/history/')
        self.assertTrue(response.context['visit_rows'][0]['parts_are_exact'])

    def test_empty_history_is_not_an_error(self):
        response = self.client_http.get(f'/equipment/{self.equipment.pk}/history/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['visits_count'], 0)

    def test_search_finds_equipment_by_different_spelling(self):
        response = self.client_http.get('/equipment/', {'q': 'буад 1234'})
        self.assertContains(response, 'БУАД-1234')

    def test_history_summary_reports_previous_visits(self):
        self._make_order()
        response = self.client_http.get(
            f'/ajax/equipment/{self.equipment.pk}/history-summary/'
        )

        data = response.json()
        self.assertEqual(data['orders_count'], 1)
        self.assertTrue(data['history_url'].endswith(f'/equipment/{self.equipment.pk}/history/'))

    def test_history_summary_is_zero_for_new_equipment(self):
        response = self.client_http.get(
            f'/ajax/equipment/{self.equipment.pk}/history-summary/'
        )
        self.assertEqual(response.json()['orders_count'], 0)

    def test_creating_similar_serial_warns_instead_of_duplicating(self):
        response = self.client_http.post('/ajax/equipment/create/', {
            'model_id': self.model.pk,
            'serial_number': 'буад 1234',
        })

        data = response.json()
        self.assertFalse(data['success'])
        self.assertEqual(len(data['similar']), 1)
        self.assertEqual(data['similar'][0]['serial_number'], 'БУАД-1234')
        # Запись не создана: сотрудник ещё не решил, та же это единица или нет
        self.assertEqual(Equipment.objects.count(), 1)

    def test_confirmed_creation_proceeds_despite_similarity(self):
        response = self.client_http.post('/ajax/equipment/create/', {
            'model_id': self.model.pk,
            'serial_number': 'буад 1234',
            'confirmed': '1',
        })

        self.assertTrue(response.json()['success'])
        self.assertEqual(Equipment.objects.count(), 2)

    def test_exact_duplicate_is_still_rejected_outright(self):
        """Полное совпадение — не повод переспрашивать, это просто дубль."""
        response = self.client_http.post('/ajax/equipment/create/', {
            'model_id': self.model.pk,
            'serial_number': 'БУАД-1234',
            'confirmed': '1',
        })

        data = response.json()
        self.assertFalse(data['success'])
        self.assertNotIn('similar', data)
        self.assertEqual(Equipment.objects.count(), 1)

    def test_unrelated_serial_creates_without_warning(self):
        response = self.client_http.post('/ajax/equipment/create/', {
            'model_id': self.model.pk,
            'serial_number': 'СОВСЕМ-ДРУГОЙ',
        })

        self.assertTrue(response.json()['success'])
        self.assertEqual(Equipment.objects.count(), 2)

    def test_history_requires_login(self):
        anonymous = TestClient()
        response = anonymous.get(f'/equipment/{self.equipment.pk}/history/')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response['Location'])


class LabelTests(TestCase):
    """Две разные этикетки: на пакет с деталью и на саму ячейку.
    Раньше кнопка в списке деталей печатала этикетку ячейки, что для
    наклейки на пакет неверно — на ней не было самой детали."""

    def setUp(self):
        self.user = Employee.objects.create_superuser(
            username='label_user', full_name='Тест', password='pass'
        )
        self.part = SparePart.objects.create(
            part_number='LBL-1', name='Деталь для этикетки',
            component_type='Резисторы', current_stock=5,
        )
        self.other = SparePart.objects.create(part_number='LBL-2', name='Соседняя деталь')
        self.cell = StorageCell.objects.create(cabinet=Cabinet.objects.get_or_create(number=2)[0], row_number=3, cell_row=4)
        self.cell.parts.add(self.part, self.other)
        self.client_http = TestClient()
        self.client_http.force_login(self.user)

    def test_part_label_shows_only_that_part(self):
        response = self.client_http.get(f'/parts/{self.part.pk}/label/')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'LBL-1')
        # Соседняя деталь из той же ячейки на этикетке пакета быть не должна
        self.assertNotContains(response, 'LBL-2')

    def test_part_label_shows_cell_address(self):
        response = self.client_http.get(f'/parts/{self.part.pk}/label/')
        self.assertContains(response, self.cell.address)

    def test_cell_label_lists_all_parts(self):
        response = self.client_http.get(f'/storage-cells/{self.cell.pk}/label/')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.cell.address)

    def test_short_urls_redirect_to_pages(self):
        part_response = self.client_http.get(f'/p/{self.part.pk}/')
        self.assertRedirects(part_response, f'/parts/{self.part.pk}/')

        cell_response = self.client_http.get(f'/c/{self.cell.pk}/')
        self.assertRedirects(
            cell_response,
            f'/storage-cells/?cabinet={self.cell.cabinet.number}&open_cell={self.cell.pk}',
        )

    def test_short_urls_require_login(self):
        anonymous = TestClient()
        response = anonymous.get(f'/p/{self.part.pk}/')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response['Location'])

    def test_label_base_url_setting_overrides_request_host(self):
        """Адрес в коде не должен зависеть от того, откуда печатали."""
        from core.views import label_base_url

        request = TestClient().get('/').wsgi_request
        with self.settings(LABEL_BASE_URL='http://100.108.92.92/'):
            self.assertEqual(label_base_url(request), 'http://100.108.92.92')

    def test_part_label_shows_the_package_next_to_the_article(self):
        """Корпус — то, по чему деталь опознают в руках."""
        self.part.package = 'TO-220'
        self.part.save(update_fields=['package'])

        response = self.client_http.get(f'/parts/{self.part.pk}/label/')

        self.assertContains(response, 'TO-220')
        self.assertContains(response, 'label-package')

    def test_part_label_prints_the_description(self):
        self.part.description = 'Ставится в блок питания БУАД'
        self.part.save(update_fields=['description'])

        response = self.client_http.get(f'/parts/{self.part.pk}/label/')

        self.assertContains(response, 'Ставится в блок питания БУАД')

    def test_part_label_falls_back_to_the_name(self):
        """Описание заполняют не всегда, а пустая строка на этикетке
        не нужна никому."""
        self.assertEqual(self.part.description, '')

        response = self.client_http.get(f'/parts/{self.part.pk}/label/')

        self.assertContains(response, 'Деталь для этикетки')

    def test_part_label_loads_the_font_fitting_script(self):
        """Без него длинный артикул обрезался бы многоточием."""
        for page in (f'/parts/{self.part.pk}/label/', '/parts/labels/?ids=%d' % self.part.pk):
            with self.subTest(page=page):
                response = self.client_http.get(page)
                self.assertContains(response, 'label-fit.js')
                self.assertContains(response, 'data-fit')

    def test_short_urls_work_without_the_trailing_slash(self):
        """Именно эта форма попадает в QR — она на символ короче."""
        part_response = self.client_http.get(f'/p/{self.part.pk}')
        self.assertRedirects(part_response, f'/parts/{self.part.pk}/')

        cell_response = self.client_http.get(f'/c/{self.cell.pk}')
        self.assertRedirects(
            cell_response,
            f'/storage-cells/?cabinet={self.cell.cabinet.number}&open_cell={self.cell.pk}',
        )

    def test_qr_links_carry_no_trailing_slash(self):
        """Косая черта стоит ровно тот символ, который на этикетке заказа
        переводит код с 25 модулей на 29."""
        from core.views import qr_url

        self.assertEqual(qr_url('http://100.108.92.92', 'o', 123),
                         'http://100.108.92.92/o/123')

    @override_settings(LABEL_BASE_URL='http://100.108.92.92')
    def test_every_label_type_points_at_the_configured_address(self):
        """Все этикетки, а не только те, о которых вспомнили."""
        equipment_model = EquipmentModel.objects.create(name='БУАД-QR')
        equipment = Equipment.objects.create(model=equipment_model, serial_number='QR-1')
        client = ClientModel.objects.create(name='Заказчик QR')
        order = RepairOrder.objects.create(
            order_number='LT-QR-1', client=client, date_received=datetime.date.today()
        )
        roe = RepairOrderEquipment.objects.create(repair_order=order, equipment=equipment)

        pages = {
            f'/parts/{self.part.pk}/label/': f'http://100.108.92.92/p/{self.part.pk}',
            f'/storage-cells/{self.cell.pk}/label/': f'http://100.108.92.92/c/{self.cell.pk}',
            f'/repair-orders/{order.pk}/equipment/{roe.pk}/label/':
                f'http://100.108.92.92/o/{order.pk}',
        }
        for page, expected in pages.items():
            with self.subTest(page=page):
                with patch('core.views.generate_qr_image') as qr:
                    qr.return_value = 'data:image/png;base64,x'
                    self.client_http.get(page)
                self.assertEqual(qr.call_args[0][0], expected)


class PiSettingsLabelTests(TestCase):
    """Настройки Raspberry Pi: адрес этикеток и то, что по нему пускают."""

    def _load(self, **env):
        """Загружает settings_pi с заданным окружением.

        Модуль настроек читает переменные при импорте, поэтому его
        приходится перезагружать, а не подменять значения после.
        """
        import importlib
        from unittest.mock import patch as patch_env

        environment = {'SECRET_KEY': 'test-key-for-settings'}
        environment.update(env)
        with patch_env.dict('os.environ', environment, clear=False):
            module = importlib.import_module('lifteam.settings_pi')
            return importlib.reload(module)

    def test_labels_point_at_the_tailscale_name_by_default(self):
        """Имя, а не адрес 100.x: этикетка — бумажка, адрес в ней впечатан
        навсегда, а 100.x меняется при заведении узла заново."""
        settings_pi = self._load(ALLOWED_HOSTS='192.168.1.50,localhost')

        self.assertEqual(settings_pi.LABEL_BASE_URL,
                         'http://lifteam.taile9b605.ts.net')

    def test_the_name_fits_the_qr_without_growing_it(self):
        """Опасение, что имя длиннее адреса и код помельчает, не оправдалось:
        27 и 39 символов дают одни и те же 29 модулей."""
        settings_pi = self._load(ALLOWED_HOSTS='localhost')
        link = f'{settings_pi.LABEL_BASE_URL}/o/1234'

        self.assertLessEqual(len(link), views.QR_MAX_CHARS)
        self.assertEqual(views.qr_length_warning([link]), '')

    def test_the_scanned_address_is_allowed(self):
        """Иначе отсканированный код приводит на «400 Bad Request»."""
        settings_pi = self._load(ALLOWED_HOSTS='192.168.1.50,localhost')

        self.assertIn('lifteam.taile9b605.ts.net', settings_pi.ALLOWED_HOSTS)
        self.assertIn('http://lifteam.taile9b605.ts.net',
                      settings_pi.CSRF_TRUSTED_ORIGINS)

    def test_a_custom_address_is_allowed_too(self):
        """Адрес можно переопределить в .env — например, дописав порт,
        если nginx не ставили."""
        settings_pi = self._load(LABEL_BASE_URL='http://100.64.0.7:8000')

        self.assertEqual(settings_pi.LABEL_BASE_URL, 'http://100.64.0.7:8000')
        # В ALLOWED_HOSTS порт не указывается, в доверенных источниках — да
        self.assertIn('100.64.0.7', settings_pi.ALLOWED_HOSTS)
        self.assertIn('http://100.64.0.7:8000', settings_pi.CSRF_TRUSTED_ORIGINS)

    def test_an_empty_address_falls_back_to_the_print_page(self):
        """Пустая настройка — прежнее поведение: адрес берётся из запроса."""
        settings_pi = self._load(LABEL_BASE_URL='')

        self.assertEqual(settings_pi.LABEL_BASE_URL, '')
        self.assertNotIn('100.108.92.92', settings_pi.ALLOWED_HOSTS)


class UpdaterTests(TestCase):
    """Обновление через интерфейс. Приложение не имеет прав root и лишь
    оставляет заявку — проверяем, что в заявку нельзя подсунуть что угодно
    и что попасть на страницу может только администратор."""

    def setUp(self):
        self.admin = Employee.objects.create_superuser(
            username='upd_admin', full_name='Админ', password='pass'
        )
        self.warehouse = Employee.objects.create_user(
            username='upd_wh', full_name='Кладовщик', password='pass', role='warehouse'
        )
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_page_requires_admin_role(self):
        client = TestClient()
        client.force_login(self.warehouse)
        response = client.get('/management/update/', follow=True)
        self.assertRedirects(response, '/')

    def test_page_open_for_admin(self):
        client = TestClient()
        client.force_login(self.admin)
        self.assertEqual(client.get('/management/update/').status_code, 200)

    def test_status_endpoint_requires_admin(self):
        client = TestClient()
        client.force_login(self.warehouse)
        response = client.get('/management/update/status/', follow=True)
        self.assertRedirects(response, '/')

    def test_request_update_rejects_command_injection(self):
        from core import updater

        for evil in ('; rm -rf /', '$(whoami)', '../../etc/passwd', 'main', 'HEAD', ''):
            with self.subTest(target=evil):
                with self.assertRaises(ValueError):
                    updater.request_update(evil)

    def test_request_update_accepts_valid_targets(self):
        from core import updater

        tmp_path = Path(self.tmp.name)
        with patch.object(updater, 'REQUEST_FILE', tmp_path / '.update-request'), \
             patch.object(updater, 'STATUS_FILE', tmp_path / '.update-status'):
            updater.request_update('latest', requested_by='upd_admin')
            written = json.loads((tmp_path / '.update-request').read_text(encoding='utf-8'))

        self.assertEqual(written['target'], 'latest')
        self.assertEqual(written['requested_by'], 'upd_admin')

    def test_request_update_accepts_commit_hash(self):
        from core import updater

        tmp_path = Path(self.tmp.name)
        with patch.object(updater, 'REQUEST_FILE', tmp_path / '.update-request'), \
             patch.object(updater, 'STATUS_FILE', tmp_path / '.update-status'):
            updater.request_update('a1b2c3d')
            written = json.loads((tmp_path / '.update-request').read_text(encoding='utf-8'))

        self.assertEqual(written['target'], 'a1b2c3d')

    def test_no_request_file_written_for_invalid_target(self):
        """Заявка не должна появляться, если значение отвергнуто."""
        from core import updater

        tmp_path = Path(self.tmp.name)
        request_file = tmp_path / '.update-request'
        with patch.object(updater, 'REQUEST_FILE', request_file), \
             patch.object(updater, 'STATUS_FILE', tmp_path / '.update-status'):
            with self.assertRaises(ValueError):
                updater.request_update('; reboot')

        self.assertFalse(request_file.exists())


class UrlRoutingTests(TestCase):
    def test_management_users_not_shadowed_by_django_admin(self):
        """Регрессия: /admin/users/ раньше перехватывался встроенной админкой Django
        (path('admin/', admin.site.urls) match'ился раньше core.urls)."""
        match = resolve('/management/users/')
        self.assertEqual(match.func.__name__, 'admin_users')


class SqliteConfigurationTests(TestCase):
    def test_busy_timeout_applied_to_connection(self):
        """Без увеличенного таймаута одновременная работа даёт 'database is locked'."""
        with connection.cursor() as cursor:
            timeout = cursor.execute('PRAGMA busy_timeout;').fetchone()[0]
        self.assertEqual(timeout, 30000)

    def test_handler_ignores_non_sqlite_backends(self):
        """На PostgreSQL PRAGMA-команды недопустимы — обработчик обязан выйти сразу."""
        from .signals import configure_sqlite

        fake_connection = type('FakeConnection', (), {
            'vendor': 'postgresql',
            'cursor': lambda self: (_ for _ in ()).throw(
                AssertionError('cursor() не должен вызываться для PostgreSQL')
            ),
        })()
        configure_sqlite(sender=None, connection=fake_connection)


class BackupRestoreTests(TestCase):
    """Проверяют, что копии действительно восстановимы, а не просто создаются."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name)
        self.source_db = self.tmp_path / 'source.sqlite3'

        con = sqlite3.connect(self.source_db)
        con.execute('CREATE TABLE parts (id INTEGER PRIMARY KEY, name TEXT);')
        con.executemany(
            'INSERT INTO parts (name) VALUES (?);',
            [('Резистор',), ('Конденсатор',), ('Диод',)],
        )
        con.commit()
        con.close()

    def _run_backup(self, **kwargs):
        db_config = settings.DATABASES['default']
        with patch.dict(db_config, {'NAME': str(self.source_db)}):
            call_command('backup_db', output=str(self.tmp_path / 'backups'), verbosity=0, **kwargs)

    def test_backup_is_created_and_restorable(self):
        self._run_backup()

        backups = list((self.tmp_path / 'backups').glob('db_*.sqlite3'))
        self.assertEqual(len(backups), 1)

        con = sqlite3.connect(backups[0])
        try:
            self.assertEqual(con.execute('PRAGMA integrity_check;').fetchone()[0], 'ok')
            names = [row[0] for row in con.execute('SELECT name FROM parts ORDER BY id;')]
        finally:
            con.close()
        self.assertEqual(names, ['Резистор', 'Конденсатор', 'Диод'])

    def test_backup_reflects_data_written_after_previous_backup(self):
        self._run_backup()

        con = sqlite3.connect(self.source_db)
        con.execute("INSERT INTO parts (name) VALUES ('Стабилитрон');")
        con.commit()
        con.close()

        self._run_backup()

        backups = sorted((self.tmp_path / 'backups').glob('db_*.sqlite3'))
        # Вторая копия за ту же секунду не должна затирать первую
        self.assertEqual(len(backups), 2)

        counts = []
        for path in backups:
            con = sqlite3.connect(path)
            try:
                counts.append(con.execute('SELECT COUNT(*) FROM parts;').fetchone()[0])
            finally:
                con.close()
        self.assertEqual(counts, [3, 4])

    def test_gzip_backup_is_compressed_and_readable(self):
        self._run_backup(gzip=True)

        backups = list((self.tmp_path / 'backups').glob('db_*.sqlite3.gz'))
        self.assertEqual(len(backups), 1)
        self.assertFalse(list((self.tmp_path / 'backups').glob('db_*.sqlite3')))

    def test_retention_keeps_only_requested_number(self):
        from core.management.commands.backup_db import Command

        backup_dir = self.tmp_path / 'retention'
        backup_dir.mkdir()
        for i in range(6):
            (backup_dir / f'db_2026-01-0{i}_120000.sqlite3').write_bytes(b'x')

        removed = Command()._prune(backup_dir, keep=2)

        self.assertEqual(removed, 4)
        remaining = sorted(p.name for p in backup_dir.glob('db_*.sqlite3'))
        self.assertEqual(remaining, [
            'db_2026-01-04_120000.sqlite3',
            'db_2026-01-05_120000.sqlite3',
        ])

    def test_retention_ignores_sqlite_sidecar_files(self):
        """Файлы -wal/-shm не должны занимать места в квоте хранения копий."""
        from core.management.commands.backup_db import Command

        backup_dir = self.tmp_path / 'sidecars'
        backup_dir.mkdir()
        for i in range(3):
            base = backup_dir / f'db_2026-01-0{i}_120000.sqlite3'
            base.write_bytes(b'x')
            base.with_name(base.name + '-wal').write_bytes(b'x')
            base.with_name(base.name + '-shm').write_bytes(b'x')

        removed = Command()._prune(backup_dir, keep=2)

        self.assertEqual(removed, 1)
        remaining = sorted(
            p.name for p in backup_dir.glob('db_*.sqlite3')
            if not p.name.endswith(('-wal', '-shm'))
        )
        self.assertEqual(remaining, [
            'db_2026-01-01_120000.sqlite3',
            'db_2026-01-02_120000.sqlite3',
        ])
        # служебные файлы удалённой копии тоже убраны
        self.assertFalse((backup_dir / 'db_2026-01-00_120000.sqlite3-wal').exists())

    def test_retention_disabled_keeps_everything(self):
        from core.management.commands.backup_db import Command

        backup_dir = self.tmp_path / 'keep-all'
        backup_dir.mkdir()
        for i in range(3):
            (backup_dir / f'db_2026-01-0{i}_120000.sqlite3').write_bytes(b'x')

        self.assertEqual(Command()._prune(backup_dir, keep=0), 0)
        self.assertEqual(len(list(backup_dir.glob('db_*.sqlite3'))), 3)

    def test_restore_refuses_corrupted_backup(self):
        """Повреждённая копия не должна затирать рабочую базу."""
        from django.core.management.base import CommandError

        corrupted = self.tmp_path / 'corrupted.sqlite3'
        corrupted.write_bytes(b'this is not a database')

        with self.assertRaises(CommandError):
            call_command('restore_db', str(corrupted), yes=True, verbosity=0)


class ReportExportTests(TestCase):
    """Выгрузка отчётов в Excel: состав строк, учёт фильтров, итог по долгам."""

    def setUp(self):
        self.admin = Employee.objects.create_superuser(
            username='admin_rep', full_name='Админ отчётов', password='pass'
        )
        self.client_http = TestClient()
        self.client_http.force_login(self.admin)

    def _sheet(self, url):
        resp = self.client_http.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('spreadsheetml', resp['Content-Type'])
        return openpyxl.load_workbook(io.BytesIO(resp.content)).active

    def _rows(self, ws):
        """Строки листа без шапки, в виде словарей по названиям колонок."""
        headers = [c.value for c in ws[1]]
        return [
            dict(zip(headers, [c.value for c in row]))
            for row in ws.iter_rows(min_row=2)
        ]

    # --- План закупок ---

    def test_purchase_plan_export_lists_only_parts_below_minimum(self):
        SparePart.objects.create(part_number='LOW-1', name='Не хватает', current_stock=1, min_stock=5)
        SparePart.objects.create(part_number='OK-1', name='В норме', current_stock=10, min_stock=5)

        rows = self._rows(self._sheet('/reports/purchase-plan/export/'))

        # Последняя строка — «Итого»: файл уходит поставщику и начальству,
        # и сумму из него достают в первую очередь
        self.assertEqual([r['Артикул'] for r in rows], ['LOW-1', 'Итого'])
        self.assertEqual(rows[0]['Не хватает'], 4)

    def test_purchase_plan_export_totals_the_prices(self):
        SparePart.objects.create(part_number='LOW-P1', name='С ценой',
                                 current_stock=0, min_stock=4, price=Decimal('25.00'))
        SparePart.objects.create(part_number='LOW-P2', name='Без цены',
                                 current_stock=0, min_stock=3)

        rows = self._rows(self._sheet('/reports/purchase-plan/export/'))

        self.assertEqual(float(rows[0]['Сумма, ₽']), 100.0)
        self.assertIsNone(rows[1]['Сумма, ₽'])  # цену не заполняли — и суммы нет
        self.assertEqual(float(rows[-1]['Сумма, ₽']), 100.0)

    def test_purchase_plan_export_shows_cell_address(self):
        part = SparePart.objects.create(part_number='LOW-2', name='Деталь', current_stock=0, min_stock=3)
        cell = StorageCell.objects.create(cabinet=Cabinet.objects.get_or_create(number=1)[0], row_number=2, cell_row=3)
        cell.parts.add(part)

        rows = self._rows(self._sheet('/reports/purchase-plan/export/'))
        self.assertEqual(rows[0]['Ячейка'], 'К1-Р2-Я3')

    def test_purchase_plan_export_requires_login(self):
        resp = TestClient().get('/reports/purchase-plan/export/')
        self.assertEqual(resp.status_code, 302)

    # --- Журнал движений ---

    def test_movements_export_writes_timezone_aware_dates(self):
        """Регрессия: openpyxl отказывается записывать дату с часовым поясом,
        а все движения хранятся именно такими."""
        part = SparePart.objects.create(part_number='MOV-1', name='Деталь', current_stock=5)
        StockMovement.objects.create(
            part=part, quantity=3, movement_type='incoming',
            notes='Первый приход', created_by=self.admin
        )

        rows = self._rows(self._sheet('/reports/stock-movements/export/'))

        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(rows[0]['Дата'])
        self.assertEqual(rows[0]['Тип'], 'Приход')
        self.assertEqual(rows[0]['Сотрудник'], 'Админ отчётов')
        self.assertEqual(rows[0]['Примечания'], 'Первый приход')

    def test_movements_export_respects_type_filter(self):
        part = SparePart.objects.create(part_number='MOV-2', name='Деталь', current_stock=5)
        StockMovement.objects.create(part=part, quantity=1, movement_type='incoming')
        StockMovement.objects.create(part=part, quantity=2, movement_type='outgoing')

        rows = self._rows(self._sheet('/reports/stock-movements/export/?type=outgoing'))

        self.assertEqual([r['Количество'] for r in rows], [2])

    def test_movements_export_respects_part_filter(self):
        first = SparePart.objects.create(part_number='MOV-3', name='Первая')
        second = SparePart.objects.create(part_number='MOV-4', name='Вторая')
        StockMovement.objects.create(part=first, quantity=1, movement_type='incoming')
        StockMovement.objects.create(part=second, quantity=1, movement_type='incoming')

        rows = self._rows(self._sheet(f'/reports/stock-movements/export/?part={first.pk}'))
        self.assertEqual([r['Артикул'] for r in rows], ['MOV-3'])

    def test_movement_filters_ignore_unparsable_values(self):
        """Адрес правят руками; мусор в фильтре не должен ронять страницу."""
        part = SparePart.objects.create(part_number='MOV-5', name='Деталь')
        StockMovement.objects.create(part=part, quantity=1, movement_type='incoming')

        page = self.client_http.get('/reports/stock-movements/?part=abc&date_from=вчера&type=что-то')
        self.assertEqual(page.status_code, 200)

        rows = self._rows(self._sheet('/reports/stock-movements/export/?part=abc&date_from=вчера'))
        self.assertEqual(len(rows), 1)

    # --- Задолженности ---

    def _debtor_order(self, cost, payment_status='unpaid'):
        client_obj = ClientModel.objects.create(name=f'Должник {cost}')
        order = RepairOrder.objects.create(client=client_obj, payment_status=payment_status)
        model = EquipmentModel.objects.create(name=f'Модель {cost}')
        equipment = Equipment.objects.create(model=model, serial_number=f'SN-{cost}')
        RepairOrderEquipment.objects.create(
            repair_order=order, equipment=equipment, repair_cost=cost
        )
        return order

    def test_debtors_export_excludes_paid_orders(self):
        self._debtor_order(1000)
        self._debtor_order(2000, payment_status='paid')

        rows = self._rows(self._sheet('/reports/debtors/export/'))

        # одна строка долга плюс строка «Итого»
        self.assertEqual(len(rows), 2)
        self.assertEqual(float(rows[0]['Сумма, ₽']), 1000.0)

    def test_debtors_export_ends_with_total(self):
        self._debtor_order(1500)
        self._debtor_order(2500, payment_status='partially_paid')

        rows = self._rows(self._sheet('/reports/debtors/export/'))
        total_row = rows[-1]

        self.assertEqual(total_row['№ заказа'], 'Итого')
        # Итог по остаткам, а не по стоимостям: часть денег могла уже прийти
        self.assertEqual(float(total_row['Остаток, ₽']), 4000.0)

    def test_debtors_export_subtracts_payments(self):
        order = self._debtor_order(2000)
        Payment.objects.create(repair_order=order, amount=500)

        rows = self._rows(self._sheet('/reports/debtors/export/'))

        self.assertEqual(float(rows[0]['Оплачено, ₽']), 500.0)
        self.assertEqual(float(rows[0]['Остаток, ₽']), 1500.0)
        self.assertEqual(float(rows[-1]['Остаток, ₽']), 1500.0)

    def test_debtors_export_is_empty_without_debts(self):
        ws = self._sheet('/reports/debtors/export/')
        self.assertEqual(ws.max_row, 1)  # только шапка, без строки «Итого»

    def test_report_pages_offer_export(self):
        self._debtor_order(100)
        SparePart.objects.create(part_number='LOW-3', name='Деталь', current_stock=0, min_stock=1)

        for url in ('/reports/purchase-plan/', '/reports/stock-movements/', '/reports/debtors/'):
            with self.subTest(url=url):
                resp = self.client_http.get(url)
                self.assertEqual(resp.status_code, 200)
                self.assertIn('export/', resp.content.decode())

    def test_total_debt_matches_sum_of_orders(self):
        """Итог считается одним запросом — он должен совпадать с суммой по заказам."""
        from core.views import _debtor_orders, _total_debt

        self._debtor_order(700)
        self._debtor_order(300, payment_status='partially_paid')

        orders = _debtor_orders()
        self.assertEqual(
            _total_debt(orders),
            sum(order.total_repair_cost for order in orders)
        )


class PaginationFilterTests(TestCase):
    """Переход на следующую страницу не должен терять выставленные фильтры."""

    def setUp(self):
        self.admin = Employee.objects.create_superuser(
            username='admin_pag', full_name='Админ', password='pass'
        )
        self.client_http = TestClient()
        self.client_http.force_login(self.admin)

    def test_parts_pagination_keeps_voltage_filter(self):
        """Регрессия: ссылка на следующую страницу собиралась вручную и не
        включала фильтры по напряжению, току и остатку — на второй странице
        показывались детали, не проходящие фильтр."""
        for i in range(30):
            SparePart.objects.create(part_number=f'PAG-{i:03d}', name='Деталь', voltage=12)

        resp = self.client_http.get('/parts/?voltage_from=5')
        content = resp.content.decode()

        self.assertIn('page=2', content)
        self.assertIn('voltage_from=5', content)

    def test_movements_pagination_keeps_date_filter(self):
        part = SparePart.objects.create(part_number='PAG-MOV', name='Деталь')
        for _ in range(60):
            StockMovement.objects.create(part=part, quantity=1, movement_type='incoming')

        resp = self.client_http.get('/reports/stock-movements/?date_from=2020-01-01')
        content = resp.content.decode()

        self.assertIn('page=2', content)
        self.assertIn('date_from=2020-01-01', content)


class WarrantyTests(TestCase):
    """Гарантия на ремонт: отсчёт от даты завершения заказа."""

    def setUp(self):
        self.admin = Employee.objects.create_superuser(
            username='admin_war', full_name='Админ', password='pass'
        )
        self.client_http = TestClient()
        self.client_http.force_login(self.admin)

        self.client_obj = ClientModel.objects.create(name='Заказчик')
        self.model = EquipmentModel.objects.create(name='БУАД-3')
        self.equipment = Equipment.objects.create(model=self.model, serial_number='W-001')

    def _repair(self, completed_at, equipment=None):
        """Завершённый ремонт единицы с заданной датой завершения."""
        order = RepairOrder.objects.create(client=self.client_obj, status='shipped')
        # date_completed заполняется сменой статуса, в тесте ставим напрямую
        RepairOrder.objects.filter(pk=order.pk).update(date_completed=completed_at)
        order.refresh_from_db()
        return RepairOrderEquipment.objects.create(
            repair_order=order, equipment=equipment or self.equipment, repair_cost=1000
        )

    # --- арифметика срока ---

    def test_add_months_keeps_day_of_month(self):
        from core.models import add_months
        from datetime import datetime

        self.assertEqual(
            add_months(datetime(2026, 8, 12), 12), datetime(2027, 8, 12)
        )

    def test_add_months_clamps_to_last_day(self):
        """29 февраля плюс год — 28 февраля, а не ошибка."""
        from core.models import add_months
        from datetime import datetime

        self.assertEqual(
            add_months(datetime(2024, 2, 29), 12), datetime(2025, 2, 28)
        )

    def test_warranty_uses_calendar_year_not_365_days(self):
        """В високосный год гарантия должна кончаться в ту же дату, а не на день раньше."""
        completed = timezone.make_aware(datetime.datetime(2024, 3, 1, 12, 0))
        visit = self._repair(completed)

        self.assertEqual(timezone.localtime(visit.warranty_until).date(),
                         datetime.date(2025, 3, 1))

    # --- границы ---

    def test_recent_repair_is_under_warranty(self):
        visit = self._repair(timezone.now() - datetime.timedelta(days=30))

        self.assertTrue(visit.is_under_warranty)
        self.assertGreater(visit.warranty_days_left, 300)

    def test_repair_just_inside_the_year_is_still_covered(self):
        visit = self._repair(timezone.now() - datetime.timedelta(days=364))
        self.assertTrue(visit.is_under_warranty)

    def test_repair_older_than_the_year_is_not_covered(self):
        visit = self._repair(timezone.now() - datetime.timedelta(days=400))

        self.assertFalse(visit.is_under_warranty)
        self.assertLess(visit.warranty_days_left, 0)

    def test_unfinished_order_has_no_warranty_yet(self):
        order = RepairOrder.objects.create(client=self.client_obj)
        visit = RepairOrderEquipment.objects.create(
            repair_order=order, equipment=self.equipment
        )

        self.assertIsNone(visit.warranty_until)
        self.assertFalse(visit.is_under_warranty)
        self.assertIsNone(visit.warranty_days_left)

    @override_settings(WARRANTY_MONTHS=0)
    def test_zero_months_disables_warranty(self):
        visit = self._repair(timezone.now() - datetime.timedelta(days=1))

        self.assertIsNone(visit.warranty_until)
        self.assertEqual(Equipment.warranty_map([self.equipment]), {})

    @override_settings(WARRANTY_MONTHS=6)
    def test_period_is_configurable(self):
        visit = self._repair(timezone.now() - datetime.timedelta(days=200))
        self.assertFalse(visit.is_under_warranty)

    # --- поиск действующей гарантии ---

    def test_active_warranty_picks_the_latest_repair(self):
        self._repair(timezone.now() - datetime.timedelta(days=300))
        recent = self._repair(timezone.now() - datetime.timedelta(days=10))

        found = self.equipment.active_warranty()
        self.assertEqual(found.pk, recent.pk)

    def test_active_warranty_is_none_when_all_repairs_are_old(self):
        self._repair(timezone.now() - datetime.timedelta(days=500))
        self.assertIsNone(self.equipment.active_warranty())

    def test_active_warranty_can_exclude_current_order(self):
        """На странице заказа его собственная гарантия не должна выглядеть
        как гарантия по прошлому ремонту."""
        visit = self._repair(timezone.now() - datetime.timedelta(days=10))

        self.assertIsNotNone(self.equipment.active_warranty())
        self.assertIsNone(
            self.equipment.active_warranty(exclude_order_id=visit.repair_order_id)
        )

    def test_warranty_map_covers_many_units_in_one_query(self):
        other = Equipment.objects.create(model=self.model, serial_number='W-002')
        cold = Equipment.objects.create(model=self.model, serial_number='W-003')
        self._repair(timezone.now() - datetime.timedelta(days=10))
        self._repair(timezone.now() - datetime.timedelta(days=20), equipment=other)
        self._repair(timezone.now() - datetime.timedelta(days=500), equipment=cold)

        units = [self.equipment, other, cold]
        with self.assertNumQueries(1):
            found = Equipment.warranty_map(units)

        self.assertEqual(set(found), {self.equipment.pk, other.pk})

    # --- интерфейс ---

    def test_history_page_shows_active_warranty(self):
        self._repair(timezone.now() - datetime.timedelta(days=10))

        resp = self.client_http.get(f'/equipment/{self.equipment.pk}/history/')
        self.assertContains(resp, 'На гарантии до')

    def test_history_page_marks_expired_warranty(self):
        self._repair(timezone.now() - datetime.timedelta(days=500))

        resp = self.client_http.get(f'/equipment/{self.equipment.pk}/history/')
        self.assertNotContains(resp, 'На гарантии до')
        self.assertContains(resp, 'истекла')

    def test_equipment_list_shows_warranty_badge(self):
        visit = self._repair(timezone.now() - datetime.timedelta(days=10))
        expiry = timezone.localtime(visit.warranty_until).strftime('%d.%m.%Y')

        resp = self.client_http.get('/equipment/')
        self.assertContains(resp, f'до {expiry}')

    def test_equipment_list_leaves_uncovered_units_empty(self):
        self._repair(timezone.now() - datetime.timedelta(days=500))

        resp = self.client_http.get('/equipment/')
        self.assertIsNone(resp.context['equipment'][0].warranty)

    def test_intake_hint_reports_warranty(self):
        visit = self._repair(timezone.now() - datetime.timedelta(days=10))

        resp = self.client_http.get(
            f'/ajax/equipment/{self.equipment.pk}/history-summary/'
        )
        data = resp.json()

        self.assertTrue(data['under_warranty'])
        self.assertEqual(data['warranty_order_number'], visit.repair_order.order_number)
        self.assertTrue(data['warranty_until'])

    def test_intake_hint_without_warranty(self):
        self._repair(timezone.now() - datetime.timedelta(days=500))

        data = self.client_http.get(
            f'/ajax/equipment/{self.equipment.pk}/history-summary/'
        ).json()

        self.assertFalse(data['under_warranty'])
        self.assertEqual(data['warranty_until'], '')

    def test_order_page_flags_repeat_visit_under_warranty(self):
        """Регрессия смысла: гарантия по прошлому заказу должна быть видна
        в новом, иначе повторный ремонт примут как обычный платный."""
        old = self._repair(timezone.now() - datetime.timedelta(days=10))

        new_order = RepairOrder.objects.create(client=self.client_obj)
        RepairOrderEquipment.objects.create(
            repair_order=new_order, equipment=self.equipment
        )

        resp = self.client_http.get(f'/repair-orders/{new_order.pk}/')
        self.assertContains(resp, old.repair_order.order_number)

    def test_order_page_does_not_flag_its_own_warranty_as_previous(self):
        visit = self._repair(timezone.now() - datetime.timedelta(days=10))

        resp = self.client_http.get(f'/repair-orders/{visit.repair_order.pk}/')
        shown = resp.context['order_equipments'][0]

        self.assertIsNone(shown.previous_warranty)
        self.assertTrue(shown.is_under_warranty)


class OrderSearchTests(TestCase):
    """Расширенный поиск заказов: по всем полям, которые сотрудник может помнить."""

    def setUp(self):
        self.admin = Employee.objects.create_superuser(
            username='admin_search', full_name='Админ', password='pass'
        )
        self.client_http = TestClient()
        self.client_http.force_login(self.admin)

        self.alpha = ClientModel.objects.create(name='ООО Альфа', inn='7700000001')
        self.beta = ClientModel.objects.create(name='ООО Бета', inn='7700000002')
        self.buad = EquipmentModel.objects.create(name='БУАД')
        self.eco = EquipmentModel.objects.create(name='Экодрайв')

        self.order_a = RepairOrder.objects.create(
            client=self.alpha, status='repair', payment_status='unpaid',
            invoice_number='СЧ-100', tracking_number='TRK-777',
            fault_description='Не запускается двигатель',
        )
        RepairOrderEquipment.objects.create(
            repair_order=self.order_a,
            equipment=Equipment.objects.create(model=self.buad, serial_number='БУАД-1234'),
        )

        self.order_b = RepairOrder.objects.create(
            client=self.beta, status='shipped', payment_status='paid',
            invoice_number='СЧ-200',
        )
        RepairOrderEquipment.objects.create(
            repair_order=self.order_b,
            equipment=Equipment.objects.create(model=self.eco, serial_number='ECO-9'),
            fault_description='Скрип при подъёме',
        )

    def _found(self, query=''):
        resp = self.client_http.get(f'/repair-orders/{query}')
        self.assertEqual(resp.status_code, 200)
        return {order.pk for order in resp.context['orders']}

    # --- текстовый поиск ---

    def test_search_by_order_number(self):
        self.assertEqual(self._found(f'?q={self.order_a.order_number}'), {self.order_a.pk})

    def test_search_by_client_name(self):
        self.assertEqual(self._found('?q=Альфа'), {self.order_a.pk})

    def test_search_by_client_inn(self):
        self.assertEqual(self._found('?q=7700000002'), {self.order_b.pk})

    def test_search_by_invoice_number(self):
        self.assertEqual(self._found('?q=СЧ-200'), {self.order_b.pk})

    def test_search_by_tracking_number(self):
        self.assertEqual(self._found('?q=TRK-777'), {self.order_a.pk})

    def test_search_by_order_fault_description(self):
        self.assertEqual(self._found('?q=двигатель'), {self.order_a.pk})

    def test_search_by_unit_fault_description(self):
        """Неисправность может быть записана у единицы, а не у заказа."""
        self.assertEqual(self._found('?q=скрип'), {self.order_b.pk})

    def test_search_by_equipment_model(self):
        self.assertEqual(self._found('?q=Экодрайв'), {self.order_b.pk})

    def test_search_by_serial_in_another_spelling(self):
        """«буад 1234» должен находить заказ с «БУАД-1234» — как в оборудовании."""
        self.assertEqual(self._found('?q=буад 1234'), {self.order_a.pk})

    def test_search_finds_nothing_for_unknown_text(self):
        self.assertEqual(self._found('?q=такого+нет'), set())

    def test_order_with_several_units_is_listed_once(self):
        """Регрессия: соединение с оборудованием размножало строки заказа."""
        RepairOrderEquipment.objects.create(
            repair_order=self.order_a,
            equipment=Equipment.objects.create(model=self.buad, serial_number='БУАД-5678'),
        )
        resp = self.client_http.get('?q=Альфа'.join(['/repair-orders/', '']))
        listed = [o.pk for o in resp.context['orders']]

        self.assertEqual(listed.count(self.order_a.pk), 1)

    # --- фильтры ---

    def test_filter_by_status(self):
        self.assertEqual(self._found('?status=repair'), {self.order_a.pk})

    def test_filter_by_payment_status(self):
        self.assertEqual(self._found('?payment_status=paid'), {self.order_b.pk})

    def test_filter_by_client(self):
        self.assertEqual(self._found(f'?client={self.beta.pk}'), {self.order_b.pk})

    def test_filter_by_equipment_model(self):
        self.assertEqual(self._found(f'?model={self.buad.pk}'), {self.order_a.pk})

    def test_filter_by_date_range(self):
        today = timezone.localdate().isoformat()
        self.assertEqual(
            self._found(f'?date_from={today}&date_to={today}'),
            {self.order_a.pk, self.order_b.pk},
        )

    def test_date_range_excludes_other_days(self):
        tomorrow = (timezone.localdate() + datetime.timedelta(days=1)).isoformat()
        self.assertEqual(self._found(f'?date_from={tomorrow}'), set())

    def test_filters_combine(self):
        self.assertEqual(
            self._found(f'?q=Альфа&status=repair&payment_status=unpaid'),
            {self.order_a.pk},
        )

    def test_contradictory_filters_find_nothing(self):
        self.assertEqual(self._found('?q=Альфа&status=shipped'), set())

    # --- устойчивость и мелочи интерфейса ---

    def test_garbage_in_filters_does_not_break_the_page(self):
        found = self._found('?client=abc&model=xyz&date_from=вчера&status=нет-такого')
        self.assertEqual(found, {self.order_a.pk, self.order_b.pk})

    def test_reset_link_shown_only_with_active_filters(self):
        with_filter = self.client_http.get('/repair-orders/?status=repair')
        without = self.client_http.get('/repair-orders/')

        self.assertTrue(with_filter.context['filters_active'])
        self.assertFalse(without.context['filters_active'])

    def test_found_count_counts_all_matches_not_just_the_page(self):
        for i in range(30):
            RepairOrder.objects.create(client=self.alpha, status='accepted')

        resp = self.client_http.get('/repair-orders/?status=accepted')
        self.assertEqual(resp.context['found_count'], 30)
        self.assertEqual(len(resp.context['orders']), 25)

    def test_pagination_keeps_filters(self):
        for i in range(30):
            RepairOrder.objects.create(client=self.alpha, status='accepted')

        content = self.client_http.get('/repair-orders/?status=accepted').content.decode()
        self.assertIn('status=accepted', content)
        self.assertIn('page=2', content)


class CyrillicSearchTests(TestCase):
    """Поиск без учёта регистра для кириллицы.

    Встроенный LIKE в SQLite приводит к одному регистру только латиницу,
    поэтому «скрип» не находил «Скрип». Заменённая функция like() чинит это
    сразу везде, где используется icontains, — проверяем на разных разделах.
    """

    def setUp(self):
        self.admin = Employee.objects.create_superuser(
            username='admin_cyr', full_name='Админ', password='pass'
        )
        self.client_http = TestClient()
        self.client_http.force_login(self.admin)

    def test_lowercase_query_finds_capitalised_client(self):
        ClientModel.objects.create(name='ООО Лифтсервис')

        resp = self.client_http.get('/clients/?q=лифтсервис')
        self.assertEqual(len(resp.context['clients']), 1)

    def test_uppercase_query_finds_lowercase_part(self):
        SparePart.objects.create(part_number='C-1', name='конденсатор плёночный')

        resp = self.client_http.get('/parts/?q=КОНДЕНСАТОР')
        self.assertEqual(len(resp.context['parts']), 1)

    def test_mixed_case_finds_equipment_model(self):
        model = EquipmentModel.objects.create(name='Экодрайв')
        Equipment.objects.create(model=model, serial_number='E-1')

        resp = self.client_http.get('/equipment/?q=эКоДрАйВ')
        self.assertEqual(len(resp.context['equipment']), 1)

    def test_latin_search_still_works(self):
        SparePart.objects.create(part_number='RES-10', name='Resistor')

        resp = self.client_http.get('/parts/?q=resistor')
        self.assertEqual(len(resp.context['parts']), 1)

    def test_percent_in_query_is_not_a_wildcard(self):
        """Регрессия замены LIKE: символы шаблона внутри запроса Django
        экранирует, и они должны искаться буквально."""
        SparePart.objects.create(part_number='P-1', name='Резистор 5% точность')
        SparePart.objects.create(part_number='P-2', name='Резистор обычный')

        resp = self.client_http.get('/parts/?q=5%25')
        self.assertEqual([p.part_number for p in resp.context['parts']], ['P-1'])

    def test_underscore_in_query_is_not_a_wildcard(self):
        SparePart.objects.create(part_number='A_1', name='С подчёркиванием')
        SparePart.objects.create(part_number='AX1', name='Без него')

        resp = self.client_http.get('/parts/?q=A_1')
        self.assertEqual([p.part_number for p in resp.context['parts']], ['A_1'])

    def test_like_helper_handles_null(self):
        from core.signals import _unicode_like

        self.assertFalse(_unicode_like('%текст%', None))
        self.assertFalse(_unicode_like(None, 'текст'))


class EquipmentShortLinkTests(TestCase):
    """Короткий адрес единицы оборудования.

    Отдельную этикетку оборудования больше не печатают — нужную наклейку
    даёт заказ. Сам адрес оставлен: коды с уже наклеенных этикеток должны
    открываться и дальше.
    """

    def setUp(self):
        self.admin = Employee.objects.create_superuser(
            username='admin_lbl', full_name='Админ', password='pass'
        )
        self.client_http = TestClient()
        self.client_http.force_login(self.admin)

        model = EquipmentModel.objects.create(name='БУАД')
        self.equipment = Equipment.objects.create(model=model, serial_number='БУАД-1234')

    def test_short_url_opens_repair_history(self):
        resp = self.client_http.get(f'/e/{self.equipment.pk}/')
        self.assertRedirects(resp, f'/equipment/{self.equipment.pk}/history/')

    def test_short_url_requires_login(self):
        resp = TestClient().get(f'/e/{self.equipment.pk}/')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login/', resp['Location'])

    def test_short_url_404_for_missing_equipment(self):
        resp = self.client_http.get('/e/999999/')
        self.assertEqual(resp.status_code, 404)

    def test_the_label_page_is_gone(self):
        """Печать этикетки оборудования вне заказа убрана в v2.47.0."""
        resp = self.client_http.get(f'/equipment/{self.equipment.pk}/label/')
        self.assertEqual(resp.status_code, 404)


class OrderLabelLinkTests(TestCase):
    """Этикетка оборудования в заказе: ссылка в QR вместо текста «LT-…/1»."""

    def setUp(self):
        self.admin = Employee.objects.create_superuser(
            username='admin_olbl', full_name='Админ', password='pass'
        )
        self.client_http = TestClient()
        self.client_http.force_login(self.admin)

        client_obj = ClientModel.objects.create(name='Заказчик')
        model = EquipmentModel.objects.create(name='БУАД-3')
        self.order = RepairOrder.objects.create(client=client_obj)
        self.roe = RepairOrderEquipment.objects.create(
            repair_order=self.order,
            equipment=Equipment.objects.create(model=model, serial_number='БУАД-1234'),
        )

    def _label_url(self):
        return f'/repair-orders/{self.order.pk}/equipment/{self.roe.pk}/label/'

    def test_short_url_opens_the_order(self):
        resp = self.client_http.get(f'/o/{self.order.pk}/')
        self.assertRedirects(resp, f'/repair-orders/{self.order.pk}/')

    def test_short_url_requires_login(self):
        resp = TestClient().get(f'/o/{self.order.pk}/')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login/', resp['Location'])

    def test_short_url_404_for_missing_order(self):
        self.assertEqual(self.client_http.get('/o/999999/').status_code, 404)

    def test_label_encodes_link_to_the_order(self):
        with patch('core.views.generate_qr_image') as qr:
            qr.return_value = 'data:image/png;base64,x'
            self.client_http.get(self._label_url())

        encoded = qr.call_args[0][0]
        self.assertTrue(encoded.endswith(f'/o/{self.order.pk}'), encoded)
        # Прежнее содержимое кода — «LT-2026-08-001/1» — больше не годится:
        # сканирование им ничего не открывало
        self.assertNotIn(self.order.order_number, encoded)

    @override_settings(LABEL_BASE_URL='http://192.168.1.50')
    def test_label_uses_configured_base_url(self):
        with patch('core.views.generate_qr_image') as qr:
            qr.return_value = 'data:image/png;base64,x'
            self.client_http.get(self._label_url())

        self.assertEqual(qr.call_args[0][0], f'http://192.168.1.50/o/{self.order.pk}')

    def test_position_still_printed_on_the_label(self):
        """Позиция в ссылку не входит, поэтому она обязана остаться на бумаге —
        в строке с номером заказа, вида «LT-2026-08-001/1»."""
        resp = self.client_http.get(self._label_url())
        content = resp.content.decode()

        self.assertContains(resp, self.order.order_number)
        self.assertIn('/1</span>', content)

    def test_the_label_has_no_emblem(self):
        """Кольцо с надписями занимало место вокруг кода и ограничивало его."""
        content = self.client_http.get(self._label_url()).content.decode()

        self.assertNotIn('<svg', content)
        self.assertNotIn('label-logo', content)

    def test_the_company_and_the_site_stay_on_the_label(self):
        """Эмблемы нет, но по этикетке должно быть видно, чья это железка."""
        response = self.client_http.get(self._label_url())

        self.assertContains(response, 'LIFT TEAM')
        self.assertContains(response, 'LIFTTEAM.RU')

    def test_the_phones_are_the_current_ones(self):
        response = self.client_http.get(self._label_url())

        self.assertContains(response, '+7 964 524 84 00')
        self.assertContains(response, '+7 977 760 10 89')
        self.assertNotContains(response, '282-40-31')

    def test_the_qr_is_the_common_size(self):
        """Размер кода один на всех этикетках: 12,3 мм."""
        response = self.client_http.get(self._label_url())

        self.assertContains(response, '12.3mm')


class OrderExportTests(TestCase):
    """Выгрузка списка заказов в Excel — с учётом фильтров страницы."""

    def setUp(self):
        self.admin = Employee.objects.create_superuser(
            username='admin_oexp', full_name='Админ', password='pass'
        )
        self.client_http = TestClient()
        self.client_http.force_login(self.admin)

        self.alpha = ClientModel.objects.create(name='ООО Альфа', inn='7700000001')
        model = EquipmentModel.objects.create(name='БУАД')

        self.paid = RepairOrder.objects.create(
            client=self.alpha, status='shipped', payment_status='paid',
            invoice_number='СЧ-1',
        )
        RepairOrderEquipment.objects.create(
            repair_order=self.paid,
            equipment=Equipment.objects.create(model=model, serial_number='SN-1'),
            repair_cost=1000,
        )

        self.unpaid = RepairOrder.objects.create(
            client=self.alpha, status='repair', payment_status='unpaid',
        )
        for serial, cost in (('SN-2', 2000), ('SN-3', 500)):
            RepairOrderEquipment.objects.create(
                repair_order=self.unpaid,
                equipment=Equipment.objects.create(model=model, serial_number=serial),
                repair_cost=cost,
            )

    def _rows(self, query=''):
        resp = self.client_http.get(f'/repair-orders/export/{query}')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('spreadsheetml', resp['Content-Type'])
        ws = openpyxl.load_workbook(io.BytesIO(resp.content)).active
        headers = [c.value for c in ws[1]]
        return [dict(zip(headers, [c.value for c in row])) for row in ws.iter_rows(min_row=2)]

    def test_export_lists_all_orders_with_total(self):
        rows = self._rows()

        self.assertEqual(len(rows), 3)  # два заказа плюс «Итого»
        self.assertEqual(rows[-1]['№ заказа'], 'Итого')
        self.assertEqual(float(rows[-1]['Сумма, ₽']), 3500.0)

    def test_export_respects_filters(self):
        rows = self._rows('?payment_status=unpaid')

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['№ заказа'], self.unpaid.order_number)
        self.assertEqual(float(rows[0]['Сумма, ₽']), 2500.0)

    def test_export_sums_all_units_of_an_order(self):
        """Регрессия: соединение с оборудованием при фильтрах могло задвоить сумму."""
        rows = self._rows(f'?q=Альфа&status=repair')

        self.assertEqual(len(rows), 2)
        self.assertEqual(float(rows[0]['Сумма, ₽']), 2500.0)

    def test_export_lists_every_unit_of_an_order(self):
        rows = self._rows('?payment_status=unpaid')
        self.assertIn('SN-2', rows[0]['Оборудование'])
        self.assertIn('SN-3', rows[0]['Оборудование'])

    def test_empty_result_has_no_total_row(self):
        ws_rows = self._rows('?q=такого-заказа-нет')
        self.assertEqual(ws_rows, [])

    def test_export_requires_login(self):
        resp = TestClient().get('/repair-orders/export/')
        self.assertEqual(resp.status_code, 302)

    def test_order_list_page_offers_export(self):
        resp = self.client_http.get('/repair-orders/')
        self.assertContains(resp, '/repair-orders/export/')


class EquipmentListFilterTests(TestCase):
    """Поиск по заказчику, фильтр по гарантии и выгрузка списка оборудования."""

    def setUp(self):
        self.admin = Employee.objects.create_superuser(
            username='admin_eqf', full_name='Админ', password='pass'
        )
        self.client_http = TestClient()
        self.client_http.force_login(self.admin)

        self.alpha = ClientModel.objects.create(name='ООО Альфа')
        model = EquipmentModel.objects.create(name='БУАД')

        self.covered = Equipment.objects.create(
            model=model, serial_number='SN-COVERED', current_client=self.alpha
        )
        self.expired = Equipment.objects.create(model=model, serial_number='SN-EXPIRED')
        self.never = Equipment.objects.create(model=model, serial_number='SN-NEVER')

        self._repair(self.covered, days_ago=10)
        self._repair(self.expired, days_ago=500)

    def _repair(self, equipment, days_ago):
        order = RepairOrder.objects.create(
            client=self.alpha, status='shipped'
        )
        RepairOrder.objects.filter(pk=order.pk).update(
            date_completed=timezone.now() - datetime.timedelta(days=days_ago)
        )
        return RepairOrderEquipment.objects.create(
            repair_order=order, equipment=equipment, repair_cost=100
        )

    def _serials(self, query=''):
        resp = self.client_http.get(f'/equipment/{query}')
        self.assertEqual(resp.status_code, 200)
        return {eq.serial_number for eq in resp.context['equipment']}

    def test_search_by_current_client(self):
        self.assertEqual(self._serials('?q=Альфа'), {'SN-COVERED'})

    def test_filter_shows_only_covered_equipment(self):
        self.assertEqual(self._serials('?warranty=active'), {'SN-COVERED'})

    def test_filter_shows_equipment_without_warranty(self):
        self.assertEqual(self._serials('?warranty=expired'), {'SN-EXPIRED', 'SN-NEVER'})

    def test_no_filter_shows_everything(self):
        self.assertEqual(len(self._serials()), 3)

    def test_unknown_filter_value_is_ignored(self):
        self.assertEqual(len(self._serials('?warranty=что-то')), 3)

    @override_settings(WARRANTY_MONTHS=0)
    def test_filter_does_nothing_when_warranty_is_off(self):
        self.assertEqual(len(self._serials('?warranty=active')), 3)

    def test_equipment_repaired_twice_is_listed_once(self):
        self._repair(self.covered, days_ago=20)
        self.assertEqual(len(self._serials('?warranty=active')), 1)

    def test_export_includes_warranty_and_repair_count(self):
        self._repair(self.covered, days_ago=20)

        resp = self.client_http.get('/equipment/export/?warranty=active')
        self.assertEqual(resp.status_code, 200)
        ws = openpyxl.load_workbook(io.BytesIO(resp.content)).active
        headers = [c.value for c in ws[1]]
        row = dict(zip(headers, [c.value for c in ws[2]]))

        self.assertEqual(row['Серийный номер'], 'SN-COVERED')
        self.assertEqual(row['Текущий заказчик'], 'ООО Альфа')
        self.assertEqual(row['Всего ремонтов'], 2)
        # openpyxl читает ячейку с датой обратно как datetime, сравниваем по дате
        self.assertEqual(
            row['Гарантия до'].date(),
            timezone.localtime(self.covered.active_warranty().warranty_until).date(),
        )
        self.assertEqual(ws.max_row, 2)  # шапка и одна единица

    def test_export_requires_login(self):
        self.assertEqual(TestClient().get('/equipment/export/').status_code, 302)


class ClientExportTests(TestCase):
    def setUp(self):
        self.admin = Employee.objects.create_superuser(
            username='admin_cexp', full_name='Админ', password='pass'
        )
        self.client_http = TestClient()
        self.client_http.force_login(self.admin)

        self.alpha = ClientModel.objects.create(name='ООО Альфа', inn='7700000001')
        ClientModel.objects.create(name='ООО Бета', inn='7700000002')

        model = EquipmentModel.objects.create(name='БУАД')
        for cost, payment in ((1000, 'unpaid'), (2000, 'paid')):
            order = RepairOrder.objects.create(client=self.alpha, payment_status=payment)
            RepairOrderEquipment.objects.create(
                repair_order=order,
                equipment=Equipment.objects.create(model=model, serial_number=f'SN-{cost}'),
                repair_cost=cost,
            )

    def _rows(self, query=''):
        resp = self.client_http.get(f'/clients/export/{query}')
        self.assertEqual(resp.status_code, 200)
        ws = openpyxl.load_workbook(io.BytesIO(resp.content)).active
        headers = [c.value for c in ws[1]]
        return [dict(zip(headers, [c.value for c in row])) for row in ws.iter_rows(min_row=2)]

    def test_export_counts_orders_and_debt(self):
        rows = {r['Название']: r for r in self._rows()}

        self.assertEqual(rows['ООО Альфа']['Заказов'], 2)
        # В долг попадает только неоплаченный заказ
        self.assertEqual(float(rows['ООО Альфа']['Долг, ₽']), 1000.0)
        self.assertEqual(rows['ООО Бета']['Заказов'], 0)
        self.assertEqual(float(rows['ООО Бета']['Долг, ₽']), 0.0)

    def test_export_respects_search(self):
        rows = self._rows('?q=Бета')
        self.assertEqual([r['Название'] for r in rows], ['ООО Бета'])

    def test_export_requires_login(self):
        self.assertEqual(TestClient().get('/clients/export/').status_code, 302)


class StockStateTests(TestCase):
    """Три состояния остатка вместо двух несогласованных определений.

    Раньше «мало на складе» считалось по-разному: сетка кассетниц красила
    ячейку по «остаток <= минимума», а дашборд, план закупок и список деталей
    отбирали строго ниже минимума. Деталь ровно на минимуме выглядела
    дефицитной, но в план закупок не попадала.
    """

    def setUp(self):
        self.admin = Employee.objects.create_superuser(
            username='admin_stock', full_name='Админ', password='pass'
        )
        self.client_http = TestClient()
        self.client_http.force_login(self.admin)

        self.below = SparePart.objects.create(
            part_number='ST-BELOW', name='Дефицит', current_stock=2, min_stock=5
        )
        self.at_min = SparePart.objects.create(
            part_number='ST-AT', name='На минимуме', current_stock=5, min_stock=5
        )
        self.ok = SparePart.objects.create(
            part_number='ST-OK', name='В норме', current_stock=9, min_stock=5
        )
        self.no_min = SparePart.objects.create(
            part_number='ST-NOMIN', name='Без минимума', current_stock=0, min_stock=0
        )

    # --- состояние детали ---

    def test_states(self):
        self.assertEqual(self.below.stock_state, 'below')
        self.assertEqual(self.at_min.stock_state, 'at_minimum')
        self.assertEqual(self.ok.stock_state, 'ok')

    def test_zero_minimum_is_not_a_shortage(self):
        """Минимум не задан — деталь не считается ни дефицитной, ни на минимуме."""
        self.assertEqual(self.no_min.stock_state, 'ok')

    def test_is_below_min_stock_matches_the_state(self):
        self.assertTrue(self.below.is_below_min_stock())
        self.assertFalse(self.at_min.is_below_min_stock())

    # --- отбор ---

    def test_queryset_below_minimum(self):
        found = SparePart.objects.below_minimum().values_list('part_number', flat=True)
        self.assertEqual(set(found), {'ST-BELOW'})

    def test_queryset_at_minimum(self):
        found = SparePart.objects.at_minimum().values_list('part_number', flat=True)
        self.assertEqual(set(found), {'ST-AT'})

    def test_purchase_plan_covers_only_shortage(self):
        resp = self.client_http.get('/reports/purchase-plan/')
        listed = {p.part_number for p in resp.context['parts']}
        self.assertEqual(listed, {'ST-BELOW'})

    # --- дашборд ---

    def test_dashboard_separates_shortage_from_at_minimum(self):
        resp = self.client_http.get('/')

        self.assertEqual(resp.context['low_stock_count'], 1)
        self.assertEqual(resp.context['at_minimum_count'], 1)
        self.assertEqual(
            {p.part_number for p in resp.context['low_stock_parts']}, {'ST-BELOW'}
        )
        self.assertEqual(
            {p.part_number for p in resp.context['at_minimum_parts']}, {'ST-AT'}
        )

    def test_dashboard_shows_both_groups(self):
        content = self.client_http.get('/').content.decode()
        self.assertIn('ST-BELOW', content)
        self.assertIn('ST-AT', content)
        self.assertIn('на минимуме', content)

    # --- ячейки ---

    def _cell_with(self, *parts):
        cell = StorageCell.objects.create(
            cabinet=Cabinet.objects.get_or_create(number=1)[0], row_number=1, cell_row=StorageCell.objects.count() + 1
        )
        cell.parts.add(*parts)
        return cell

    def test_cell_with_shortage_is_red(self):
        self.assertEqual(self._cell_with(self.below).get_status(), 'low_stock')

    def test_cell_at_minimum_is_its_own_status(self):
        self.assertEqual(self._cell_with(self.at_min).get_status(), 'at_minimum')

    def test_cell_shows_the_worst_state_of_its_parts(self):
        self.assertEqual(self._cell_with(self.at_min, self.below).get_status(), 'low_stock')

    def test_normal_and_empty_cells(self):
        self.assertEqual(self._cell_with(self.ok).get_status(), 'normal')
        self.assertEqual(self._cell_with().get_status(), 'free')


class PartStockFilterTests(TestCase):
    """Фильтры списка деталей: состояние остатка, диапазоны, устойчивость."""

    def setUp(self):
        self.admin = Employee.objects.create_superuser(
            username='admin_pf', full_name='Админ', password='pass'
        )
        self.client_http = TestClient()
        self.client_http.force_login(self.admin)

        SparePart.objects.create(part_number='PF-BELOW', name='Дефицит',
                                 current_stock=1, min_stock=5, voltage=12, current=2, power=1)
        SparePart.objects.create(part_number='PF-AT', name='На минимуме',
                                 current_stock=5, min_stock=5, voltage=24, current=5, power=10)
        SparePart.objects.create(part_number='PF-OK', name='В норме',
                                 current_stock=50, min_stock=5, voltage=48, current=10, power=100)

    def _found(self, query=''):
        resp = self.client_http.get(f'/parts/{query}')
        self.assertEqual(resp.status_code, 200)
        return {p.part_number for p in resp.context['parts']}

    def test_filter_by_package(self):
        """Корпус — такой же признак отбора, как тип: 0805 вместо 1206
        на плату не встанет, как бы ни совпадали характеристики."""
        SparePart.objects.filter(part_number='PF-OK').update(package='TO-220')
        SparePart.objects.filter(part_number='PF-AT').update(package='0805')

        self.assertEqual(self._found('?package=TO-220'), {'PF-OK'})

    def test_search_finds_the_package(self):
        SparePart.objects.filter(part_number='PF-OK').update(package='SOT-23')

        self.assertEqual(self._found('?q=SOT-23'), {'PF-OK'})

    def test_only_shortage(self):
        self.assertEqual(self._found('?stock_state=below'), {'PF-BELOW'})

    def test_only_at_minimum(self):
        self.assertEqual(self._found('?stock_state=at_minimum'), {'PF-AT'})

    def test_attention_covers_both(self):
        self.assertEqual(self._found('?stock_state=attention'), {'PF-BELOW', 'PF-AT'})

    def test_old_below_min_link_still_works(self):
        """Ссылки вида ?below_min=1 могли остаться в закладках."""
        self.assertEqual(self._found('?below_min=1'), {'PF-BELOW'})

    def test_stock_range(self):
        self.assertEqual(self._found('?stock_from=2&stock_to=10'), {'PF-AT'})

    def test_current_range_now_reaches_the_view(self):
        self.assertEqual(self._found('?current_from=4&current_to=6'), {'PF-AT'})

    def test_power_range_is_supported(self):
        self.assertEqual(self._found('?power_from=50'), {'PF-OK'})

    def test_ranges_combine_with_state(self):
        self.assertEqual(self._found('?stock_state=attention&voltage_from=20'), {'PF-AT'})

    def test_garbage_in_range_does_not_break_the_page(self):
        """Регрессия: голый float() на «5 В» ронял страницу целиком."""
        self.assertEqual(len(self._found('?voltage_from=5 В&stock_from=много')), 3)

    def test_comma_decimal_is_accepted(self):
        self.assertEqual(self._found('?voltage_from=23,5&voltage_to=24,5'), {'PF-AT'})

    def test_export_uses_the_same_filters(self):
        resp = self.client_http.get('/parts/export/?stock_state=below')
        ws = openpyxl.load_workbook(io.BytesIO(resp.content)).active
        self.assertEqual(ws.max_row, 2)  # шапка и одна деталь
        self.assertEqual(ws.cell(row=2, column=1).value, 'PF-BELOW')


class EquipmentHistoryExportTests(TestCase):
    def setUp(self):
        self.admin = Employee.objects.create_superuser(
            username='admin_hexp', full_name='Админ', password='pass'
        )
        self.client_http = TestClient()
        self.client_http.force_login(self.admin)

        self.client_obj = ClientModel.objects.create(name='ООО Альфа')
        model = EquipmentModel.objects.create(name='БУАД')
        self.equipment = Equipment.objects.create(model=model, serial_number='SN-100')
        self.other = Equipment.objects.create(model=model, serial_number='SN-200')

        self.order = RepairOrder.objects.create(
            client=self.client_obj, status='shipped', fault_description='Общая неисправность'
        )
        RepairOrder.objects.filter(pk=self.order.pk).update(
            date_completed=timezone.now() - datetime.timedelta(days=5)
        )
        self.visit = RepairOrderEquipment.objects.create(
            repair_order=self.order, equipment=self.equipment,
            fault_description='Не крутится', seal_numbers='П-1, П-2',
            initial_condition='Корпус цел', repair_cost=3000,
        )
        part = SparePart.objects.create(part_number='D-1', name='Диод', current_stock=10)
        RepairOrderDetail.objects.create(repair_order=self.order, part=part, quantity_used=2)

    def _rows(self, equipment=None):
        target = equipment or self.equipment
        resp = self.client_http.get(f'/equipment/{target.pk}/history/export/')
        self.assertEqual(resp.status_code, 200)
        ws = openpyxl.load_workbook(io.BytesIO(resp.content)).active
        headers = [c.value for c in ws[1]]
        return [dict(zip(headers, [c.value for c in row])) for row in ws.iter_rows(min_row=2)]

    def test_export_contains_the_visit(self):
        row = self._rows()[0]

        self.assertEqual(row['№ заказа'], self.order.order_number)
        self.assertEqual(row['Заказчик'], 'ООО Альфа')
        self.assertEqual(row['Неисправность'], 'Не крутится')
        self.assertEqual(row['Номера пломб'], 'П-1, П-2')
        self.assertEqual(float(row['Стоимость, ₽']), 3000.0)
        self.assertIn('Диод x2', row['Детали'])

    def test_parts_are_marked_as_order_wide_for_multi_unit_orders(self):
        RepairOrderEquipment.objects.create(repair_order=self.order, equipment=self.other)

        row = self._rows()[0]
        self.assertIn('на весь заказ', row['Детали'])

    def test_export_covers_only_the_requested_equipment(self):
        self.assertEqual(self._rows(self.other), [])

    def test_warranty_column_filled_for_completed_order(self):
        row = self._rows()[0]
        self.assertIsNotNone(row['Гарантия до'])

    def test_history_page_offers_export(self):
        resp = self.client_http.get(f'/equipment/{self.equipment.pk}/history/')
        self.assertContains(resp, f'/equipment/{self.equipment.pk}/history/export/')

    def test_export_requires_login(self):
        resp = TestClient().get(f'/equipment/{self.equipment.pk}/history/export/')
        self.assertEqual(resp.status_code, 302)


class BatchLabelTests(TestCase):
    """Печать этикеток пачкой: отбор, ограничение, раскладка."""

    def setUp(self):
        self.admin = Employee.objects.create_superuser(
            username='admin_batch', full_name='Админ', password='pass'
        )
        self.client_http = TestClient()
        self.client_http.force_login(self.admin)

        self.resistor = SparePart.objects.create(
            part_number='B-RES', name='Резистор', component_type='Резистор', current_stock=5
        )
        self.diode = SparePart.objects.create(
            part_number='B-DIO', name='Диод', component_type='Диод', current_stock=5
        )

        self.filled = StorageCell.objects.create(cabinet=Cabinet.objects.get_or_create(number=1)[0], row_number=1, cell_row=1)
        self.filled.parts.add(self.resistor)
        self.empty = StorageCell.objects.create(cabinet=Cabinet.objects.get_or_create(number=1)[0], row_number=1, cell_row=2)
        StorageCell.objects.create(cabinet=Cabinet.objects.get_or_create(number=2)[0], row_number=1, cell_row=1)

    def _labels(self, url):
        resp = self.client_http.get(url)
        self.assertEqual(resp.status_code, 200)
        return resp, resp.context['labels']

    # --- детали ---

    def test_selected_parts_only(self):
        _, labels = self._labels(f'/parts/labels/?ids={self.diode.pk}')

        self.assertEqual([item['part'].part_number for item in labels], ['B-DIO'])

    def test_several_selected_parts(self):
        _, labels = self._labels(f'/parts/labels/?ids={self.diode.pk}&ids={self.resistor.pk}')
        self.assertEqual(len(labels), 2)

    def test_without_selection_falls_back_to_the_current_filter(self):
        """Кнопка «на все отобранные» передаёт условия списка, а не номера."""
        _, labels = self._labels('/parts/labels/?q=Диод')

        self.assertEqual([item['part'].part_number for item in labels], ['B-DIO'])

    def test_without_selection_and_filter_takes_everything(self):
        _, labels = self._labels('/parts/labels/')
        self.assertEqual(len(labels), 2)

    def test_garbage_in_ids_is_ignored(self):
        _, labels = self._labels('/parts/labels/?ids=abc')
        # мусор отброшен, отбора нет — печатается всё
        self.assertEqual(len(labels), 2)

    def test_every_label_has_its_own_qr(self):
        _, labels = self._labels('/parts/labels/')
        codes = {item['qr_img'] for item in labels}

        self.assertEqual(len(codes), 2)
        self.assertTrue(all(code for code in codes))

    def test_batch_is_capped(self):
        from core.views import MAX_LABELS_PER_BATCH

        SparePart.objects.bulk_create([
            SparePart(part_number=f'CAP-{i:04d}', name='Деталь')
            for i in range(MAX_LABELS_PER_BATCH + 5)
        ])
        _, labels = self._labels('/parts/labels/')

        self.assertEqual(len(labels), MAX_LABELS_PER_BATCH)

    # --- ячейки ---

    def test_whole_cabinet(self):
        _, labels = self._labels('/storage-cells/labels/?cabinet=1')
        self.assertEqual({item['cell'].address for item in labels}, {'К1-Р1-Я1', 'К1-Р1-Я2'})

    def test_only_filled_cells(self):
        _, labels = self._labels('/storage-cells/labels/?cabinet=1&only_filled=1')
        self.assertEqual([item['cell'].address for item in labels], ['К1-Р1-Я1'])

    def test_cabinet_filter_excludes_other_cabinets(self):
        _, labels = self._labels('/storage-cells/labels/?cabinet=2')
        self.assertEqual([item['cell'].address for item in labels], ['К2-Р1-Я1'])

    def test_selected_cells_win_over_cabinet(self):
        _, labels = self._labels(f'/storage-cells/labels/?ids={self.empty.pk}&cabinet=2')
        self.assertEqual([item['cell'].address for item in labels], ['К1-Р1-Я2'])

    def test_without_cabinet_nothing_is_printed(self):
        """Иначе одна опечатка в адресе вывела бы этикетки на все 768 ячеек."""
        _, labels = self._labels('/storage-cells/labels/')
        self.assertEqual(labels, [])

    def test_cell_label_keeps_grouping_logic(self):
        """Пачка и одиночная печать используют одну сборку данных."""
        second = SparePart.objects.create(
            part_number='B-RES2', name='Резистор 2', component_type='Резистор'
        )
        self.filled.parts.add(second)

        _, labels = self._labels('/storage-cells/labels/?cabinet=1&only_filled=1')
        self.assertEqual(labels[0]['title'], 'Набор резисторов')

    # --- раскладка и доступ ---

    def test_roll_layout_by_default(self):
        resp, _ = self._labels('/parts/labels/')

        self.assertEqual(resp.context['layout'], 'roll')
        self.assertContains(resp, 'size: 43mm 25mm')
        self.assertContains(resp, 'page-break-after: always')

    def test_a4_layout(self):
        resp, _ = self._labels('/parts/labels/?layout=a4')

        self.assertEqual(resp.context['layout'], 'a4')
        self.assertContains(resp, 'size: A4')
        self.assertNotContains(resp, 'page-break-after: always')

    def test_unknown_layout_falls_back_to_roll(self):
        resp, _ = self._labels('/parts/labels/?layout=что-то')
        self.assertEqual(resp.context['layout'], 'roll')

    def test_batch_pages_require_login(self):
        anon = TestClient()
        self.assertEqual(anon.get('/parts/labels/').status_code, 302)
        self.assertEqual(anon.get('/storage-cells/labels/').status_code, 302)

    def test_entry_points_are_on_the_pages(self):
        parts_page = self.client_http.get('/parts/')
        grid_page = self.client_http.get('/storage-cells/?cabinet=1')

        self.assertContains(parts_page, '/parts/labels/')
        self.assertContains(grid_page, '/storage-cells/labels/')


class StockSocketTests(TestCase):
    """Канал живых обновлений остатка: доступ и рассылка.

    Серверная часть существовала с самого начала, но к ней никто
    не подключался — сообщения уходили в пустоту, а сам сокет пускал любого.
    """

    def setUp(self):
        self.user = Employee.objects.create_user(
            username='ws_user', full_name='Кладовщик', password='pass', role='warehouse'
        )
        self.part = SparePart.objects.create(
            part_number='WS-1', name='Деталь', current_stock=10, min_stock=3
        )

    def _communicator(self, user=None):
        from channels.testing import WebsocketCommunicator
        from lifteam.asgi import websocket_urlpatterns
        from channels.routing import URLRouter

        communicator = WebsocketCommunicator(URLRouter(websocket_urlpatterns), '/ws/stock/')
        # AuthMiddlewareStack в тестах подменяем прямой подстановкой:
        # проверяем поведение потребителя, а не саму сессионную прослойку
        communicator.scope['user'] = user
        return communicator

    async def _connect(self, user):
        communicator = self._communicator(user)
        connected, _ = await communicator.connect()
        return communicator, connected

    def test_anonymous_connection_is_refused(self):
        from asgiref.sync import async_to_sync
        from django.contrib.auth.models import AnonymousUser

        async def run():
            communicator, connected = await self._connect(AnonymousUser())
            await communicator.disconnect()
            return connected

        self.assertFalse(async_to_sync(run)())

    def test_connection_without_user_is_refused(self):
        from asgiref.sync import async_to_sync

        async def run():
            communicator, connected = await self._connect(None)
            await communicator.disconnect()
            return connected

        self.assertFalse(async_to_sync(run)())

    def test_authenticated_user_gets_updates(self):
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer

        async def run():
            communicator, connected = await self._connect(self.user)
            greeting = await communicator.receive_json_from()

            layer = get_channel_layer()
            await layer.group_send('stock_updates', {
                'type': 'stock_update',
                'data': {'part_id': self.part.pk, 'part_number': 'WS-1',
                         'current_stock': 2, 'min_stock': 3},
            })
            update = await communicator.receive_json_from()
            await communicator.disconnect()
            return connected, greeting, update

        connected, greeting, update = async_to_sync(run)()

        self.assertTrue(connected)
        self.assertEqual(greeting['type'], 'connection_established')
        self.assertEqual(update['type'], 'stock_update')
        self.assertEqual(update['data']['current_stock'], 2)


class StockWatchMarkupTests(TestCase):
    """Разметка, по которой скрипт находит цифры остатка на странице."""

    def setUp(self):
        self.admin = Employee.objects.create_superuser(
            username='admin_ws', full_name='Админ', password='pass'
        )
        self.client_http = TestClient()
        self.client_http.force_login(self.admin)
        self.part = SparePart.objects.create(
            part_number='WS-2', name='Деталь', current_stock=7, min_stock=3
        )

    def test_part_list_marks_stock(self):
        resp = self.client_http.get('/parts/')
        self.assertContains(resp, f'data-stock-part="{self.part.pk}"')

    def test_part_detail_marks_stock(self):
        resp = self.client_http.get(f'/parts/{self.part.pk}/')
        self.assertContains(resp, f'data-stock-part="{self.part.pk}"')

    def test_script_and_toast_holder_are_on_the_page(self):
        content = self.client_http.get('/parts/').content.decode()
        self.assertIn('js/stock-updates.js', content)
        self.assertIn('id="stockToasts"', content)


class DashboardStatusStatsTests(TestCase):
    """Разбивка заказов по статусам на дашборде.

    Данные считались с самого начала, но в шаблон не попадали — на экране
    их не было вовсе.
    """

    def setUp(self):
        self.admin = Employee.objects.create_superuser(
            username='admin_stats', full_name='Админ', password='pass'
        )
        self.client_http = TestClient()
        self.client_http.force_login(self.admin)

        client_obj = ClientModel.objects.create(name='Заказчик')
        for status in ('repair', 'repair', 'shipped'):
            RepairOrder.objects.create(client=client_obj, status=status)

    def test_counts_by_status(self):
        resp = self.client_http.get('/')
        counts = {item['code']: item['count'] for item in resp.context['status_stats']}

        self.assertEqual(counts['repair'], 2)
        self.assertEqual(counts['shipped'], 1)

    def test_statuses_without_orders_are_shown_as_zero(self):
        """Иначе набор плиток менялся бы от загрузки к загрузке."""
        resp = self.client_http.get('/')
        codes = [item['code'] for item in resp.context['status_stats']]

        self.assertEqual(codes, [code for code, _ in RepairOrder.STATUS_CHOICES])
        counts = {item['code']: item['count'] for item in resp.context['status_stats']}
        self.assertEqual(counts['diagnostic'], 0)

    def test_tiles_link_to_the_filtered_order_list(self):
        content = self.client_http.get('/').content.decode()
        self.assertIn('/repair-orders/?status=repair', content)

    def test_link_actually_filters(self):
        resp = self.client_http.get('/repair-orders/?status=repair')
        self.assertEqual(len(resp.context['orders']), 2)


class GridLiveUpdateMarkupTests(TestCase):
    """Сетка кассетниц отдаёт данные, по которым скрипт перекрашивает ячейки."""

    def setUp(self):
        self.admin = Employee.objects.create_superuser(
            username='admin_grid', full_name='Админ', password='pass'
        )
        self.client_http = TestClient()
        self.client_http.force_login(self.admin)

        call_command('init_cells', verbosity=0)
        self.part = SparePart.objects.create(
            part_number='GRID-1', name='Деталь', current_stock=10, min_stock=2
        )
        cell = StorageCell.objects.filter(cabinet__number=1).first()
        cell.parts.add(self.part)

    def test_grid_exposes_cells_data_to_the_script(self):
        content = self.client_http.get('/storage-cells/?cabinet=1').content.decode()

        self.assertIn('window.CELLS_DATA = CELLS_DATA', content)
        self.assertIn('js/stock-updates.js', content)

    def test_cells_carry_their_id(self):
        content = self.client_http.get('/storage-cells/?cabinet=1').content.decode()
        self.assertIn('data-cell-id=', content)


@override_settings(NOTIFY_CLIENTS=True)
class OrderNotificationTests(TestCase):
    """Оповещения заказчику о смене статуса заказа."""

    def setUp(self):
        self.user = Employee.objects.create_superuser(
            username='admin_notify', full_name='Админ', password='pass'
        )
        self.client_http = TestClient()
        self.client_http.force_login(self.user)

        self.client_obj = ClientModel.objects.create(
            name='ООО Альфа', email='client@example.com'
        )
        self.order = RepairOrder.objects.create(client=self.client_obj)
        model = EquipmentModel.objects.create(name='БУАД')
        RepairOrderEquipment.objects.create(
            repair_order=self.order,
            equipment=Equipment.objects.create(model=model, serial_number='SN-1'),
        )

    def _change_status(self, status):
        return self.client_http.post(
            f'/repair-orders/{self.order.pk}/change-status/',
            {'new_status': status, 'notes': ''},
        )

    def test_ready_for_shipment_queues_a_letter(self):
        self._change_status('ready_for_shipment')

        note = Notification.objects.get(event='order_status')
        self.assertEqual(note.recipient, 'client@example.com')
        self.assertEqual(note.status, 'pending')
        self.assertIn(self.order.order_number, note.subject)
        self.assertIn('SN-1', note.body)
        self.assertEqual(note.repair_order, self.order)

    def test_internal_statuses_do_not_bother_the_client(self):
        """«Диагностика» и «ремонт» заказчику ничего не говорят."""
        self._change_status('diagnostic')
        self._change_status('repair')

        self.assertEqual(Notification.objects.count(), 0)

    def test_shipped_letter_carries_the_tracking_number(self):
        self.order.tracking_number = 'TRK-77'
        self.order.save(update_fields=['tracking_number'])
        self._change_status('shipped')

        self.assertIn('TRK-77', Notification.objects.get().body)

    def test_client_without_email_gets_nothing(self):
        self.client_obj.email = ''
        self.client_obj.save(update_fields=['email'])
        self._change_status('ready_for_shipment')

        self.assertEqual(Notification.objects.count(), 0)

    @override_settings(NOTIFY_CLIENTS=False)
    def test_disabled_client_letters_are_not_queued(self):
        """Переписка с внешними людьми не должна включиться сама собой."""
        self._change_status('ready_for_shipment')
        self.assertEqual(Notification.objects.count(), 0)

    def test_status_change_still_works_and_is_logged(self):
        """Оповещение не должно мешать основному делу."""
        self._change_status('ready_for_shipment')
        self.order.refresh_from_db()

        self.assertEqual(self.order.status, 'ready_for_shipment')
        self.assertTrue(self.order.status_history.exists())


@override_settings(NOTIFY_CLIENTS=True)
class UnrepairableStatusTests(TestCase):
    """Статус «Ремонт невозможен»: переход, дашборд, должники, письмо."""

    def setUp(self):
        self.user = Employee.objects.create_superuser(
            username='admin_unrepairable', full_name='Админ', password='pass'
        )
        self.client_http = TestClient()
        self.client_http.force_login(self.user)

        self.client_obj = ClientModel.objects.create(
            name='ООО Гамма', email='gamma@example.com'
        )
        self.order = RepairOrder.objects.create(client=self.client_obj)
        model = EquipmentModel.objects.create(name='Привод дверей')
        RepairOrderEquipment.objects.create(
            repair_order=self.order,
            equipment=Equipment.objects.create(model=model, serial_number='SN-UNREP'),
        )

    def _change_status(self, status):
        return self.client_http.post(
            f'/repair-orders/{self.order.pk}/change-status/',
            {'new_status': status, 'notes': ''},
        )

    def test_order_can_be_marked_unrepairable(self):
        self._change_status('unrepairable')
        self.order.refresh_from_db()

        self.assertEqual(self.order.status, 'unrepairable')
        self.assertTrue(
            self.order.status_history.filter(status='unrepairable').exists()
        )

    def test_unrepairable_orders_are_not_active(self):
        response = self.client_http.get('/')
        before = response.context['active_orders']

        self._change_status('unrepairable')

        response = self.client_http.get('/')
        self.assertEqual(response.context['active_orders'], before - 1)

    def test_unrepairable_orders_are_not_debtors(self):
        """Ремонт не сделан, счёт не выставлен — это не долг заказчика."""
        self._change_status('unrepairable')
        self.order.refresh_from_db()

        self.assertEqual(self.order.payment_status, 'unpaid')
        self.assertNotIn(self.order, RepairOrder.objects.with_debt())

        response = self.client_http.get('/')
        self.assertNotIn(self.order, response.context['debtors'])

    def test_client_is_notified(self):
        self._change_status('unrepairable')

        note = Notification.objects.get(event='order_status')
        self.assertEqual(note.recipient, 'gamma@example.com')
        self.assertIn(self.order.order_number, note.subject)
        self.assertIn(self.order.order_number, note.body)


class LowStockNotificationTests(TestCase):
    """Оповещения сотрудникам о дефиците."""

    def setUp(self):
        self.warehouse = Employee.objects.create_user(
            username='wh', full_name='Кладовщик', password='pass',
            role='warehouse', email='wh@example.com',
        )
        Employee.objects.create_user(
            username='acc', full_name='Бухгалтер', password='pass',
            role='accountant', email='acc@example.com',
        )
        Employee.objects.create_user(
            username='wh_noemail', full_name='Без почты', password='pass',
            role='warehouse',
        )
        self.part = SparePart.objects.create(
            part_number='LOW-1', name='Резистор', current_stock=10, min_stock=5
        )

    def _spend(self, quantity):
        self.part.current_stock -= quantity
        self.part.save(update_fields=['current_stock'])

    def test_shortage_notifies_warehouse_only(self):
        self._spend(6)

        recipients = set(Notification.objects.values_list('recipient', flat=True))
        self.assertEqual(recipients, {'wh@example.com'})

    def test_letter_contains_what_is_needed_to_order(self):
        self.part.preferred_supplier = 'Чип и Дип'
        self.part.lead_time_days = 14
        self.part.save()
        self._spend(6)

        body = Notification.objects.first().body
        self.assertIn('LOW-1', body)
        self.assertIn('Чип и Дип', body)
        self.assertIn('14', body)

    def test_stock_above_minimum_notifies_nobody(self):
        self._spend(1)
        self.assertEqual(Notification.objects.count(), 0)

    def test_repeated_write_offs_do_not_spam(self):
        """При разборе заказа списывают по несколько деталей подряд."""
        self._spend(6)
        self._spend(1)
        self._spend(1)

        self.assertEqual(Notification.objects.count(), 1)

    @override_settings(NOTIFY_LOW_STOCK_COOLDOWN_HOURS=0)
    def test_cooldown_can_be_switched_off(self):
        self._spend(6)
        self._spend(1)

        self.assertEqual(Notification.objects.count(), 2)

    @override_settings(NOTIFY_LOW_STOCK=False)
    def test_can_be_disabled(self):
        self._spend(6)
        self.assertEqual(Notification.objects.count(), 0)


class SendNotificationsCommandTests(TestCase):
    """Команда отправки: выключатель, повторы, просроченные."""

    def setUp(self):
        self.note = Notification.objects.create(
            event='low_stock', recipient='wh@example.com',
            subject='Дефицит', body='Текст',
        )

    def _run(self, **options):
        from io import StringIO
        out = StringIO()
        call_command('send_notifications', stdout=out, stderr=StringIO(), **options)
        return out.getvalue()

    @override_settings(NOTIFICATIONS_ENABLED=False)
    def test_nothing_is_sent_while_disabled(self):
        from django.core import mail

        output = self._run()
        self.note.refresh_from_db()

        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(self.note.status, 'pending')
        self.assertIn('выключена', output)

    @override_settings(NOTIFICATIONS_ENABLED=True,
                       EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
    def test_sends_and_marks_as_sent(self):
        from django.core import mail

        self._run()
        self.note.refresh_from_db()

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['wh@example.com'])
        self.assertEqual(self.note.status, 'sent')
        self.assertIsNotNone(self.note.sent_at)

    @override_settings(NOTIFICATIONS_ENABLED=True,
                       EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
    def test_dry_run_sends_nothing(self):
        from django.core import mail

        output = self._run(dry_run=True)
        self.note.refresh_from_db()

        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(self.note.status, 'pending')
        self.assertIn('проверка', output)

    @override_settings(NOTIFICATIONS_ENABLED=True, NOTIFICATIONS_MAX_ATTEMPTS=2)
    def test_failure_is_retried_then_given_up(self):
        with patch('core.management.commands.send_notifications.EmailMessage.send',
                   side_effect=OSError('SMTP молчит')):
            self._run()
            self.note.refresh_from_db()
            self.assertEqual(self.note.status, 'pending')
            self.assertEqual(self.note.attempts, 1)
            self.assertIn('SMTP молчит', self.note.last_error)

            self._run()
            self.note.refresh_from_db()

        self.assertEqual(self.note.status, 'failed')
        self.assertEqual(self.note.attempts, 2)

    @override_settings(NOTIFICATIONS_ENABLED=True, NOTIFICATIONS_MAX_AGE_HOURS=24,
                       EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
    def test_stale_notifications_are_skipped(self):
        """Иначе после включения отправки уедет месячная пачка новостей."""
        from django.core import mail

        Notification.objects.filter(pk=self.note.pk).update(
            created_at=timezone.now() - datetime.timedelta(days=3)
        )
        self._run()
        self.note.refresh_from_db()

        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(self.note.status, 'skipped')
        self.assertIn('Старше', self.note.last_error)


class PartPriceTests(TestCase):
    """Закупочные цены: план закупок, стоимость запаса, себестоимость ремонта."""

    def setUp(self):
        self.admin = Employee.objects.create_superuser(
            username='admin_price', full_name='Админ', password='pass')
        self.client_http = TestClient()
        self.client_http.force_login(self.admin)

        self.part = SparePart.objects.create(
            part_number='PR-1', name='Конденсатор', current_stock=2, min_stock=10,
            price=Decimal('150.00'))
        self.no_price = SparePart.objects.create(
            part_number='PR-2', name='Без цены', current_stock=0, min_stock=5)

    def test_purchase_cost_covers_only_the_shortage(self):
        """Закупать надо недостающее, а не весь минимум."""
        self.assertEqual(self.part.stock_deficit, 8)
        self.assertEqual(self.part.purchase_cost, Decimal('1200.00'))

    def test_stock_value_uses_what_is_on_the_shelf(self):
        self.assertEqual(self.part.stock_value, Decimal('300.00'))

    def test_a_part_without_a_price_has_no_sums(self):
        """Пустая цена — не ноль: «неизвестно» и «бесплатно» разные вещи."""
        self.assertIsNone(self.no_price.price)
        self.assertIsNone(self.no_price.purchase_cost)
        self.assertIsNone(self.no_price.stock_value)

    def test_the_plan_totals_what_it_can_and_names_the_rest(self):
        resp = self.client_http.get('/reports/purchase-plan/')

        self.assertEqual(resp.context['plan_total'], Decimal('1200.00'))
        self.assertEqual(resp.context['without_price'], 1)
        self.assertContains(resp, 'без учёта 1 поз. без цены')

    def test_the_plan_export_carries_price_and_total(self):
        resp = self.client_http.get('/reports/purchase-plan/export/')
        ws = openpyxl.load_workbook(io.BytesIO(resp.content)).active
        rows = [[c.value for c in row] for row in ws.iter_rows()]
        headers = rows[0]

        self.assertIn('Цена, ₽', headers)
        self.assertIn('Сумма, ₽', headers)
        self.assertEqual(rows[-1][0], 'Итого')
        self.assertEqual(float(rows[-1][headers.index('Сумма, ₽')]), 1200.0)

    def test_incoming_with_a_price_updates_the_part(self):
        """Вводить цену дважды — в приходе и в карточке — никто не будет."""
        self.client_http.post(f'/parts/{self.part.pk}/stock-incoming/', {
            'quantity': 5, 'unit_price': '175.50', 'document_number': 'ТН-7', 'notes': '',
        })
        self.part.refresh_from_db()

        self.assertEqual(self.part.price, Decimal('175.50'))
        self.assertEqual(self.part.current_stock, 7)

    def test_incoming_without_a_price_keeps_the_old_one(self):
        self.client_http.post(f'/parts/{self.part.pk}/stock-incoming/', {
            'quantity': 5, 'unit_price': '', 'document_number': '', 'notes': '',
        })
        self.part.refresh_from_db()

        self.assertEqual(self.part.price, Decimal('150.00'))

    def test_the_delivery_price_stays_in_the_history(self):
        """На вопрос «почему деталь подорожала вдвое» отвечает история приходов."""
        self.client_http.post(f'/parts/{self.part.pk}/stock-incoming/', {
            'quantity': 4, 'unit_price': '200.00', 'document_number': '', 'notes': '',
        })
        movement = StockMovement.objects.get(part=self.part, movement_type='incoming')

        self.assertEqual(movement.unit_price, Decimal('200.00'))
        self.assertEqual(movement.total_price, Decimal('800.00'))

    def test_parts_used_in_an_order_show_their_cost(self):
        order = RepairOrder.objects.create(
            client=ClientModel.objects.create(name='ООО Цена'))
        detail = RepairOrderDetail.objects.create(
            repair_order=order, part=self.part, quantity_used=3)
        RepairOrderDetail.objects.create(
            repair_order=order, part=self.no_price, quantity_used=1)

        self.assertEqual(detail.cost, Decimal('450.00'))

        resp = self.client_http.get(f'/repair-orders/{order.pk}/')
        self.assertEqual(resp.context['details_cost'], Decimal('450.00'))

    def test_the_cost_price_never_reaches_the_client_act(self):
        """Себестоимость — внутренняя цифра; заказчику идёт стоимость работ."""
        order = RepairOrder.objects.create(
            client=ClientModel.objects.create(name='ООО Акт-цена'))
        RepairOrderDetail.objects.create(
            repair_order=order, part=self.part, quantity_used=3)

        resp = self.client_http.get(f'/repair-orders/{order.pk}/act/complete/')

        self.assertContains(resp, 'Конденсатор')      # деталь названа
        self.assertNotContains(resp, '450')            # а цена — нет
        self.assertNotContains(resp, 'Себестоимость')

    def test_price_survives_import_and_export(self):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(['part_number', 'name', 'component_type', 'price'])
        ws.append(['PR-IMP', 'Импортированная', 'Резистор', '99.90'])
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        buf.name = 'import.xlsx'

        self.client_http.post('/parts/import/', {'file': buf, 'update_existing': False})
        self.assertEqual(SparePart.objects.get(part_number='PR-IMP').price,
                         Decimal('99.90'))

        resp = self.client_http.get('/parts/export/?q=PR-IMP')
        exported = openpyxl.load_workbook(io.BytesIO(resp.content)).active
        row = dict(zip([c.value for c in exported[1]], [c.value for c in exported[2]]))
        self.assertEqual(float(row['price']), 99.90)


class ActTests(TestCase):
    """Печатные акты: приёма оборудования и выполненных работ."""

    def setUp(self):
        self.admin = Employee.objects.create_superuser(
            username='admin_act', full_name='Админ', password='pass')
        self.manager = Employee.objects.create_user(
            username='mgr_act', full_name='Менеджер', password='pass',
            role='repair_manager')
        self.client_http = TestClient()
        self.client_http.force_login(self.admin)

        self.organization = Organization.get_solo()
        self.organization.name = 'ООО «Лифт Тим»'
        self.organization.inn = '7701234567'
        self.organization.signatory_position = 'Директор'
        self.organization.signatory_name = 'Петров П. П.'
        self.organization.save()

        self.order = RepairOrder.objects.create(
            client=ClientModel.objects.create(name='МУП «Городские лифты»', inn='5001'),
        )
        model = EquipmentModel.objects.create(name='БУАД-акт')
        self.roe = RepairOrderEquipment.objects.create(
            repair_order=self.order,
            equipment=Equipment.objects.create(model=model, serial_number='SN-ACT'),
            fault_description='Не запускается привод',
            seal_numbers='П-0012',
            initial_condition='Разъём окислен',
            repair_cost=7200,
        )

    def _receive(self):
        return self.client_http.get(f'/repair-orders/{self.order.pk}/act/receive/')

    def _complete(self):
        return self.client_http.get(f'/repair-orders/{self.order.pk}/act/complete/')

    def test_receive_act_carries_seals_and_condition(self):
        """Ради этих двух граф акт и подписывают: о них потом спорят."""
        resp = self._receive()

        self.assertContains(resp, 'П-0012')
        self.assertContains(resp, 'Разъём окислен')
        self.assertContains(resp, 'SN-ACT')

    def test_both_acts_carry_the_company_details(self):
        for resp in (self._receive(), self._complete()):
            self.assertContains(resp, 'ООО «Лифт Тим»')
            self.assertContains(resp, '7701234567')
            self.assertContains(resp, 'Петров П. П.')

    def test_acts_name_both_parties(self):
        resp = self._receive()

        self.assertContains(resp, 'МУП «Городские лифты»')
        self.assertContains(resp, 'Исполнитель')
        self.assertContains(resp, 'Заказчик')

    def test_completion_act_prints_the_work_not_the_fault(self):
        """Подписывать «выполнено» под описанием поломки нельзя."""
        self.roe.work_performed = 'Заменён силовой ключ, настроен привод'
        self.roe.save(update_fields=['work_performed'])

        resp = self._complete()

        self.assertContains(resp, 'Заменён силовой ключ')

    def test_without_recorded_work_the_fault_is_marked_as_fixed(self):
        resp = self._complete()

        self.assertContains(resp, 'Устранена неисправность')

    def test_completion_act_shows_the_total(self):
        resp = self._complete()

        self.assertContains(resp, '7200')

    def test_completion_act_shows_the_warranty_date(self):
        self.order.date_completed = timezone.now()
        self.order.save(update_fields=['date_completed'])

        resp = self._complete()

        self.assertContains(resp, 'Гарантия на выполненные работы')

    def test_an_unfinished_order_warns_instead_of_inventing_a_date(self):
        self.assertIsNone(self.order.date_completed)

        resp = self._complete()

        self.assertNotContains(resp, 'Гарантия на выполненные работы')
        self.assertContains(resp, 'Заказ ещё не завершён')

    def test_completion_act_lists_replaced_parts(self):
        part = SparePart.objects.create(part_number='ACT-1', name='Конденсатор',
                                        current_stock=10)
        RepairOrderDetail.objects.create(repair_order=self.order, part=part,
                                         quantity_used=2)
        resp = self._complete()

        self.assertContains(resp, 'Конденсатор')
        self.assertContains(resp, 'ACT-1')

    def test_acts_print_even_without_company_details(self):
        """Пустая шапка — не повод не напечатать: допишут от руки."""
        Organization.objects.all().delete()

        resp = self._receive()

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Реквизиты не заполнены')

    def test_acts_require_login(self):
        resp = TestClient().get(f'/repair-orders/{self.order.pk}/act/receive/')

        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login/', resp['Location'])


class DefectActTests(TestCase):
    """Акт дефектации: что нашли при диагностике и во сколько это обойдётся."""

    def setUp(self):
        self.admin = Employee.objects.create_superuser(
            username='admin_defect', full_name='Админ', password='pass')
        self.client_http = TestClient()
        self.client_http.force_login(self.admin)

        organization = Organization.get_solo()
        organization.name = 'ООО «Лифт Тим»'
        organization.signatory_position = 'Директор'
        organization.signatory_name = 'Петров П. П.'
        organization.save()

        self.order = RepairOrder.objects.create(
            client=ClientModel.objects.create(name='ООО «Ремонт»'),
        )
        self.model = EquipmentModel.objects.create(
            name='Emotron DSV35-40-028 Lift', kind='Преобразователь частоты')
        self.roe = RepairOrderEquipment.objects.create(
            repair_order=self.order,
            equipment=Equipment.objects.create(model=self.model, serial_number='SN-DEF'),
            fault_description='Не включается',
        )

    def _act_url(self):
        return f'/repair-orders/{self.order.pk}/equipment/{self.roe.pk}/act/defect/'

    def _edit_url(self):
        return f'/repair-orders/{self.order.pk}/equipment/{self.roe.pk}/defect/'

    def _fill(self, **overrides):
        data = {
            'defect_act_date': '2026-05-06',
            'diagnosis': 'Выявлен выход из строя IGBT модуля и его обвязки.',
            'error_codes': '«Десат» — короткое замыкание между фазами\n'
                           '«EOL» — переезд зоны полного открытия',
            'warranty_case': 'non_warranty',
            'non_warranty_reason': 'перепадами напряжения в питающей сети',
            'estimated_cost': '14000',
        }
        data.update(overrides)
        return self.client_http.post(self._edit_url(), data)

    def test_the_generic_type_stands_before_the_model(self):
        """«Тип: Преобразователь частоты Emotron …» — так набран их бланк."""
        self.assertEqual(self.model.full_name,
                         'Преобразователь частоты Emotron DSV35-40-028 Lift')

    def test_a_model_without_a_generic_type_prints_as_is(self):
        model = EquipmentModel.objects.create(name='БУАД-3')

        self.assertEqual(model.full_name, 'БУАД-3')

    def test_filling_the_act_opens_it(self):
        resp = self._fill()

        self.assertRedirects(resp, self._act_url())
        self.roe.refresh_from_db()
        self.assertEqual(self.roe.estimated_cost, Decimal('14000'))
        self.assertEqual(self.roe.defect_act_date, datetime.date(2026, 5, 6))

    def test_the_act_carries_diagnosis_codes_and_estimate(self):
        self._fill()

        resp = self.client_http.get(self._act_url())

        self.assertContains(resp, 'IGBT модуля')
        self.assertContains(resp, '«Десат»')
        self.assertContains(resp, '«EOL»')
        self.assertContains(resp, 'Случай не является гарантийным')
        self.assertContains(resp, 'перепадами напряжения')
        # Разряды разделены неразрывным пробелом: иначе печать перенесёт
        # строку посреди суммы
        self.assertContains(resp, '14 000 рублей')
        self.assertContains(resp, 'Преобразователь частоты Emotron DSV35-40-028 Lift')
        self.assertContains(resp, 'SN-DEF')

    def test_the_estimate_is_grouped_by_thousands(self):
        self.roe.estimated_cost = Decimal('54000.00')

        self.assertEqual(self.roe.estimated_cost_text, '54 000')

    def test_no_estimate_means_no_text(self):
        self.assertIsNone(self.roe.estimated_cost_text)

    def test_error_codes_are_split_by_lines(self):
        self.roe.error_codes = ' «F2340» — КЗ в модуле \n\n «EOC» — КЗ на выходе '
        self.roe.save(update_fields=['error_codes'])

        self.assertEqual(self.roe.error_code_lines,
                         ['«F2340» — КЗ в модуле', '«EOC» — КЗ на выходе'])

    def test_a_warranty_case_does_not_demand_money(self):
        """Гарантийному случаю оценка ремонта в акте не место."""
        self._fill(warranty_case='warranty', estimated_cost='')

        resp = self.client_http.get(self._act_url())

        self.assertContains(resp, 'Случай является гарантийным')
        self.assertNotContains(resp, 'Случай не является гарантийным')
        self.assertNotContains(resp, 'Ориентировочная стоимость')

    def test_a_warranty_case_drops_the_prefilled_reason(self):
        """Заготовка причины не должна противоречить самому акту."""
        self._fill(warranty_case='warranty')

        self.roe.refresh_from_db()
        self.assertEqual(self.roe.non_warranty_reason, '')

    def test_an_undecided_case_says_nothing_about_the_warranty(self):
        self._fill(warranty_case='', estimated_cost='')

        resp = self.client_http.get(self._act_url())

        self.assertNotContains(resp, 'гарантийным')

    def test_an_empty_act_warns_instead_of_printing_a_blank(self):
        resp = self.client_http.get(self._act_url())

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Результаты диагностики не заполнены')

    def test_without_a_date_the_act_is_dated_today(self):
        self._fill(defect_act_date='')

        resp = self.client_http.get(self._act_url())

        self.assertContains(resp, timezone.localdate().strftime('%d.%m.%Y'))

    def test_the_prefilled_reason_is_the_usual_wording(self):
        resp = self.client_http.get(self._edit_url())

        self.assertContains(resp, 'естественной деградацией электронных компонентов')

    def test_equipment_from_another_order_is_not_reachable(self):
        """Иначе в акт попал бы серийник из чужого заказа."""
        other = RepairOrder.objects.create(
            client=ClientModel.objects.create(name='ООО «Другой»'))

        resp = self.client_http.get(
            f'/repair-orders/{other.pk}/equipment/{self.roe.pk}/act/defect/')

        self.assertEqual(resp.status_code, 404)

    def test_the_act_requires_login(self):
        resp = TestClient().get(self._act_url())

        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login/', resp['Location'])

    def test_the_order_card_offers_to_fill_the_act(self):
        resp = self.client_http.get(f'/repair-orders/{self.order.pk}/')

        self.assertContains(resp, self._edit_url())

    def test_a_filled_act_is_linked_from_the_order_card(self):
        self._fill()

        resp = self.client_http.get(f'/repair-orders/{self.order.pk}/')

        self.assertContains(resp, self._act_url())

    def test_known_generic_types_are_suggested_in_the_model_form(self):
        resp = self.client_http.get('/equipment/models/create/')

        self.assertContains(resp, 'equipment-kinds')
        self.assertContains(resp, 'Преобразователь частоты')


class OrganizationTests(TestCase):
    """Реквизиты фирмы: одна запись, править может только администратор."""

    def setUp(self):
        self.admin = Employee.objects.create_superuser(
            username='admin_org', full_name='Админ', password='pass')
        self.accountant = Employee.objects.create_user(
            username='buh_org', full_name='Бухгалтер', password='pass',
            role='accountant')
        self.client_http = TestClient()
        self.client_http.force_login(self.admin)

    def test_the_record_is_created_on_first_use(self):
        self.assertEqual(Organization.objects.count(), 0)

        Organization.get_solo()
        Organization.get_solo()

        self.assertEqual(Organization.objects.count(), 1)

    def test_admin_can_save_the_details(self):
        resp = self.client_http.post('/management/organization/', {
            'name': 'ООО «Лифт Тим»', 'inn': '7701234567', 'kpp': '770101001',
            'address': 'Москва', 'phone': '+7 925 282-40-31', 'email': '',
            'signatory_position': 'Директор', 'signatory_name': 'Петров П. П.',
        }, follow=True)

        self.assertEqual(resp.status_code, 200)
        organization = Organization.get_solo()
        self.assertEqual(organization.name, 'ООО «Лифт Тим»')
        self.assertIn('ИНН 7701234567', organization.details_line)

    def test_others_cannot_edit_the_details(self):
        other = TestClient()
        other.force_login(self.accountant)

        resp = other.get('/management/organization/')

        self.assertEqual(resp.status_code, 302)


class PaymentTests(TestCase):
    """Оплаты частями. До них «оплачено» было только статусом, и при
    частичной оплате долгом считалась вся стоимость ремонта."""

    def setUp(self):
        self.accountant = Employee.objects.create_user(
            username='buh_pay', full_name='Бухгалтер', password='pass',
            role='accountant')
        self.manager = Employee.objects.create_user(
            username='mgr_pay', full_name='Менеджер', password='pass',
            role='repair_manager')
        self.client_http = TestClient()
        self.client_http.force_login(self.accountant)

        self.order = RepairOrder.objects.create(
            client=ClientModel.objects.create(name='ООО Плательщик'),
            payment_status='unpaid',
        )
        model = EquipmentModel.objects.create(name='БУАД-оплата')
        RepairOrderEquipment.objects.create(
            repair_order=self.order,
            equipment=Equipment.objects.create(model=model, serial_number='SN-PAY'),
            repair_cost=10000,
        )

    def _pay(self, amount, client=None):
        return (client or self.client_http).post(
            f'/repair-orders/{self.order.pk}/payments/add/',
            {'amount': amount, 'payment_date': datetime.date.today().isoformat(),
             'note': 'п/п 1'},
        )

    def test_partial_payment_leaves_the_remainder_as_debt(self):
        self._pay('4000')
        self.order.refresh_from_db()

        self.assertEqual(self.order.paid_amount, Decimal('4000'))
        self.assertEqual(self.order.debt, Decimal('6000'))
        self.assertEqual(self.order.payment_status, 'partially_paid')

    def test_full_payment_closes_the_order(self):
        """Иначе заказ висел бы в должниках после последнего платежа."""
        self._pay('4000')
        self._pay('6000')
        self.order.refresh_from_db()

        self.assertEqual(self.order.debt, 0)
        self.assertEqual(self.order.payment_status, 'paid')
        self.assertNotIn(self.order, RepairOrder.objects.with_debt())

    def test_overpayment_still_counts_as_paid(self):
        self._pay('12000')
        self.order.refresh_from_db()

        self.assertEqual(self.order.debt, 0)
        self.assertEqual(self.order.payment_status, 'paid')

    def test_deleting_a_payment_brings_the_debt_back(self):
        """Суммы вбивают руками, опечатка в разряде — обычное дело."""
        self._pay('10000')
        payment = Payment.objects.get()

        self.client_http.post(
            f'/repair-orders/{self.order.pk}/payments/{payment.pk}/delete/')
        self.order.refresh_from_db()

        self.assertEqual(self.order.paid_amount, 0)
        self.assertEqual(self.order.debt, Decimal('10000'))
        self.assertEqual(self.order.payment_status, 'unpaid')

    def test_a_manually_paid_order_has_no_debt(self):
        """Статус ставит человек, видевший платёжку, — он главнее арифметики."""
        self.order.payment_status = 'paid'
        self.order.save(update_fields=['payment_status'])

        self.assertEqual(self.order.paid_amount, 0)
        self.assertEqual(self.order.debt, 0)

    def test_payment_is_recorded_in_the_history(self):
        self._pay('4000')

        note = self.order.status_history.first().notes
        self.assertIn('4000', note)
        self.assertIn('п/п 1', note)

    def test_a_negative_amount_is_rejected(self):
        self._pay('-500')

        self.assertEqual(Payment.objects.count(), 0)

    def test_only_accounting_may_enter_payments(self):
        manager_client = TestClient()
        manager_client.force_login(self.manager)

        resp = self._pay('1000', client=manager_client)

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Payment.objects.count(), 0)


class DebtQuerySetTests(TestCase):
    """Что считается просроченным долгом."""

    def setUp(self):
        self.client_obj = ClientModel.objects.create(name='ООО Должник')
        self.today = datetime.date.today()

    def _order(self, **kwargs):
        order = RepairOrder.objects.create(client=self.client_obj, **kwargs)
        model, _ = EquipmentModel.objects.get_or_create(name='БУАД-долг')
        RepairOrderEquipment.objects.create(
            repair_order=order,
            equipment=Equipment.objects.create(
                model=model, serial_number=f'SN-{order.pk}'),
            repair_cost=1000,
        )
        return order

    def test_paid_orders_are_not_debts(self):
        self._order(payment_status='paid',
                    invoice_date=self.today - datetime.timedelta(days=90))

        self.assertEqual(RepairOrder.objects.with_debt().count(), 0)

    def test_partially_paid_is_a_debt(self):
        self._order(payment_status='partially_paid')

        self.assertEqual(RepairOrder.objects.with_debt().count(), 1)

    def test_unrepairable_is_not_a_debt(self):
        """По неремонтопригодному оборудованию счёт не выставляют."""
        self._order(payment_status='unpaid', status='unrepairable')

        self.assertEqual(RepairOrder.objects.with_debt().count(), 0)

    @override_settings(DEBT_OVERDUE_DAYS=14)
    def test_fresh_invoice_is_not_overdue_yet(self):
        self._order(payment_status='unpaid',
                    invoice_date=self.today - datetime.timedelta(days=3))

        self.assertEqual(RepairOrder.objects.overdue().count(), 0)

    @override_settings(DEBT_OVERDUE_DAYS=14)
    def test_old_invoice_is_overdue(self):
        order = self._order(payment_status='unpaid',
                            invoice_date=self.today - datetime.timedelta(days=20))

        self.assertEqual(list(RepairOrder.objects.overdue()), [order])
        self.assertEqual(order.days_overdue, 20)
        self.assertTrue(order.is_overdue)

    @override_settings(DEBT_OVERDUE_DAYS=14)
    def test_a_zero_sum_order_is_not_chased(self):
        """Письмо «оплатите 0 ₽» позорнее, чем отсутствие письма."""
        order = RepairOrder.objects.create(
            client=self.client_obj, payment_status='unpaid',
            invoice_date=self.today - datetime.timedelta(days=30))
        model, _ = EquipmentModel.objects.get_or_create(name='БУАД-ноль')
        RepairOrderEquipment.objects.create(
            repair_order=order,
            equipment=Equipment.objects.create(model=model, serial_number='SN-ZERO'),
            repair_cost=0,
        )

        self.assertEqual(RepairOrder.objects.overdue().count(), 0)
        # В отчёте он при этом остаётся: это недозаполненная карточка
        self.assertIn(order, RepairOrder.objects.with_debt())

    def test_without_an_invoice_nothing_is_overdue(self):
        """Пока счёт не выставлен, требовать оплату не за что."""
        order = self._order(payment_status='unpaid', invoice_date=None)

        self.assertEqual(RepairOrder.objects.overdue().count(), 0)
        self.assertEqual(order.days_overdue, 0)
        self.assertFalse(order.is_overdue)

    @override_settings(DEBT_OVERDUE_DAYS=14)
    def test_a_fully_paid_order_leaves_the_overdue_list(self):
        order = self._order(payment_status='unpaid',
                            invoice_date=self.today - datetime.timedelta(days=30))
        self.assertEqual(RepairOrder.objects.overdue().count(), 1)

        Payment.objects.create(repair_order=order, amount=1000)

        self.assertEqual(RepairOrder.objects.overdue().count(), 0)

    @override_settings(DEBT_OVERDUE_DAYS=14)
    def test_a_partly_paid_order_stays_but_owes_less(self):
        order = self._order(payment_status='unpaid',
                            invoice_date=self.today - datetime.timedelta(days=30))
        Payment.objects.create(repair_order=order, amount=400)
        order.refresh_from_db()

        self.assertEqual(list(RepairOrder.objects.overdue()), [order])
        self.assertEqual(order.debt, Decimal('600'))

    def test_shipped_without_an_invoice_is_found_separately(self):
        """Это не долг заказчика, а свой недосмотр — но видеть его надо."""
        order = self._order(payment_status='unpaid', invoice_date=None,
                            status='shipped')
        self._order(payment_status='unpaid', invoice_date=None, status='repair')

        self.assertEqual(list(RepairOrder.objects.shipped_without_invoice()), [order])


class DebtReminderTests(TestCase):
    """Напоминания заказчику и сводка бухгалтерии."""

    def setUp(self):
        Employee.objects.create_user(
            username='buh', full_name='Бухгалтер', password='pass',
            role='accountant', email='buh@example.com',
        )
        Employee.objects.create_user(
            username='sklad_d', full_name='Кладовщик', password='pass',
            role='warehouse', email='sklad@example.com',
        )
        self.client_obj = ClientModel.objects.create(
            name='ООО Должник', email='debtor@example.com')
        self.order = RepairOrder.objects.create(
            client=self.client_obj, payment_status='unpaid',
            invoice_number='42',
            invoice_date=datetime.date.today() - datetime.timedelta(days=30),
        )
        model = EquipmentModel.objects.create(name='БУАД-напоминание')
        RepairOrderEquipment.objects.create(
            repair_order=self.order,
            equipment=Equipment.objects.create(model=model, serial_number='SN-D1'),
            repair_cost=15000,
        )

    def _run(self, **options):
        out = io.StringIO()
        call_command('debt_reminders', stdout=out, stderr=io.StringIO(), **options)
        return out.getvalue()

    def _queued(self, event):
        return list(Notification.objects.filter(event=event))

    @override_settings(NOTIFY_DEBT_DIGEST=True)
    def test_digest_goes_to_accounting_not_to_the_warehouse(self):
        """Долги — дело бухгалтерии; кладовщику этот список ни к чему."""
        self._run()

        recipients = {n.recipient for n in self._queued('debt_digest')}
        self.assertEqual(recipients, {'buh@example.com'})

    @override_settings(NOTIFY_DEBT_DIGEST=True)
    def test_digest_lists_the_order_and_the_total(self):
        self._run()

        body = self._queued('debt_digest')[0].body
        self.assertIn(self.order.order_number, body)
        self.assertIn('ООО Должник', body)
        self.assertIn(notifications.money(15000), body)
        self.assertIn('просрочено 30 дней', body)

    @override_settings(NOTIFY_DEBT_DIGEST=True)
    def test_digest_counts_the_remainder_not_the_full_price(self):
        """Регрессия: при частичной оплате требовали всю стоимость."""
        Payment.objects.create(repair_order=self.order, amount=5000)

        self._run()
        note = self._queued('debt_digest')[0]

        self.assertIn(notifications.money(10000), note.body)
        self.assertIn(notifications.money(5000), note.body)
        self.assertIn(notifications.money(10000), note.subject)

    @override_settings(NOTIFY_CLIENTS=True, NOTIFY_DEBTS=True)
    def test_the_client_is_asked_for_the_remainder_only(self):
        Payment.objects.create(repair_order=self.order, amount=5000)

        self._run()
        body = self._queued('debt_reminder')[0].body

        self.assertIn(f'Остаток к оплате: {notifications.money(10000)}', body)
        self.assertIn(notifications.money(5000), body)

    @override_settings(NOTIFY_CLIENTS=True, NOTIFY_DEBTS=True,
                       NOTIFY_DEBT_DIGEST=True)
    def test_a_settled_order_is_left_alone(self):
        Payment.objects.create(repair_order=self.order, amount=15000)

        self._run()

        self.assertEqual(Notification.objects.count(), 0)

    @override_settings(NOTIFY_DEBT_DIGEST=True, DEBT_DIGEST_COOLDOWN_DAYS=7)
    def test_digest_is_not_repeated_every_day(self):
        """Команда запускается ежедневно, сводка каждый день не нужна."""
        self._run()
        self._run()

        self.assertEqual(len(self._queued('debt_digest')), 1)

    @override_settings(NOTIFY_DEBT_DIGEST=True)
    def test_shipped_without_an_invoice_gets_into_the_digest(self):
        without = RepairOrder.objects.create(
            client=self.client_obj, payment_status='unpaid', status='shipped')
        self._run()

        body = self._queued('debt_digest')[0].body
        self.assertIn('счёт не выставлен', body)
        self.assertIn(without.order_number, body)

    @override_settings(NOTIFY_CLIENTS=True, NOTIFY_DEBTS=True)
    def test_client_reminder_carries_the_invoice_and_the_sum(self):
        self._run()

        reminders = self._queued('debt_reminder')
        self.assertEqual([n.recipient for n in reminders], ['debtor@example.com'])
        self.assertIn('42', reminders[0].body)
        self.assertIn(notifications.money(15000), reminders[0].body)

    @override_settings(NOTIFY_CLIENTS=True, NOTIFY_DEBTS=False)
    def test_client_reminders_are_off_by_default(self):
        """Требование денег от лица фирмы не должно начаться само собой."""
        self._run()

        self.assertEqual(self._queued('debt_reminder'), [])

    @override_settings(NOTIFY_CLIENTS=False, NOTIFY_DEBTS=True)
    def test_the_general_client_switch_also_holds_them(self):
        self._run()

        self.assertEqual(self._queued('debt_reminder'), [])

    @override_settings(NOTIFY_CLIENTS=True, NOTIFY_DEBTS=True,
                       DEBT_REMINDER_COOLDOWN_DAYS=7)
    def test_the_same_client_is_not_pestered_daily(self):
        self._run()
        self._run()

        self.assertEqual(len(self._queued('debt_reminder')), 1)

    @override_settings(NOTIFY_CLIENTS=True, NOTIFY_DEBTS=True)
    def test_a_client_without_an_email_is_skipped_quietly(self):
        self.client_obj.email = ''
        self.client_obj.save(update_fields=['email'])

        self._run()

        self.assertEqual(self._queued('debt_reminder'), [])

    @override_settings(NOTIFY_CLIENTS=True, NOTIFY_DEBTS=True,
                       NOTIFY_DEBT_DIGEST=True)
    def test_dry_run_queues_nothing(self):
        output = self._run(dry_run=True)

        self.assertEqual(Notification.objects.count(), 0)
        self.assertIn('проверка', output)
        self.assertIn(self.order.order_number, output)

    @override_settings(DEBT_OVERDUE_DAYS=14, NOTIFY_CLIENTS=True, NOTIFY_DEBTS=True,
                       NOTIFY_DEBT_DIGEST=True)
    def test_a_fresh_invoice_is_left_alone(self):
        RepairOrder.objects.filter(pk=self.order.pk).update(
            invoice_date=datetime.date.today())

        self._run()

        self.assertEqual(Notification.objects.count(), 0)


class OrderOverdueTests(TestCase):
    """Оповещения о заказах, зависших в одном статусе дольше порога (SLA)."""

    ORDER_OVERDUE_DAYS = {
        'accepted': 2, 'diagnostic': 3, 'repair': 7, 'ready_for_shipment': 5,
    }

    def setUp(self):
        self.manager = Employee.objects.create_user(
            username='rem', full_name='Менеджер по ремонту', password='pass',
            role='repair_manager', email='rem@example.com',
        )
        Employee.objects.create_user(
            username='sklad_o', full_name='Кладовщик', password='pass',
            role='warehouse', email='sklad_o@example.com',
        )
        self.client_obj = ClientModel.objects.create(name='ООО Клиент')

    def _order(self, status, days_ago):
        """Заказ, застрявший в `status` уже `days_ago` дней назад.

        Создание заказа уже заводит запись истории само (сигнал
        `create_status_history`) — сдвигаем в прошлое именно её,
        а не добавляем свою: `order_last_touched` берёт самую свежую
        запись, и вторая, недвинутая, забила бы первую. `changed_at`
        задним числом ставится через queryset.update(), потому что
        auto_now_add применяется только при создании.
        """
        order = RepairOrder.objects.create(client=self.client_obj, status=status)
        when = timezone.now() - datetime.timedelta(days=days_ago)
        OrderStatusHistory.objects.filter(order=order).update(changed_at=when)
        return order

    def _queued(self):
        return list(Notification.objects.filter(event='order_overdue'))

    @override_settings(ORDER_OVERDUE_DAYS=ORDER_OVERDUE_DAYS)
    def test_an_order_younger_than_the_threshold_is_left_alone(self):
        order = self._order('accepted', days_ago=1)

        self.assertIsNone(notifications.notify_order_overdue(order))
        self.assertEqual(self._queued(), [])

    @override_settings(ORDER_OVERDUE_DAYS=ORDER_OVERDUE_DAYS)
    def test_an_overdue_order_is_notified_once_not_on_every_check(self):
        order = self._order('diagnostic', days_ago=4)

        notifications.notify_order_overdue(order)
        notifications.notify_order_overdue(order)

        self.assertEqual(len(self._queued()), 1)

    @override_settings(ORDER_OVERDUE_DAYS=ORDER_OVERDUE_DAYS, ORDER_OVERDUE_ESCALATION_DAYS=7)
    def test_escalation_repeats_after_the_extra_interval_not_before(self):
        # Заказ давно застрял (дольше, чем окно эскалации), чтобы порог по
        # статусу заведомо не мешал проверке самой эскалации
        order = self._order('diagnostic', days_ago=20)
        notifications.notify_order_overdue(order)
        first = self._queued()[0]

        # До эскалации остался день — повторно писать ещё рано
        Notification.objects.filter(pk=first.pk).update(
            created_at=timezone.now() - datetime.timedelta(days=6))
        self.assertIsNone(notifications.notify_order_overdue(order))
        self.assertEqual(len(self._queued()), 1)

        # А неделя с прошлого письма уже прошла — пора написать снова
        Notification.objects.filter(pk=first.pk).update(
            created_at=timezone.now() - datetime.timedelta(days=8))
        notifications.notify_order_overdue(order)
        self.assertEqual(len(self._queued()), 2)

    @override_settings(ORDER_OVERDUE_DAYS=ORDER_OVERDUE_DAYS)
    def test_different_statuses_apply_different_thresholds(self):
        """4 дня в диагностике (порог 3) — уже просрочка; 4 дня в ремонте
        (порог 7) — ещё нет."""
        diagnostic_order = self._order('diagnostic', days_ago=4)
        repair_order = self._order('repair', days_ago=4)

        self.assertTrue(notifications.notify_order_overdue(diagnostic_order))
        self.assertIsNone(notifications.notify_order_overdue(repair_order))

    @override_settings(ORDER_OVERDUE_DAYS=ORDER_OVERDUE_DAYS)
    def test_terminal_statuses_have_no_threshold(self):
        order = self._order('shipped', days_ago=60)

        self.assertIsNone(notifications.notify_order_overdue(order))
        self.assertEqual(self._queued(), [])

    @override_settings(ORDER_OVERDUE_DAYS=ORDER_OVERDUE_DAYS)
    def test_only_repair_manager_and_admin_are_notified_not_the_warehouse(self):
        order = self._order('diagnostic', days_ago=4)

        notifications.notify_order_overdue(order)

        recipients = {n.recipient for n in self._queued()}
        self.assertEqual(recipients, {'rem@example.com'})

    @override_settings(ORDER_OVERDUE_DAYS=ORDER_OVERDUE_DAYS, NOTIFY_ORDER_OVERDUE=False)
    def test_the_whole_check_can_be_switched_off(self):
        order = self._order('diagnostic', days_ago=10)

        self.assertIsNone(notifications.notify_order_overdue(order))

    @override_settings(ORDER_OVERDUE_DAYS=ORDER_OVERDUE_DAYS)
    def test_the_command_queues_the_overdue_order(self):
        self._order('diagnostic', days_ago=4)

        call_command('order_overdue_check', stdout=io.StringIO(), stderr=io.StringIO())

        self.assertEqual(len(self._queued()), 1)

    @override_settings(ORDER_OVERDUE_DAYS=ORDER_OVERDUE_DAYS)
    def test_dry_run_queues_nothing(self):
        order = self._order('diagnostic', days_ago=4)

        out = io.StringIO()
        call_command('order_overdue_check', '--dry-run', stdout=out, stderr=io.StringIO())

        self.assertEqual(Notification.objects.count(), 0)
        self.assertIn(order.order_number, out.getvalue())

    def test_open_orders_exclude_shipped_and_unrepairable(self):
        open_order = self._order('repair', days_ago=1)
        self._order('shipped', days_ago=1)
        self._order('unrepairable', days_ago=1)

        self.assertEqual(list(RepairOrder.objects.open()), [open_order])


class RepairAnalyticsTests(TestCase):
    """Отчёт «Аналитика ремонта»: средние сроки, загрузка, разбивка по
    типу неисправности, права доступа.

    «Инженер» в отчёте — суррогат вместо отсутствующего поля
    «ответственный»: сотрудник, который последним перевёл заказ в статус
    «Ремонт», а если такого перехода не было — в «Диагностика» (см.
    `core.views._order_assignees`).
    """

    def setUp(self):
        self.admin = Employee.objects.create_superuser(
            username='admin_ra', full_name='Админ', password='pass')
        self.manager1 = Employee.objects.create_user(
            username='rm1_ra', full_name='Иванов', password='pass', role='repair_manager')
        self.manager2 = Employee.objects.create_user(
            username='rm2_ra', full_name='Петров', password='pass', role='repair_manager')
        Employee.objects.create_user(
            username='sklad_ra', full_name='Кладовщик', password='pass', role='warehouse')
        self.client_http = TestClient()

        self.model = EquipmentModel.objects.create(name='БУАД-аналитика')
        self.client_obj = ClientModel.objects.create(name='ООО Аналитика')
        self.fault_a = FaultType.objects.create(equipment_model=self.model, name='Не включается')
        self.fault_b = FaultType.objects.create(equipment_model=self.model, name='Перегрев')

        self._serial = 0

    def _equipment(self):
        self._serial += 1
        return Equipment.objects.create(model=self.model, serial_number=f'SN-RA-{self._serial}')

    def _make_order(self, received_days_ago, transitions, faults=()):
        """Заказ с управляемой историей: `transitions` — список
        (статус, сколько дней назад, кто сменил), от старой записи
        к новой. Дата приёма и `changed_at` каждой записи истории
        (включая автосозданную при создании) ставятся задним числом —
        auto_now_add применяет их только при создании, а дальше это
        делается через queryset.update()."""
        order = RepairOrder.objects.create(client=self.client_obj, status='accepted')
        when_received = timezone.now() - datetime.timedelta(days=received_days_ago)
        RepairOrder.objects.filter(pk=order.pk).update(date_received=when_received)
        OrderStatusHistory.objects.filter(order=order).update(changed_at=when_received)

        final_status = 'accepted'
        for status, days_ago, changed_by in transitions:
            entry = OrderStatusHistory.objects.create(
                order=order, status=status, changed_by=changed_by)
            when = timezone.now() - datetime.timedelta(days=days_ago)
            OrderStatusHistory.objects.filter(pk=entry.pk).update(changed_at=when)
            final_status = status

        RepairOrder.objects.filter(pk=order.pk).update(status=final_status)

        roe = RepairOrderEquipment.objects.create(repair_order=order, equipment=self._equipment())
        if faults:
            roe.faults.set(faults)

        order.refresh_from_db()
        return order

    def _build_dataset(self):
        # Заказ 1: диагностика → ремонт → отгружен. Инженер — тот, кто вёл
        # ремонт (manager1), а не тот, кто отгрузил (manager2).
        # Приём 10 дн. назад, отгрузка 1 дн. назад — длительность 9 дней.
        self.order1 = self._make_order(
            received_days_ago=10,
            transitions=[
                ('diagnostic', 9, self.manager1),
                ('repair', 8, self.manager1),
                ('shipped', 1, self.manager2),
            ],
            faults=[self.fault_a],
        )
        # Заказ 2: до ремонта не дошло — диагностика → ремонт невозможен.
        # Инженер — тот, кто вёл диагностику. Приём 6 дн., завершение 2 дн.
        # назад — длительность 4 дня. Неисправность не из справочника.
        self.order2 = self._make_order(
            received_days_ago=6,
            transitions=[
                ('diagnostic', 5, self.manager2),
                ('unrepairable', 2, self.manager2),
            ],
        )
        # Заказ 3: отгружен без диагностики и ремонта вовсе (редкий, но
        # возможный случай) — своего инженера в статистике не имеет.
        # Приём 20 дн., отгрузка 15 дн. назад — длительность 5 дней.
        self.order3 = self._make_order(
            received_days_ago=20,
            transitions=[('shipped', 15, self.manager1)],
        )

    def _get(self, employee, url='/reports/repair-analytics/', **params):
        self.client_http.force_login(employee)
        return self.client_http.get(url, params)

    def test_average_repair_duration_is_computed_correctly(self):
        self._build_dataset()

        resp = self._get(self.admin, date_from='2000-01-01')

        self.assertEqual(resp.context['total_orders'], 3)
        # (9 + 4 + 5) / 3 = 6.0
        self.assertEqual(resp.context['avg_days'], 6.0)

    def test_duration_is_grouped_by_surrogate_engineer(self):
        self._build_dataset()

        resp = self._get(self.admin, date_from='2000-01-01')

        by_employee = {row['employee'].pk: row for row in resp.context['by_employee']}
        self.assertEqual(by_employee[self.manager1.pk]['orders'], 1)
        self.assertEqual(by_employee[self.manager1.pk]['avg_days'], 9.0)
        self.assertEqual(by_employee[self.manager2.pk]['orders'], 1)
        self.assertEqual(by_employee[self.manager2.pk]['avg_days'], 4.0)

    def test_an_order_without_a_repair_or_diagnostic_transition_has_no_engineer(self):
        """Заказ 3 не входит ни в чью персональную разбивку, но входит
        в общее среднее (проверено выше — там total_orders включает все три)."""
        self._build_dataset()

        resp = self._get(self.admin, date_from='2000-01-01')

        assigned_orders = sum(row['orders'] for row in resp.context['by_employee'])
        self.assertEqual(assigned_orders, 2)  # заказ 3 не учтён ни у кого

    def test_fault_type_breakdown_only_counts_orders_with_a_chosen_fault_type(self):
        self._build_dataset()

        resp = self._get(self.admin, date_from='2000-01-01')

        by_fault = {row['fault_type']: row for row in resp.context['by_fault_type']}
        self.assertEqual(list(by_fault.keys()), [str(self.fault_a)])
        self.assertEqual(by_fault[str(self.fault_a)]['orders'], 1)
        self.assertEqual(by_fault[str(self.fault_a)]['avg_days'], 9.0)

    def test_period_filter_excludes_orders_completed_outside_it(self):
        self._build_dataset()

        # Только заказ 1 (отгружен 1 день назад) попадает в узкое окно —
        # заказ 2 (завершён 2 дня назад) уже за его границей
        today = timezone.localdate()
        resp = self._get(
            self.admin,
            date_from=(today - datetime.timedelta(days=1)).isoformat(),
            date_to=today.isoformat(),
        )

        self.assertEqual(resp.context['total_orders'], 1)
        self.assertEqual(resp.context['avg_days'], 9.0)

    def test_current_load_counts_open_orders_by_the_last_person_who_touched_them(self):
        self._make_order(
            received_days_ago=4,
            transitions=[('diagnostic', 3, self.manager1), ('repair', 2, self.manager1)],
        )
        self._make_order(
            received_days_ago=2,
            transitions=[('diagnostic', 1, self.manager2)],
        )
        # Только что создан, статус ремонта не менялся вовсе — не в чьей
        # загрузке не отражается (у автосозданной записи истории нет автора)
        RepairOrder.objects.create(client=self.client_obj, status='accepted')

        resp = self._get(self.admin)

        load = {row['employee'].pk: row['count'] for row in resp.context['load_rows']}
        self.assertEqual(load[self.manager1.pk], 1)
        self.assertEqual(load[self.manager2.pk], 1)

    def test_current_load_ignores_completed_orders(self):
        self._build_dataset()  # все три завершены (отгружены/неремонтопригодны)

        resp = self._get(self.admin)

        load = {row['employee'].pk: row['count'] for row in resp.context['load_rows']}
        self.assertEqual(load.get(self.manager1.pk, 0), 0)
        self.assertEqual(load.get(self.manager2.pk, 0), 0)

    def test_admin_sees_the_whole_report(self):
        self._build_dataset()

        resp = self._get(self.admin, date_from='2000-01-01')

        self.assertTrue(resp.context['is_admin'])
        self.assertEqual(resp.context['total_orders'], 3)
        self.assertEqual(len(resp.context['by_employee']), 2)

    def test_a_non_admin_role_sees_only_their_own_figures(self):
        """Менеджер по ремонту не должен видеть ни отчёт целиком, ни
        показатели коллеги — только свой заказ 1 (manager2 к нему не
        имеет отношения, хотя и отгружал его)."""
        self._build_dataset()

        resp = self._get(self.manager1, date_from='2000-01-01')

        self.assertFalse(resp.context['is_admin'])
        self.assertEqual(resp.context['total_orders'], 1)
        self.assertEqual(resp.context['avg_days'], 9.0)

    def test_a_non_admin_role_does_not_see_a_colleagues_load(self):
        self._make_order(
            received_days_ago=4,
            transitions=[('diagnostic', 3, self.manager1), ('repair', 2, self.manager1)],
        )
        self._make_order(
            received_days_ago=2,
            transitions=[('diagnostic', 1, self.manager2)],
        )

        resp = self._get(self.manager1)

        self.assertEqual(len(resp.context['load_rows']), 1)
        self.assertEqual(resp.context['load_rows'][0]['employee'], self.manager1)
        self.assertEqual(resp.context['load_rows'][0]['count'], 1)

    def test_a_role_with_no_repairs_of_its_own_sees_an_empty_personal_report(self):
        self._build_dataset()
        warehouse = Employee.objects.get(username='sklad_ra')

        resp = self._get(warehouse, date_from='2000-01-01')

        self.assertFalse(resp.context['is_admin'])
        self.assertEqual(resp.context['total_orders'], 0)

    def test_the_report_page_loads_without_error(self):
        self._build_dataset()

        resp = self._get(self.admin)

        self.assertEqual(resp.status_code, 200)

    def test_export_has_three_sheets_with_the_expected_data(self):
        self._build_dataset()
        self.client_http.force_login(self.admin)

        resp = self.client_http.get('/reports/repair-analytics/export/', {'date_from': '2000-01-01'})

        self.assertEqual(resp.status_code, 200)
        wb = openpyxl.load_workbook(io.BytesIO(resp.content))
        self.assertEqual(
            set(wb.sheetnames),
            {'По инженерам', 'По типу неисправности', 'Текущая загрузка'},
        )

        engineers = {row[0].value: row[1].value for row in wb['По инженерам'].iter_rows(min_row=2)}
        self.assertEqual(engineers.get('Иванов'), 1)

        faults = {row[0].value: row[1].value for row in wb['По типу неисправности'].iter_rows(min_row=2)}
        self.assertEqual(faults.get(str(self.fault_a)), 1)


class DebtReportTests(TestCase):
    """Отчёт «Задолженности»: просрочка и след от напоминаний."""

    def setUp(self):
        self.admin = Employee.objects.create_superuser(
            username='admin_debt', full_name='Админ', password='pass')
        self.client_http = TestClient()
        self.client_http.force_login(self.admin)

        client_obj = ClientModel.objects.create(name='ООО Должник')
        self.order = RepairOrder.objects.create(
            client=client_obj, payment_status='unpaid',
            invoice_date=datetime.date.today() - datetime.timedelta(days=30))
        model = EquipmentModel.objects.create(name='БУАД-отчёт')
        RepairOrderEquipment.objects.create(
            repair_order=self.order,
            equipment=Equipment.objects.create(model=model, serial_number='SN-R1'),
            repair_cost=2500,
        )

    @override_settings(DEBT_OVERDUE_DAYS=14)
    def test_overdue_is_marked(self):
        resp = self.client_http.get('/reports/debtors/')

        self.assertContains(resp, 'просрочен 30 дн.')

    def test_last_reminder_is_shown(self):
        """Иначе на вопрос «мы им вообще писали?» отвечать нечем."""
        Notification.objects.create(
            event='debt_reminder', recipient='debtor@example.com',
            subject='Оплата', body='Текст', repair_order=self.order,
        )
        resp = self.client_http.get('/reports/debtors/')

        self.assertEqual(
            resp.context['orders'][0].last_reminder.date(), datetime.date.today())

    def test_the_report_shows_the_remainder(self):
        Payment.objects.create(repair_order=self.order, amount=1000)

        resp = self.client_http.get('/reports/debtors/')

        self.assertEqual(resp.context['total_debt'], Decimal('1500'))
        self.assertEqual(resp.context['orders'][0].debt, Decimal('1500'))

    def test_the_total_is_not_multiplied_by_several_payments(self):
        """Соединение с оплатами могло посчитать заказ трижды."""
        for _ in range(3):
            Payment.objects.create(repair_order=self.order, amount=100)

        resp = self.client_http.get('/reports/debtors/')

        self.assertEqual(resp.context['total_debt'], Decimal('2200'))

    def test_total_is_not_doubled_by_the_reminder_join(self):
        """Регрессия: соединение с очередью могло посчитать сумму дважды."""
        for _ in range(3):
            Notification.objects.create(
                event='debt_reminder', recipient='debtor@example.com',
                subject='Оплата', body='Текст', repair_order=self.order,
            )
        resp = self.client_http.get('/reports/debtors/')

        self.assertEqual(resp.context['total_debt'], 2500)
        self.assertEqual(len(resp.context['orders']), 1)


class MaxTransportTests(TestCase):
    """Отправка в MAX: что именно уходит в сеть и как разбирается ответ."""

    def _fake_urlopen(self, body='{"message": {}}', calls=None):
        """Подменяет сеть, запоминая запрос."""
        class _Response:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *args):
                return False

            def read(self_inner):
                return body.encode('utf-8')

        def _open(req, timeout=None):
            if calls is not None:
                calls.append(req)
            return _Response()

        return _open

    @override_settings(MAX_BOT_TOKEN='secret-token',
                       MAX_API_URL='https://platform-api2.max.ru')
    def test_personal_message_goes_to_user_id(self):
        calls = []
        with patch('core.messengers.request.urlopen', self._fake_urlopen(calls=calls)):
            messengers.send_max_message('user:842910', 'Дефицит')

        request_sent = calls[0]
        self.assertEqual(
            request_sent.full_url,
            'https://platform-api2.max.ru/messages?user_id=842910',
        )
        self.assertEqual(json.loads(request_sent.data.decode()), {'text': 'Дефицит'})

    @override_settings(MAX_BOT_TOKEN='secret-token')
    def test_token_goes_in_the_header_without_bearer(self):
        """MAX ждёт голый токен: с префиксом Bearer запрос отвергается."""
        calls = []
        with patch('core.messengers.request.urlopen', self._fake_urlopen(calls=calls)):
            messengers.send_max_message('user:1', 'текст')

        self.assertEqual(calls[0].get_header('Authorization'), 'secret-token')

    @override_settings(MAX_BOT_TOKEN='secret-token')
    def test_group_chat_goes_to_chat_id(self):
        calls = []
        with patch('core.messengers.request.urlopen', self._fake_urlopen(calls=calls)):
            messengers.send_max_message('chat:-98765', 'текст')

        self.assertIn('chat_id=-98765', calls[0].full_url)

    @override_settings(MAX_BOT_TOKEN='')
    def test_without_token_it_refuses(self):
        with self.assertRaises(messengers.MaxError):
            messengers.send_max_message('user:1', 'текст')

    @override_settings(MAX_BOT_TOKEN='secret-token')
    def test_unknown_recipient_format_is_rejected(self):
        with self.assertRaises(messengers.MaxError):
            messengers.send_max_message('wh@example.com', 'текст')

    @override_settings(MAX_BOT_TOKEN='secret-token')
    def test_error_inside_a_200_response_is_noticed(self):
        """MAX умеет отвечать «200 OK» с ошибкой в теле."""
        body = '{"code": "not.found", "message": "chat not found"}'
        with patch('core.messengers.request.urlopen', self._fake_urlopen(body=body)):
            with self.assertRaises(messengers.MaxError) as caught:
                messengers.send_max_message('user:1', 'текст')

        self.assertIn('chat not found', str(caught.exception))

    @override_settings(MAX_BOT_TOKEN='secret-token')
    def test_http_error_becomes_a_readable_reason(self):
        import urllib.error

        failure = urllib.error.HTTPError(
            'https://platform-api2.max.ru/messages', 401, 'Unauthorized',
            {}, io.BytesIO(b'{"message": "invalid access_token"}')
        )
        with patch('core.messengers.request.urlopen', side_effect=failure):
            with self.assertRaises(messengers.MaxError) as caught:
                messengers.send_max_message('user:1', 'текст')

        self.assertIn('401', str(caught.exception))
        self.assertIn('invalid access_token', str(caught.exception))


class MaxQueueTests(TestCase):
    """Кому ставится оповещение в MAX при дефиците детали."""

    def setUp(self):
        Employee.objects.create_user(
            username='wh_max', full_name='Кладовщик', password='pass',
            role='warehouse', email='wh@example.com', max_user_id='842910',
        )
        Employee.objects.create_user(
            username='wh_nomax', full_name='Без MAX', password='pass',
            role='warehouse', email='wh2@example.com',
        )
        Employee.objects.create_user(
            username='acc_max', full_name='Бухгалтер', password='pass',
            role='accountant', email='acc@example.com', max_user_id='111',
        )
        self.part = SparePart.objects.create(
            part_number='MAX-1', name='Резистор', current_stock=10, min_stock=5
        )

    def _spend(self):
        self.part.current_stock -= 6
        self.part.save(update_fields=['current_stock'])

    def _max_recipients(self):
        return set(
            Notification.objects.filter(channel='max').values_list('recipient', flat=True)
        )

    @override_settings(NOTIFY_MAX=True, MAX_BOT_TOKEN='t', MAX_GROUP_CHAT_ID='')
    def test_shortage_reaches_those_who_gave_their_id(self):
        self._spend()

        self.assertEqual(self._max_recipients(), {'user:842910'})
        # Почта при этом никуда не делась
        self.assertEqual(
            set(Notification.objects.filter(channel='email').values_list('recipient', flat=True)),
            {'wh@example.com', 'wh2@example.com'},
        )

    @override_settings(NOTIFY_MAX=True, MAX_BOT_TOKEN='t', MAX_GROUP_CHAT_ID='-98765')
    def test_group_chat_replaces_personal_messages(self):
        """Три одинаковых сообщения подряд — это не оповещение, а шум."""
        self._spend()

        self.assertEqual(self._max_recipients(), {'chat:-98765'})

    @override_settings(NOTIFY_MAX=False, MAX_BOT_TOKEN='t')
    def test_disabled_channel_queues_nothing(self):
        self._spend()
        self.assertEqual(Notification.objects.filter(channel='max').count(), 0)

    @override_settings(NOTIFY_MAX=True, MAX_BOT_TOKEN='')
    def test_without_a_token_the_channel_is_silent(self):
        self._spend()
        self.assertEqual(Notification.objects.filter(channel='max').count(), 0)

    @override_settings(NOTIFY_MAX=True, MAX_BOT_TOKEN='t', MAX_GROUP_CHAT_ID='')
    def test_message_carries_the_subject_inside_the_text(self):
        """У сообщения в мессенджере нет отдельного поля темы."""
        self._spend()
        note = Notification.objects.get(channel='max')

        self.assertTrue(note.body.startswith(note.subject))
        self.assertIn('MAX-1', note.body)


class PersonalNotificationChoiceTests(TestCase):
    """Личный выбор канала (`notify_by_*`) поверх глобального включения."""

    def setUp(self):
        self.opted_out = Employee.objects.create_user(
            username='wh_optout', full_name='Отключил MAX', password='pass',
            role='warehouse', email='optout@example.com', max_user_id='555',
            notify_by_max=False,
        )
        self.opted_in = Employee.objects.create_user(
            username='wh_optin', full_name='Не отключал', password='pass',
            role='warehouse', email='optin@example.com', max_user_id='777',
        )
        self.part = SparePart.objects.create(
            part_number='PNC-1', name='Резистор', current_stock=10, min_stock=5
        )

    def _spend(self):
        self.part.current_stock -= 6
        self.part.save(update_fields=['current_stock'])

    @override_settings(NOTIFY_MAX=True, MAX_BOT_TOKEN='t', MAX_GROUP_CHAT_ID='')
    def test_opting_out_silences_the_channel_even_with_an_id_on_file(self):
        """Личный выбор перевешивает и глобальное включение, и заполненный ID."""
        self._spend()

        recipients = set(
            Notification.objects.filter(channel='max').values_list('recipient', flat=True)
        )
        self.assertEqual(recipients, {'user:777'})

    @override_settings(NOTIFY_MAX=True, MAX_BOT_TOKEN='t', MAX_GROUP_CHAT_ID='')
    def test_an_empty_id_still_means_silence_regardless_of_the_choice(self):
        """Поведение без ID не меняется: раньше молчал, молчит и теперь."""
        self.opted_in.max_user_id = ''
        self.opted_in.save(update_fields=['max_user_id'])
        self._spend()

        self.assertEqual(Notification.objects.filter(channel='max').count(), 0)


class MyNotificationsPageTests(TestCase):
    """Страница «Мои оповещения»: каждый видит и правит только свои три поля."""

    def setUp(self):
        self.employee = Employee.objects.create_user(
            username='my_notif', full_name='Сотрудник', password='pass',
            role='warehouse',
        )
        self.client_http = TestClient()
        self.client_http.force_login(self.employee)

    def test_shows_current_settings(self):
        response = self.client_http.get('/my-notifications/')

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['form'].initial.get('notify_by_max', True))

    def test_saves_own_choice(self):
        response = self.client_http.post('/my-notifications/', {
            'notify_by_email': '',
            'notify_by_max': 'on',
            'notify_by_telegram': '',
        })
        self.assertEqual(response.status_code, 302)

        self.employee.refresh_from_db()
        self.assertFalse(self.employee.notify_by_email)
        self.assertTrue(self.employee.notify_by_max)
        self.assertFalse(self.employee.notify_by_telegram)

    def test_does_not_touch_role_or_password(self):
        """Своя форма не даёт поменять то, что не про личный выбор канала."""
        self.client_http.post('/my-notifications/', {
            'notify_by_email': 'on', 'notify_by_max': 'on', 'notify_by_telegram': 'on',
            'role': 'admin',
        })

        self.employee.refresh_from_db()
        self.assertEqual(self.employee.role, 'warehouse')


class AdminEditsStaffNotificationChoiceTests(TestCase):
    """Администратор может поменять флаги оповещений другому сотруднику."""

    def setUp(self):
        self.admin = Employee.objects.create_superuser(
            username='admin_notif_edit', full_name='Админ', password='pass'
        )
        self.staff = Employee.objects.create_user(
            username='staff_notif_edit', full_name='Кладовщик', password='pass',
            role='warehouse', email='staff@example.com',
        )
        self.client_http = TestClient()
        self.client_http.force_login(self.admin)

    def test_admin_can_turn_channels_off_for_another_employee(self):
        response = self.client_http.post(
            f'/management/users/{self.staff.pk}/edit/',
            {
                'username': self.staff.username,
                'full_name': self.staff.full_name,
                'email': self.staff.email,
                'max_user_id': '', 'telegram_chat_id': '',
                'role': 'warehouse', 'is_active': 'on',
                'notify_by_email': '',
                'notify_by_max': '',
                'notify_by_telegram': '',
            },
        )
        self.assertEqual(response.status_code, 302)

        self.staff.refresh_from_db()
        self.assertFalse(self.staff.notify_by_email)
        self.assertFalse(self.staff.notify_by_max)
        self.assertFalse(self.staff.notify_by_telegram)


class MaxDeliveryTests(TestCase):
    """Команда отправки: оповещение MAX не должно уходить почтой и наоборот."""

    def setUp(self):
        self.letter = Notification.objects.create(
            event='low_stock', recipient='wh@example.com',
            subject='Дефицит', body='Текст',
        )
        self.message = Notification.objects.create(
            event='low_stock', channel='max', recipient='user:842910',
            subject='Дефицит', body='Дефицит\n\nТекст',
        )

    def _run(self):
        call_command('send_notifications', stdout=io.StringIO(), stderr=io.StringIO())

    @override_settings(NOTIFICATIONS_ENABLED=True, MAX_BOT_TOKEN='t',
                       EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
    def test_each_channel_uses_its_own_transport(self):
        from django.core import mail

        with patch('core.messengers.send_max_message') as send_max:
            self._run()

        self.letter.refresh_from_db()
        self.message.refresh_from_db()

        send_max.assert_called_once_with('user:842910', 'Дефицит\n\nТекст')
        self.assertEqual([m.to for m in mail.outbox], [['wh@example.com']])
        self.assertEqual(self.letter.status, 'sent')
        self.assertEqual(self.message.status, 'sent')

    @override_settings(NOTIFICATIONS_ENABLED=True, MAX_BOT_TOKEN='t',
                       NOTIFICATIONS_MAX_ATTEMPTS=2,
                       EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
    def test_a_broken_messenger_does_not_stop_the_post(self):
        from django.core import mail

        with patch('core.messengers.send_max_message',
                   side_effect=messengers.MaxError('MAX недоступен')):
            self._run()

        self.letter.refresh_from_db()
        self.message.refresh_from_db()

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(self.letter.status, 'sent')
        self.assertEqual(self.message.status, 'pending')
        self.assertIn('MAX недоступен', self.message.last_error)

    @override_settings(NOTIFICATIONS_ENABLED=True, MAX_BOT_TOKEN='t')
    def test_a_max_only_batch_does_not_open_smtp(self):
        """Иначе очередь из одних сообщений упиралась бы в почтовый сервер."""
        self.letter.delete()

        with patch('core.messengers.send_max_message'), \
             patch('core.management.commands.send_notifications.get_connection') as connect:
            self._run()

        connect.assert_not_called()


class TelegramTransportTests(TestCase):
    """Отправка в Telegram: что уходит в сеть и как разбирается ответ."""

    def _fake_urlopen(self, body='{"ok": true, "result": {}}', calls=None):
        class _Response:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *args):
                return False

            def read(self_inner):
                return body.encode('utf-8')

        def _open(req, timeout=None):
            if calls is not None:
                calls.append(req)
            return _Response()

        return _open

    @override_settings(TELEGRAM_BOT_TOKEN='123:ABC',
                       TELEGRAM_API_URL='https://api.telegram.org')
    def test_token_is_part_of_the_address(self):
        """У Telegram токен в адресе, а не в заголовке, — в отличие от MAX."""
        calls = []
        with patch('core.messengers.request.urlopen', self._fake_urlopen(calls=calls)):
            messengers.send_telegram_message('842910', 'Дефицит')

        self.assertEqual(
            calls[0].full_url,
            'https://api.telegram.org/bot123:ABC/sendMessage',
        )
        self.assertEqual(
            json.loads(calls[0].data.decode()),
            {'chat_id': '842910', 'text': 'Дефицит'},
        )

    @override_settings(TELEGRAM_BOT_TOKEN='123:ABC')
    def test_group_looks_the_same_as_a_person(self):
        """В Telegram и человек, и группа — это chat_id."""
        calls = []
        with patch('core.messengers.request.urlopen', self._fake_urlopen(calls=calls)):
            messengers.send_telegram_message('-1001234567890', 'текст')

        self.assertEqual(
            json.loads(calls[0].data.decode())['chat_id'], '-1001234567890'
        )

    @override_settings(TELEGRAM_BOT_TOKEN='')
    def test_without_token_it_refuses(self):
        with self.assertRaises(messengers.TelegramError):
            messengers.send_telegram_message('1', 'текст')

    @override_settings(TELEGRAM_BOT_TOKEN='123:ABC')
    def test_empty_recipient_is_rejected(self):
        with self.assertRaises(messengers.TelegramError):
            messengers.send_telegram_message('  ', 'текст')

    @override_settings(TELEGRAM_BOT_TOKEN='123:ABC')
    def test_refusal_inside_a_200_response_is_noticed(self):
        """«ok: false» приходит и с кодом 200 — например, если бота заблокировали."""
        body = '{"ok": false, "error_code": 403, "description": "bot was blocked by the user"}'
        with patch('core.messengers.request.urlopen', self._fake_urlopen(body=body)):
            with self.assertRaises(messengers.TelegramError) as caught:
                messengers.send_telegram_message('1', 'текст')

        self.assertIn('bot was blocked', str(caught.exception))

    @override_settings(TELEGRAM_BOT_TOKEN='123:ABC')
    def test_unreachable_telegram_becomes_a_readable_reason(self):
        """Из России Telegram может быть попросту недоступен."""
        import urllib.error

        with patch('core.messengers.request.urlopen',
                   side_effect=urllib.error.URLError('Network is unreachable')):
            with self.assertRaises(messengers.TelegramError) as caught:
                messengers.send_telegram_message('1', 'текст')

        self.assertIn('Telegram недоступен', str(caught.exception))


class TelegramQueueTests(TestCase):
    """Кому ставится оповещение в Telegram при дефиците детали."""

    def setUp(self):
        Employee.objects.create_user(
            username='wh_tg', full_name='Кладовщик', password='pass',
            role='warehouse', email='wh@example.com', telegram_chat_id='842910',
        )
        Employee.objects.create_user(
            username='wh_notg', full_name='Без Telegram', password='pass',
            role='warehouse', email='wh2@example.com',
        )
        self.part = SparePart.objects.create(
            part_number='TG-1', name='Резистор', current_stock=10, min_stock=5
        )

    def _spend(self):
        self.part.current_stock -= 6
        self.part.save(update_fields=['current_stock'])

    def _recipients(self):
        return set(
            Notification.objects.filter(channel='telegram')
            .values_list('recipient', flat=True)
        )

    @override_settings(NOTIFY_TELEGRAM=True, TELEGRAM_BOT_TOKEN='123:ABC',
                       TELEGRAM_GROUP_CHAT_ID='')
    def test_shortage_reaches_those_who_gave_their_id(self):
        self._spend()
        self.assertEqual(self._recipients(), {'842910'})

    @override_settings(NOTIFY_TELEGRAM=True, TELEGRAM_BOT_TOKEN='123:ABC',
                       TELEGRAM_GROUP_CHAT_ID='-1001234567890')
    def test_group_replaces_personal_messages(self):
        self._spend()
        self.assertEqual(self._recipients(), {'-1001234567890'})

    @override_settings(NOTIFY_TELEGRAM=False, TELEGRAM_BOT_TOKEN='123:ABC')
    def test_disabled_channel_queues_nothing(self):
        self._spend()
        self.assertEqual(Notification.objects.filter(channel='telegram').count(), 0)

    @override_settings(NOTIFY_TELEGRAM=True, TELEGRAM_BOT_TOKEN='')
    def test_without_a_token_the_channel_is_silent(self):
        self._spend()
        self.assertEqual(Notification.objects.filter(channel='telegram').count(), 0)

    @override_settings(NOTIFY_TELEGRAM=True, TELEGRAM_BOT_TOKEN='123:ABC',
                       TELEGRAM_GROUP_CHAT_ID='',
                       NOTIFY_MAX=True, MAX_BOT_TOKEN='t', MAX_GROUP_CHAT_ID='-9')
    def test_channels_do_not_interfere(self):
        """Оба мессенджера и почта работают одновременно и независимо."""
        self._spend()

        self.assertEqual(self._recipients(), {'842910'})
        self.assertEqual(
            set(Notification.objects.filter(channel='max').values_list('recipient', flat=True)),
            {'chat:-9'},
        )
        self.assertEqual(Notification.objects.filter(channel='email').count(), 2)


class TelegramDeliveryTests(TestCase):
    """Команда отправки: Telegram идёт своим транспортом."""

    def setUp(self):
        self.message = Notification.objects.create(
            event='low_stock', channel='telegram', recipient='842910',
            subject='Дефицит', body='Дефицит\n\nТекст',
        )

    def _run(self):
        call_command('send_notifications', stdout=io.StringIO(), stderr=io.StringIO())

    @override_settings(NOTIFICATIONS_ENABLED=True, TELEGRAM_BOT_TOKEN='123:ABC')
    def test_message_goes_through_telegram_and_not_the_post(self):
        with patch('core.messengers.send_telegram_message') as send, \
             patch('core.management.commands.send_notifications.get_connection') as connect:
            self._run()

        self.message.refresh_from_db()

        send.assert_called_once_with('842910', 'Дефицит\n\nТекст')
        connect.assert_not_called()
        self.assertEqual(self.message.status, 'sent')

    @override_settings(NOTIFICATIONS_ENABLED=True, TELEGRAM_BOT_TOKEN='123:ABC',
                       NOTIFICATIONS_MAX_ATTEMPTS=2)
    def test_failure_is_recorded_and_retried(self):
        with patch('core.messengers.send_telegram_message',
                   side_effect=messengers.TelegramError('Telegram недоступен')):
            self._run()

        self.message.refresh_from_db()

        self.assertEqual(self.message.status, 'pending')
        self.assertEqual(self.message.attempts, 1)
        self.assertIn('Telegram недоступен', self.message.last_error)


class MaxRecipientDisplayTests(TestCase):
    """«user:842910» в списке оповещений ни о чём не говорит."""

    def test_known_employee_is_shown_by_name(self):
        Employee.objects.create_user(
            username='wh_disp', full_name='Иванов И.И.', password='pass',
            role='warehouse', max_user_id='842910',
        )
        note = Notification.objects.create(
            event='low_stock', channel='max', recipient='user:842910',
            subject='Дефицит', body='Текст',
        )

        self.assertEqual(note.recipient_display, 'Иванов И.И. (MAX)')

    def test_unknown_id_is_still_readable(self):
        note = Notification.objects.create(
            event='low_stock', channel='max', recipient='user:1',
            subject='Дефицит', body='Текст',
        )
        self.assertIn('1', note.recipient_display)

    def test_email_is_shown_as_is(self):
        note = Notification.objects.create(
            event='low_stock', recipient='wh@example.com',
            subject='Дефицит', body='Текст',
        )
        self.assertEqual(note.recipient_display, 'wh@example.com')

    def test_telegram_employee_is_shown_by_name(self):
        Employee.objects.create_user(
            username='wh_tg_disp', full_name='Сидоров С.С.', password='pass',
            role='warehouse', telegram_chat_id='842910',
        )
        note = Notification.objects.create(
            event='low_stock', channel='telegram', recipient='842910',
            subject='Дефицит', body='Текст',
        )

        self.assertEqual(note.recipient_display, 'Сидоров С.С. (Telegram)')

    def test_telegram_group_is_told_apart_by_the_minus(self):
        """Идентификатор группы в Telegram отрицательный."""
        note = Notification.objects.create(
            event='low_stock', channel='telegram', recipient='-1001234567890',
            subject='Дефицит', body='Текст',
        )

        self.assertIn('чат', note.recipient_display)


class NotificationAdminPageTests(TestCase):
    def setUp(self):
        self.admin = Employee.objects.create_superuser(
            username='admin_np', full_name='Админ', password='pass'
        )
        self.staff = Employee.objects.create_user(
            username='wh_np', full_name='Кладовщик', password='pass', role='warehouse'
        )
        self.client_http = TestClient()
        self.client_http.force_login(self.admin)

        self.failed = Notification.objects.create(
            event='low_stock', recipient='wh@example.com', subject='Дефицит',
            body='Текст', status='failed', attempts=5, last_error='SMTP молчит',
        )

    def test_page_lists_the_queue(self):
        resp = self.client_http.get('/management/notifications/')

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'wh@example.com')
        self.assertContains(resp, 'SMTP молчит')

    def test_max_row_shows_the_channel_and_the_person(self):
        Employee.objects.create_user(
            username='wh_max_page', full_name='Петров П.П.', password='pass',
            role='warehouse', max_user_id='842910',
        )
        Notification.objects.create(
            event='low_stock', channel='max', recipient='user:842910',
            subject='Дефицит', body='Текст',
        )
        resp = self.client_http.get('/management/notifications/')

        self.assertContains(resp, 'MAX')
        self.assertContains(resp, 'Петров П.П.')

    def test_status_filter(self):
        Notification.objects.create(
            event='low_stock', recipient='other@example.com',
            subject='В очереди', body='Текст',
        )
        resp = self.client_http.get('/management/notifications/?status=failed')

        self.assertEqual([n.pk for n in resp.context['notifications']], [self.failed.pk])

    def test_retry_returns_it_to_the_queue(self):
        self.client_http.post(f'/management/notifications/{self.failed.pk}/retry/')
        self.failed.refresh_from_db()

        self.assertEqual(self.failed.status, 'pending')
        self.assertEqual(self.failed.attempts, 0)
        self.assertEqual(self.failed.last_error, '')

    def test_only_admin_gets_in(self):
        staff_client = TestClient()
        staff_client.force_login(self.staff)

        resp = staff_client.get('/management/notifications/')
        self.assertEqual(resp.status_code, 302)

    def test_page_warns_when_sending_is_off(self):
        with override_settings(NOTIFICATIONS_ENABLED=False):
            resp = self.client_http.get('/management/notifications/')
        self.assertFalse(resp.context['sending_enabled'])


class TBankTransportTests(TestCase):
    """Обращение к банку: что уходит в сеть и как разбирается ответ."""

    def _fake_urlopen(self, body='{"operations": []}', calls=None):
        class _Response:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *args):
                return False

            def read(self_inner):
                return body.encode('utf-8')

        def _open(req, timeout=None):
            if calls is not None:
                calls.append(req)
            return _Response()

        return _open

    @override_settings(TBANK_TOKEN='secret', TBANK_ACCOUNT='40802810700006096146',
                       TBANK_API_URL='https://business.tbank.ru/openapi')
    def test_statement_request_carries_account_and_period(self):
        calls = []
        with patch('core.tbank.request.urlopen', self._fake_urlopen(calls=calls)):
            tbank.get_statement(datetime.date(2026, 8, 1), datetime.date(2026, 8, 13))

        sent = calls[0]
        self.assertIn('/openapi/api/v1/statement?', sent.full_url)
        self.assertIn('accountNumber=40802810700006096146', sent.full_url)
        self.assertIn('from=2026-08-01', sent.full_url)
        self.assertIn('till=2026-08-13', sent.full_url)

    @override_settings(TBANK_TOKEN='secret', TBANK_ACCOUNT='40802810700006096146')
    def test_token_goes_as_bearer(self):
        calls = []
        with patch('core.tbank.request.urlopen', self._fake_urlopen(calls=calls)):
            tbank.get_statement(datetime.date(2026, 8, 1), datetime.date(2026, 8, 2))

        self.assertEqual(calls[0].get_header('Authorization'), 'Bearer secret')

    @override_settings(TBANK_TOKEN='secret', TBANK_ACCOUNT='')
    def test_statement_without_an_account_says_so(self):
        with self.assertRaises(tbank.TBankError) as caught:
            tbank.get_statement(datetime.date(2026, 8, 1), datetime.date(2026, 8, 2))

        self.assertIn('TBANK_ACCOUNT', str(caught.exception))

    @override_settings(TBANK_TOKEN='')
    def test_without_a_token_the_channel_is_off(self):
        self.assertFalse(tbank.is_configured())

    @override_settings(TBANK_TOKEN='secret', TBANK_ACCOUNT='123')
    def test_an_expired_token_is_named_not_just_numbered(self):
        def _fail(req, timeout=None):
            raise urllib_error.HTTPError(req.full_url, 401, 'Unauthorized', {}, io.BytesIO(b'{}'))

        with patch('core.tbank.request.urlopen', _fail):
            with self.assertRaises(tbank.TBankError) as caught:
                tbank.get_statement(datetime.date(2026, 8, 1), datetime.date(2026, 8, 2))

        self.assertIn('TBANK_TOKEN', str(caught.exception))


class TBankParsingTests(TestCase):
    """Разбор выписки. Имена полей у банка не одни и те же, отсюда терпимость."""

    def test_operations_are_found_under_any_known_key(self):
        for key in ('operations', 'items', 'data', 'result', 'transactions'):
            payload = {key: [{'id': '1'}]}

            self.assertEqual(len(tbank.operation_list(payload)), 1, key)

    def test_a_bare_list_is_a_list_of_operations(self):
        self.assertEqual(len(tbank.operation_list([{'id': '1'}, {'id': '2'}])), 2)

    def test_amount_is_read_from_a_number_a_string_or_an_object(self):
        cases = [
            {'id': '1', 'typeOfOperation': 'Credit', 'amount': 14000},
            {'id': '1', 'typeOfOperation': 'Credit', 'amount': '14 000,00'},
            {'id': '1', 'typeOfOperation': 'Credit', 'amount': {'value': '14000.00'}},
        ]
        for raw in cases:
            self.assertEqual(tbank.parse_operation(raw)['amount'], Decimal('14000'), raw)

    def test_date_is_read_with_or_without_time(self):
        for value in ('2026-08-13', '2026-08-13T10:20:30+03:00', '13.08.2026'):
            parsed = tbank.parse_operation({'id': '1', 'operationDate': value})

            self.assertEqual(parsed['operation_date'], datetime.date(2026, 8, 13), value)

    def test_counterparty_is_read_from_a_nested_object_too(self):
        parsed = tbank.parse_operation({
            'id': '1',
            'counterParty': {'name': 'ООО «ЛИФТПРОЕКТ»', 'inn': '9722051089'},
        })

        self.assertEqual(parsed['counterparty'], 'ООО «ЛИФТПРОЕКТ»')
        self.assertEqual(parsed['counterparty_inn'], '9722051089')

    def test_only_incoming_money_is_taken(self):
        payload = {'operations': [
            {'id': 'in', 'typeOfOperation': 'Credit', 'amount': 100},
            {'id': 'out', 'typeOfOperation': 'Debit', 'amount': 100},
        ]}

        found = tbank.incoming_operations(payload)

        self.assertEqual([item['external_id'] for item in found], ['in'])

    def test_without_a_direction_a_negative_amount_is_an_expense(self):
        payload = {'operations': [
            {'id': 'in', 'amount': 100},
            {'id': 'out', 'amount': -100},
        ]}

        found = tbank.incoming_operations(payload)

        self.assertEqual([item['external_id'] for item in found], ['in'])

    def test_an_operation_without_an_id_is_dropped(self):
        """Без идентификатора её не отличить от такой же в следующей выписке."""
        payload = {'operations': [{'typeOfOperation': 'Credit', 'amount': 100}]}

        self.assertEqual(tbank.incoming_operations(payload), [])


class BankOperationTests(TestCase):
    """Поступления из выписки и разнесение их по заказам."""

    def setUp(self):
        self.accountant = Employee.objects.create_user(
            username='buh_bank', full_name='Бухгалтер', password='pass',
            role='accountant')
        self.warehouse = Employee.objects.create_user(
            username='sklad_bank', full_name='Кладовщик', password='pass',
            role='warehouse')
        self.client_http = TestClient()
        self.client_http.force_login(self.accountant)

        self.customer = ClientModel.objects.create(
            name='ООО «ЛИФТПРОЕКТ»', inn='9722051089')
        self.order = RepairOrder.objects.create(
            client=self.customer, invoice_number='942', invoice_date=datetime.date(2026, 8, 1))
        model = EquipmentModel.objects.create(name='Emotron-банк')
        RepairOrderEquipment.objects.create(
            repair_order=self.order,
            equipment=Equipment.objects.create(model=model, serial_number='SN-BANK'),
            repair_cost=Decimal('14000'),
        )

    def _operation(self, **overrides):
        data = {
            'external_id': 'op-1',
            'operation_date': datetime.date(2026, 8, 10),
            'amount': Decimal('14000'),
            'purpose': 'Оплата по счету 942 от 01.08.2026, без НДС',
            'counterparty': 'ООО «ЛИФТПРОЕКТ»',
            'counterparty_inn': '9722051089',
            'document_number': '178',
        }
        data.update(overrides)
        return BankOperation.objects.create(**data)

    def test_the_order_is_guessed_by_the_invoice_number(self):
        operation = self._operation()

        self.assertEqual(operation.guess_orders(), [self.order])

    def test_the_order_number_in_the_purpose_wins_over_the_inn(self):
        """Точное попадание не должно тонуть среди прочих долгов заказчика."""
        other = RepairOrder.objects.create(client=self.customer)
        operation = self._operation(
            purpose=f'Оплата по заказу {other.order_number}')

        self.assertEqual(operation.guess_orders(), [other])

    def test_an_invoice_number_with_a_suffix_still_matches(self):
        operation = self._operation(purpose='Оплата счет № 942-01 за ремонт')

        self.assertEqual(operation.guess_orders(), [self.order])

    def test_without_any_hint_debts_of_the_payer_are_offered(self):
        operation = self._operation(purpose='Оплата за услуги')

        self.assertEqual(operation.guess_orders(), [self.order])

    def test_a_stranger_gets_no_suggestions(self):
        operation = self._operation(purpose='Возврат средств', counterparty_inn='7700000000')

        self.assertEqual(operation.guess_orders(), [])

    def test_applying_creates_a_payment_and_closes_the_debt(self):
        operation = self._operation()

        resp = self.client_http.post(
            f'/bank/operations/{operation.pk}/apply/', {'order': self.order.pk})

        self.assertRedirects(resp, '/bank/operations/')
        operation.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(operation.status, 'applied')
        self.assertEqual(operation.payment.amount, Decimal('14000'))
        self.assertEqual(operation.payment.payment_date, datetime.date(2026, 8, 10))
        self.assertEqual(self.order.paid_amount, Decimal('14000'))
        self.assertEqual(self.order.payment_status, 'paid')

    def test_the_payment_note_says_where_the_money_came_from(self):
        operation = self._operation()

        self.client_http.post(
            f'/bank/operations/{operation.pk}/apply/', {'order': self.order.pk})

        operation.refresh_from_db()
        self.assertIn('Выписка Т-Банка', operation.payment.note)
        self.assertIn('178', operation.payment.note)

    def test_the_same_money_cannot_be_applied_twice(self):
        operation = self._operation()
        self.client_http.post(
            f'/bank/operations/{operation.pk}/apply/', {'order': self.order.pk})

        self.client_http.post(
            f'/bank/operations/{operation.pk}/apply/', {'order': self.order.pk})

        self.order.refresh_from_db()
        self.assertEqual(self.order.payments.count(), 1)
        self.assertEqual(self.order.paid_amount, Decimal('14000'))

    def test_cancelling_removes_the_payment_and_returns_the_operation(self):
        operation = self._operation()
        self.client_http.post(
            f'/bank/operations/{operation.pk}/apply/', {'order': self.order.pk})

        self.client_http.post(f'/bank/operations/{operation.pk}/reset/')

        operation.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(operation.status, 'new')
        self.assertIsNone(operation.payment)
        self.assertEqual(self.order.paid_amount, 0)

    def test_deleting_the_payment_frees_the_operation(self):
        """Иначе деньги в банке есть, по заказу их нет, и никто не заметит."""
        operation = self._operation()
        self.client_http.post(
            f'/bank/operations/{operation.pk}/apply/', {'order': self.order.pk})
        operation.refresh_from_db()

        operation.payment.delete()

        operation.refresh_from_db()
        self.assertEqual(operation.status, 'new')
        self.assertIsNone(operation.payment)

    def test_money_not_related_to_orders_can_be_put_aside(self):
        operation = self._operation(purpose='Перевод между своими счетами')

        self.client_http.post(f'/bank/operations/{operation.pk}/skip/')

        operation.refresh_from_db()
        self.assertEqual(operation.status, 'skipped')

    def test_an_applied_operation_is_not_skipped_silently(self):
        operation = self._operation()
        self.client_http.post(
            f'/bank/operations/{operation.pk}/apply/', {'order': self.order.pk})

        self.client_http.post(f'/bank/operations/{operation.pk}/skip/')

        operation.refresh_from_db()
        self.assertEqual(operation.status, 'applied')

    def test_the_list_shows_the_suggestion(self):
        self._operation()

        resp = self.client_http.get('/bank/operations/')

        self.assertContains(resp, self.order.order_number)
        self.assertContains(resp, 'ООО «ЛИФТПРОЕКТ»')

    def test_the_warehouse_does_not_see_the_bank(self):
        """Выписка по расчётному счёту — не то, что нужно кладовщику."""
        self.client_http.force_login(self.warehouse)

        resp = self.client_http.get('/bank/operations/')

        self.assertEqual(resp.status_code, 302)

    def test_the_dashboard_warns_about_unapplied_money(self):
        self._operation()

        resp = self.client_http.get('/')

        self.assertEqual(resp.context['unapplied_operations'], 1)

    def test_the_dashboard_stays_quiet_for_the_warehouse(self):
        """Кладовщик деньги не разносит — и знать о них ему незачем."""
        self._operation()
        self.client_http.force_login(self.warehouse)

        resp = self.client_http.get('/')

        self.assertFalse(resp.context['unapplied_operations'])


class TBankStatementCommandTests(TestCase):
    """Команда загрузки выписки."""

    PAYLOAD = {'operations': [
        {'id': 'op-1', 'typeOfOperation': 'Credit', 'amount': 14000,
         'operationDate': '2026-08-10', 'paymentPurpose': 'Оплата по счету 942',
         'payerName': 'ООО «ЛИФТПРОЕКТ»', 'payerInn': '9722051089',
         'documentNumber': '178'},
        {'id': 'op-2', 'typeOfOperation': 'Debit', 'amount': 500,
         'operationDate': '2026-08-11', 'paymentPurpose': 'Комиссия'},
    ]}

    def _run(self, **options):
        out = io.StringIO()
        with patch('core.tbank.get_statement', return_value=self.PAYLOAD):
            call_command('tbank_statement', stdout=out, stderr=out, **options)
        return out.getvalue()

    @override_settings(TBANK_TOKEN='secret', TBANK_ACCOUNT='123')
    def test_only_incoming_operations_are_stored(self):
        self._run()

        self.assertEqual(BankOperation.objects.count(), 1)
        operation = BankOperation.objects.get()
        self.assertEqual(operation.external_id, 'op-1')
        self.assertEqual(operation.amount, Decimal('14000'))
        self.assertEqual(operation.counterparty_inn, '9722051089')

    @override_settings(TBANK_TOKEN='secret', TBANK_ACCOUNT='123')
    def test_running_twice_does_not_duplicate_money(self):
        self._run()
        self._run()

        self.assertEqual(BankOperation.objects.count(), 1)

    @override_settings(TBANK_TOKEN='secret', TBANK_ACCOUNT='123')
    def test_dry_run_stores_nothing(self):
        output = self._run(dry_run=True)

        self.assertEqual(BankOperation.objects.count(), 0)
        self.assertIn('проверка', output)

    @override_settings(TBANK_TOKEN='')
    def test_without_a_token_the_command_says_so_and_stops(self):
        out = io.StringIO()
        call_command('tbank_statement', stdout=out)

        self.assertIn('не настроен', out.getvalue())
        self.assertEqual(BankOperation.objects.count(), 0)

    @override_settings(TBANK_TOKEN='secret', TBANK_ACCOUNT='123')
    def test_a_bank_failure_is_reported_without_a_traceback(self):
        out = io.StringIO()
        with patch('core.tbank.get_statement',
                   side_effect=tbank.TBankError('Т-Банк недоступен')):
            call_command('tbank_statement', stdout=out, stderr=out)

        self.assertIn('Выписка не получена', out.getvalue())


class TBankInvoiceBuildingTests(TestCase):
    """Сборка счёта: что именно уходит в банк."""

    def setUp(self):
        self.customer = ClientModel.objects.create(
            name='ООО «ЛИФТПРОЕКТ»', inn='9722051089', kpp='772201001',
            email='buh@liftproekt.ru')
        self.order = RepairOrder.objects.create(client=self.customer)
        model = EquipmentModel.objects.create(
            name='EkoDrive-2.2-1.0', kind='Устройство управления дверьми лифта')
        self.roe = RepairOrderEquipment.objects.create(
            repair_order=self.order,
            equipment=Equipment.objects.create(model=model, serial_number='13593'),
            work_performed='Ремонт импульсного блока питания,\nзамена транзисторов',
            repair_cost=Decimal('14000'),
        )

    def test_the_line_repeats_the_wording_used_in_hand_written_invoices(self):
        self.assertEqual(
            self.roe.invoice_line,
            'Ремонт Устройство управления дверьми лифта EkoDrive-2.2-1.0 SN:13593 '
            '(Ремонт импульсного блока питания, замена транзисторов)'
        )

    def test_line_breaks_do_not_leak_into_the_invoice(self):
        """Перенос строки разорвал бы ячейку таблицы в PDF банка."""
        self.assertNotIn('\n', self.roe.invoice_line)

    def test_equipment_without_a_price_is_not_a_line(self):
        RepairOrderEquipment.objects.create(
            repair_order=self.order,
            equipment=Equipment.objects.create(
                model=EquipmentModel.objects.create(name='Без цены'),
                serial_number='NOPRICE'),
        )

        items = self.order.invoice_items()

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['price'], 14000.0)

    @override_settings(TBANK_INVOICE_UNIT='шт.', TBANK_INVOICE_VAT='None')
    def test_items_carry_the_unit_and_the_vat_mode(self):
        item = self.order.invoice_items()[0]

        self.assertEqual(item['unit'], 'шт.')
        self.assertEqual(item['vat'], 'None')
        self.assertEqual(item['amount'], 1)

    @override_settings(TBANK_ACCOUNT='40802810700006096146')
    def test_the_payload_matches_the_documented_shape(self):
        payload = tbank.build_invoice(
            number='943',
            items=self.order.invoice_items(),
            payer={'name': self.customer.name, 'inn': self.customer.inn,
                   'kpp': self.customer.kpp},
            emails=['buh@liftproekt.ru'],
            invoice_date=datetime.date(2026, 8, 13),
            due_date=datetime.date(2026, 8, 27),
        )

        self.assertEqual(payload['invoiceNumber'], '943')
        self.assertEqual(payload['invoiceDate'], '2026-08-13')
        self.assertEqual(payload['dueDate'], '2026-08-27')
        self.assertEqual(payload['accountNumber'], '40802810700006096146')
        self.assertEqual(payload['payer'],
                         {'name': 'ООО «ЛИФТПРОЕКТ»', 'inn': '9722051089',
                          'kpp': '772201001'})
        self.assertEqual(payload['contacts'], [{'email': 'buh@liftproekt.ru'}])
        self.assertEqual(len(payload['items']), 1)

    def test_empty_payer_fields_are_not_sent(self):
        """Пустая строка в реквизитах счёта выглядит как ошибка."""
        payload = tbank.build_invoice(
            number='1', items=[{'name': 'x', 'price': 1, 'unit': 'шт.',
                                'vat': 'None', 'amount': 1}],
            payer={'name': 'ИП Петров', 'inn': '770000000000', 'kpp': ''},
        )

        self.assertEqual(payload['payer'], {'name': 'ИП Петров', 'inn': '770000000000'})

    def test_without_recipients_there_are_no_contacts(self):
        payload = tbank.build_invoice(
            number='1', items=[{'name': 'x'}], emails=[])

        self.assertNotIn('contacts', payload)


class TBankInvoiceNumberTests(TestCase):
    """Номер счёта. Банк его не выдаёт — он приходит от нас."""

    def setUp(self):
        self.customer = ClientModel.objects.create(name='ООО «Счётчик»')

    def _order(self, invoice_number=''):
        return RepairOrder.objects.create(
            client=self.customer, invoice_number=invoice_number)

    @override_settings(TBANK_INVOICE_NUMBER_START=1)
    def test_the_next_number_follows_the_biggest_one_seen(self):
        self._order('942')
        self._order('906')

        self.assertEqual(RepairOrder.next_invoice_number(), '943')

    @override_settings(TBANK_INVOICE_NUMBER_START=1)
    def test_non_numeric_numbers_are_not_guessed_from(self):
        """«942-01» и «б/н» в сквозной ряд не встают."""
        self._order('942-01')
        self._order('б/н')
        self._order('900')

        self.assertEqual(RepairOrder.next_invoice_number(), '901')

    @override_settings(TBANK_INVOICE_NUMBER_START=664)
    def test_the_starting_number_is_respected_on_an_empty_base(self):
        self.assertEqual(RepairOrder.next_invoice_number(), '664')

    @override_settings(TBANK_INVOICE_NUMBER_START=664)
    def test_the_start_does_not_pull_the_series_backwards(self):
        self._order('943')

        self.assertEqual(RepairOrder.next_invoice_number(), '944')


class TBankInvoiceSendingTests(TestCase):
    """Отправка счёта. Единственное, что программа создаёт в банке."""

    def setUp(self):
        self.accountant = Employee.objects.create_user(
            username='buh_inv', full_name='Бухгалтер', password='pass',
            role='accountant')
        self.manager = Employee.objects.create_user(
            username='mgr_inv', full_name='Менеджер', password='pass',
            role='repair_manager')
        self.client_http = TestClient()
        self.client_http.force_login(self.accountant)

        self.customer = ClientModel.objects.create(
            name='ООО «ЛИФТПРОЕКТ»', inn='9722051089', email='buh@liftproekt.ru')
        self.order = RepairOrder.objects.create(client=self.customer)
        model = EquipmentModel.objects.create(name='Emotron-счёт')
        RepairOrderEquipment.objects.create(
            repair_order=self.order,
            equipment=Equipment.objects.create(model=model, serial_number='SN-INV'),
            repair_cost=Decimal('54000'),
        )

    def _url(self):
        return f'/repair-orders/{self.order.pk}/invoice/'

    def _post(self, **overrides):
        data = {
            'invoice_number': '943',
            'invoice_date': '2026-08-13',
            'due_date': '2026-08-27',
            'emails': 'buh@liftproekt.ru',
        }
        data.update(overrides)
        return self.client_http.post(self._url(), data)

    @override_settings(TBANK_TOKEN='secret', TBANK_INVOICE_ENABLED=True)
    def test_sending_records_the_number_and_the_pdf(self):
        with patch('core.tbank.send_invoice',
                   return_value={'pdfUrl': 'https://example.org/1.pdf'}) as send:
            resp = self._post()

        self.assertRedirects(resp, f'/repair-orders/{self.order.pk}/')
        self.order.refresh_from_db()
        self.assertEqual(self.order.invoice_number, '943')
        self.assertEqual(self.order.invoice_date, datetime.date(2026, 8, 13))
        self.assertEqual(self.order.tbank_invoice_pdf_url, 'https://example.org/1.pdf')
        self.assertIsNotNone(self.order.tbank_invoice_sent_at)
        self.assertEqual(self.order.tbank_invoice_error, '')
        self.assertEqual(send.call_count, 1)

    @override_settings(TBANK_TOKEN='secret', TBANK_INVOICE_ENABLED=True)
    def test_what_is_sent_is_what_was_shown(self):
        with patch('core.tbank.send_invoice', return_value={}) as send:
            self._post()

        payload = send.call_args[0][0]
        self.assertEqual(payload['invoiceNumber'], '943')
        self.assertEqual(payload['payer']['inn'], '9722051089')
        self.assertEqual(payload['contacts'], [{'email': 'buh@liftproekt.ru'}])
        self.assertEqual(payload['items'][0]['price'], 54000.0)

    @override_settings(TBANK_TOKEN='secret', TBANK_INVOICE_ENABLED=True)
    def test_the_send_is_recorded_in_the_order_history(self):
        with patch('core.tbank.send_invoice', return_value={}):
            self._post()

        last = self.order.status_history.order_by('-id').first()
        self.assertIn('943', last.notes)
        self.assertEqual(last.changed_by, self.accountant)

    @override_settings(TBANK_TOKEN='secret', TBANK_INVOICE_ENABLED=True)
    def test_a_refusal_is_kept_and_nothing_is_marked_as_sent(self):
        with patch('core.tbank.send_invoice',
                   side_effect=tbank.TBankError('Т-Банк отказал: нет прав')):
            self._post()

        self.order.refresh_from_db()
        self.assertIn('нет прав', self.order.tbank_invoice_error)
        self.assertIsNone(self.order.tbank_invoice_sent_at)
        self.assertEqual(self.order.invoice_number, '')

    @override_settings(TBANK_TOKEN='secret', TBANK_INVOICE_ENABLED=False)
    def test_sending_is_off_by_default(self):
        """Отправка документа заказчику не должна включаться сама."""
        self.assertFalse(tbank.invoice_enabled())
        with self.assertRaises(tbank.TBankError) as caught:
            tbank.send_invoice({'invoiceNumber': '1', 'items': [{'name': 'x'}]})

        self.assertIn('выключено', str(caught.exception))

    @override_settings(TBANK_TOKEN='secret', TBANK_INVOICE_ENABLED=True)
    def test_an_invoice_without_lines_is_refused_before_the_network(self):
        with self.assertRaises(tbank.TBankError) as caught:
            tbank.send_invoice({'invoiceNumber': '1', 'items': []})

        self.assertIn('ни одной позиции', str(caught.exception))

    @override_settings(TBANK_TOKEN='secret', TBANK_INVOICE_ENABLED=True)
    def test_an_invoice_without_a_number_is_refused_before_the_network(self):
        with self.assertRaises(tbank.TBankError) as caught:
            tbank.send_invoice({'items': [{'name': 'x'}]})

        self.assertIn('номер счёта', str(caught.exception).lower())

    @override_settings(TBANK_TOKEN='secret', TBANK_INVOICE_ENABLED=True)
    def test_a_refusal_inside_a_200_response_is_still_a_refusal(self):
        def _fake(req, timeout=None):
            class _R:
                def __enter__(self_inner):
                    return self_inner

                def __exit__(self_inner, *args):
                    return False

                def read(self_inner):
                    return b'{"errorMessage": "Counterparty not found"}'
            return _R()

        with patch('core.tbank.request.urlopen', _fake):
            with self.assertRaises(tbank.TBankError) as caught:
                tbank.send_invoice({'invoiceNumber': '1', 'items': [{'name': 'x'}]})

        self.assertIn('Counterparty not found', str(caught.exception))

    @override_settings(TBANK_TOKEN='secret', TBANK_INVOICE_ENABLED=True)
    def test_the_request_is_a_post_with_json(self):
        calls = []

        def _fake(req, timeout=None):
            calls.append(req)

            class _R:
                def __enter__(self_inner):
                    return self_inner

                def __exit__(self_inner, *args):
                    return False

                def read(self_inner):
                    return b'{"pdfUrl": "https://example.org/x.pdf"}'
            return _R()

        with patch('core.tbank.request.urlopen', _fake):
            tbank.send_invoice({'invoiceNumber': '943', 'items': [{'name': 'Ремонт'}]})

        sent = calls[0]
        self.assertEqual(sent.method, 'POST')
        self.assertIn('/openapi/api/v1/invoice/send', sent.full_url)
        self.assertEqual(sent.get_header('Content-type'), 'application/json')
        self.assertEqual(sent.get_header('Authorization'), 'Bearer secret')
        # Кириллица уходит как есть, а не escape-последовательностями
        self.assertIn('Ремонт', sent.data.decode('utf-8'))

    @override_settings(TBANK_TOKEN='secret', TBANK_INVOICE_ENABLED=True)
    def test_a_broken_recipient_stops_the_whole_send(self):
        """Иначе счёт уйдёт не туда, а человек будет уверен, что отправил."""
        with patch('core.tbank.send_invoice') as send:
            resp = self._post(emails='buh@liftproekt.ru, кривой-адрес')

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(send.call_count, 0)
        self.assertContains(resp, 'Непохоже на адрес почты')

    @override_settings(TBANK_TOKEN='secret', TBANK_INVOICE_ENABLED=True)
    def test_a_due_date_before_the_invoice_date_is_refused(self):
        with patch('core.tbank.send_invoice') as send:
            resp = self._post(due_date='2026-08-01')

        self.assertEqual(send.call_count, 0)
        self.assertContains(resp, 'раньше даты счёта')

    @override_settings(TBANK_TOKEN='secret', TBANK_INVOICE_ENABLED=True)
    def test_the_page_shows_the_lines_and_the_total(self):
        resp = self.client_http.get(self._url())

        self.assertContains(resp, 'SN-INV')
        # Запятая, а не точка: локаль ru-ru
        self.assertContains(resp, '54000,00')
        self.assertContains(resp, 'ООО «ЛИФТПРОЕКТ»')

    @override_settings(TBANK_TOKEN='secret', TBANK_INVOICE_ENABLED=False)
    def test_the_page_says_why_the_button_is_dead(self):
        resp = self.client_http.get(self._url())

        self.assertContains(resp, 'TBANK_INVOICE_ENABLED')
        self.assertContains(resp, 'disabled')

    @override_settings(TBANK_TOKEN='secret', TBANK_INVOICE_ENABLED=True)
    def test_a_repeat_send_is_warned_about(self):
        self.order.tbank_invoice_sent_at = timezone.now()
        self.order.invoice_number = '943'
        self.order.save(update_fields=['tbank_invoice_sent_at', 'invoice_number'])

        resp = self.client_http.get(self._url())

        self.assertContains(resp, 'уже выставлен счёт')

    def test_the_repair_manager_does_not_issue_invoices(self):
        self.client_http.force_login(self.manager)

        resp = self.client_http.get(self._url())

        self.assertEqual(resp.status_code, 302)


class QuoteTests(TestCase):
    """Коммерческое предложение по заказу."""

    def setUp(self):
        self.admin = Employee.objects.create_superuser(
            username='admin_quote', full_name='Админ', password='pass')
        self.client_http = TestClient()
        self.client_http.force_login(self.admin)

        organization = Organization.get_solo()
        organization.name = 'ИП Петров П. П.'
        organization.inn = '772600000000'
        organization.ogrn = '324774600000000'
        organization.address = '117042, Москва, ул. Примерная, д. 1'
        organization.bank_name = 'АО «ТБанк»'
        organization.bank_bik = '044525974'
        organization.bank_account = '40802810700006096146'
        organization.corr_account = '30101810145250000974'
        organization.tax_note = 'Без НДС, применяется УСН, ПСН'
        organization.signatory_position = 'Индивидуальный предприниматель'
        organization.signatory_name = 'Петров П. П.'
        organization.save()

        self.customer = ClientModel.objects.create(
            name='ООО ГК «Промресурс»',
            address='305048, г. Курск, проспект Дружбы, д. 9А')
        self.order = RepairOrder.objects.create(client=self.customer)
        model = EquipmentModel.objects.create(
            name='EkoDrive-2.3-1.3', kind='Привод дверей')
        self.roe = RepairOrderEquipment.objects.create(
            repair_order=self.order,
            equipment=Equipment.objects.create(model=model, serial_number='44223'),
        )

    def _edit_url(self):
        return f'/repair-orders/{self.order.pk}/quote/edit/'

    def _print_url(self):
        return f'/repair-orders/{self.order.pk}/quote/'

    def _fill(self, **overrides):
        data = {
            'quote_subject': 'на ремонт приводов дверей EkoDrive-2.3-1.3',
            'quote_date': '2026-02-06',
            'quote_valid_until': '2026-02-20',
            'quote_lead_time': '3-14',
            'quote_payment_terms': '100% предоплата покупателем',
            'quote_delivery_terms': 'до склада Покупателя включена в Предложение',
            'order_equipments-TOTAL_FORMS': '1',
            'order_equipments-INITIAL_FORMS': '1',
            'order_equipments-MIN_NUM_FORMS': '0',
            'order_equipments-MAX_NUM_FORMS': '1000',
            'order_equipments-0-id': str(self.roe.pk),
            'order_equipments-0-proposed_work':
                'Ремонт импульсного блока питания,\nзамена транзисторов ВКО, ВКЗ, РЕВ',
            'order_equipments-0-repair_complexity': 'simple',
            'order_equipments-0-estimated_cost': '6000',
        }
        data.update(overrides)
        return self.client_http.post(self._edit_url(), data)

    def test_filling_the_quote_opens_it(self):
        resp = self._fill()

        self.assertRedirects(resp, self._print_url())
        self.order.refresh_from_db()
        self.roe.refresh_from_db()
        self.assertEqual(self.order.quote_date, datetime.date(2026, 2, 6))
        self.assertEqual(self.roe.estimated_cost, Decimal('6000'))
        self.assertEqual(self.roe.repair_complexity, 'simple')

    def test_the_quote_carries_the_letterhead_and_the_bank_details(self):
        self._fill()

        resp = self.client_http.get(self._print_url())

        self.assertContains(resp, 'ИП Петров П. П.')
        self.assertContains(resp, 'ОГРН/ОГРНИП 324774600000000')
        self.assertContains(resp, 'АО «ТБанк» БИК 044525974')
        self.assertContains(resp, '40802810700006096146')
        self.assertContains(resp, 'Без НДС, применяется УСН, ПСН')

    def test_the_quote_names_the_addressee_with_the_address(self):
        self._fill()

        resp = self.client_http.get(self._print_url())

        self.assertContains(resp, 'ООО ГК «Промресурс»')
        self.assertContains(resp, 'проспект Дружбы')

    def test_the_table_repeats_the_shape_of_their_own_quotes(self):
        self._fill()

        resp = self.client_http.get(self._print_url())

        self.assertContains(resp, 'Ремонт импульсного блока питания')
        self.assertContains(resp, '44223')
        self.assertContains(resp, 'Простой')
        self.assertContains(resp, '3-14')
        self.assertContains(resp, '100% предоплата покупателем')
        self.assertContains(resp, 'действительно до 20.02.2026')

    def test_line_breaks_do_not_break_the_table_cell(self):
        self.roe.proposed_work = 'Ремонт блока питания,\nзамена транзисторов'
        self.roe.save(update_fields=['proposed_work'])

        self.assertEqual(self.roe.quote_line,
                         'Ремонт блока питания, замена транзисторов')

    def test_proposed_work_is_not_the_same_as_performed_work(self):
        """Предложение пишут до согласия, выполненные работы — после ремонта."""
        self.roe.work_performed = 'Заменён блок питания'
        self.roe.proposed_work = 'Ремонт блока питания'
        self.roe.save(update_fields=['work_performed', 'proposed_work'])

        self.assertEqual(self.roe.quote_line, 'Ремонт блока питания')

    def test_without_proposed_work_the_performed_one_is_used(self):
        self.roe.work_performed = 'Заменён блок питания'
        self.roe.save(update_fields=['work_performed'])

        self.assertEqual(self.roe.quote_line, 'Заменён блок питания')

    def test_with_nothing_written_the_line_is_still_meaningful(self):
        self.assertEqual(self.roe.quote_line, 'Ремонт Привод дверей EkoDrive-2.3-1.3')

    def test_the_estimate_is_preferred_over_the_final_cost(self):
        """Предложение делают до ремонта: оценка ближе к разговору с заказчиком."""
        self.roe.repair_cost = Decimal('9000')
        self.roe.estimated_cost = Decimal('6000')
        self.roe.save(update_fields=['repair_cost', 'estimated_cost'])

        self.assertEqual(self.roe.quote_price, Decimal('6000'))

    def test_without_an_estimate_the_final_cost_is_used(self):
        self.roe.repair_cost = Decimal('9000')
        self.roe.save(update_fields=['repair_cost'])

        self.assertEqual(self.roe.quote_price, Decimal('9000'))

    def test_a_line_without_a_price_is_printed_with_a_dash_not_dropped(self):
        """Потерять строку молча хуже, чем напечатать её без суммы."""
        second = RepairOrderEquipment.objects.create(
            repair_order=self.order,
            equipment=Equipment.objects.create(
                model=EquipmentModel.objects.create(name='Без оценки'),
                serial_number='NOEST'),
        )
        self.roe.estimated_cost = Decimal('6000')
        self.roe.save(update_fields=['estimated_cost'])

        rows = self.order.quote_rows()

        self.assertEqual(len(rows), 2)
        self.assertIsNone(rows[1]['price'])
        self.assertEqual(second.quote_price, None)

    def test_the_total_skips_unpriced_lines(self):
        RepairOrderEquipment.objects.create(
            repair_order=self.order,
            equipment=Equipment.objects.create(
                model=EquipmentModel.objects.create(name='Без оценки 2'),
                serial_number='NOEST2'),
        )
        self.roe.estimated_cost = Decimal('6000')
        self.roe.save(update_fields=['estimated_cost'])

        self.assertEqual(self.order.quote_total, Decimal('6000'))

    def test_an_empty_quote_warns_instead_of_printing_a_blank(self):
        resp = self.client_http.get(self._print_url())

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Условия предложения не заполнены')

    def test_a_validity_date_before_the_quote_date_is_refused(self):
        resp = self._fill(quote_valid_until='2026-01-01')

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Срок действия раньше даты предложения')
        self.order.refresh_from_db()
        self.assertIsNone(self.order.quote_date)

    def test_the_order_card_leads_to_filling_while_the_quote_is_empty(self):
        resp = self.client_http.get(f'/repair-orders/{self.order.pk}/')

        self.assertContains(resp, self._edit_url())

    def test_a_filled_quote_is_linked_from_the_order_card(self):
        self._fill()

        resp = self.client_http.get(f'/repair-orders/{self.order.pk}/')

        self.assertContains(resp, self._print_url())

    def test_a_missing_client_address_is_pointed_out_before_printing(self):
        self.customer.address = ''
        self.customer.save(update_fields=['address'])

        resp = self.client_http.get(self._edit_url())

        self.assertContains(resp, 'не заполнен адрес')

    def test_the_client_form_keeps_the_address(self):
        resp = self.client_http.post('/clients/create/', {
            'name': 'ООО «Адресное»', 'inn': '', 'kpp': '',
            'address': '305048, г. Курск, проспект Дружбы, д. 9А',
            'contact_person': '', 'phone': '', 'email': '',
        }, follow=True)

        self.assertEqual(resp.status_code, 200)
        created = ClientModel.objects.get(name='ООО «Адресное»')
        self.assertEqual(created.address, '305048, г. Курск, проспект Дружбы, д. 9А')

    def test_the_organization_form_keeps_the_bank_details(self):
        resp = self.client_http.post('/management/organization/', {
            'name': 'ИП Петров П. П.', 'inn': '772600000000', 'kpp': '',
            'ogrn': '324774600000000', 'address': 'Москва', 'phone': '', 'email': '',
            'signatory_position': 'ИП', 'signatory_name': 'Петров П. П.',
            'bank_name': 'АО «ТБанк»', 'bank_bik': '044525974',
            'bank_account': '40802810700006096146',
            'corr_account': '30101810145250000974',
            'tax_note': 'Без НДС, применяется УСН',
        }, follow=True)

        self.assertEqual(resp.status_code, 200)
        organization = Organization.get_solo()
        self.assertEqual(organization.bank_bik, '044525974')
        self.assertEqual(organization.ogrn, '324774600000000')

    def test_the_bank_lines_are_empty_when_nothing_is_filled(self):
        Organization.objects.all().delete()

        self.assertEqual(Organization.get_solo().bank_lines, [])

    def test_the_quote_requires_login(self):
        resp = TestClient().get(self._print_url())

        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login/', resp['Location'])


class QrLinkLengthTests(TestCase):
    """Длина ссылки в QR. Этикетка — бумажка, чинить её после печати нечем."""

    def setUp(self):
        self.admin = Employee.objects.create_superuser(
            username='admin_qr', full_name='Админ', password='pass')
        self.client_http = TestClient()
        self.client_http.force_login(self.admin)

        self.part = SparePart.objects.create(
            part_number='QR-1', name='Резистор', current_stock=5)
        self.cell = StorageCell.objects.create(
            cabinet=Cabinet.objects.get_or_create(number=1)[0], row_number=1, cell_row=1)
        self.order = RepairOrder.objects.create(
            client=ClientModel.objects.create(name='ООО «QR»'))
        self.roe = RepairOrderEquipment.objects.create(
            repair_order=self.order,
            equipment=Equipment.objects.create(
                model=EquipmentModel.objects.create(name='QR-модель'),
                serial_number='QR-SN'),
        )

    def test_the_threshold_matches_the_qr_version_boundary(self):
        """43-й символ переводит код на 33 модуля — 2,3 точки на модуль."""
        self.assertEqual(views.QR_MAX_CHARS, 42)

    def test_a_short_link_says_nothing(self):
        self.assertEqual(views.qr_length_warning(['http://lifteam/p/1']), '')

    def test_a_link_exactly_at_the_limit_is_still_fine(self):
        self.assertEqual(views.qr_length_warning(['h' * 42]), '')

    def test_one_character_over_the_limit_warns(self):
        warning = views.qr_length_warning(['h' * 43])

        self.assertIn('43', warning)
        self.assertIn('LABEL_BASE_URL', warning)

    def test_the_longest_link_on_the_page_decides(self):
        """Длина растёт с номером записи, а не только от настройки."""
        warning = views.qr_length_warning(['короткая', 'h' * 50])

        self.assertIn('50', warning)

    def test_no_links_at_all_is_not_a_problem(self):
        self.assertEqual(views.qr_length_warning([]), '')

    @override_settings(LABEL_BASE_URL='http://lifteam.taile9b605.ts.net')
    def test_the_magic_dns_name_fits(self):
        """Имя длиннее адреса 100.x, но код от этого не растёт."""
        for url in (f'/parts/{self.part.pk}/label/',
                    f'/storage-cells/{self.cell.pk}/label/',
                    f'/repair-orders/{self.order.pk}/equipment/{self.roe.pk}/label/'):
            resp = self.client_http.get(url)

            self.assertEqual(resp.status_code, 200, url)
            self.assertEqual(resp.context['qr_warning'], '', url)

    @override_settings(LABEL_BASE_URL='http://' + 'x' * 60 + '.example.org')
    def test_an_overlong_base_warns_before_printing_not_after(self):
        resp = self.client_http.get(f'/parts/{self.part.pk}/label/')

        self.assertNotEqual(resp.context['qr_warning'], '')

    @override_settings(LABEL_BASE_URL='http://' + 'x' * 60 + '.example.org')
    def test_batch_pages_warn_too(self):
        for url in ('/parts/labels/?ids=%d' % self.part.pk,
                    '/storage-cells/labels/?cabinet=1'):
            resp = self.client_http.get(url)

            self.assertEqual(resp.status_code, 200, url)
            self.assertNotEqual(resp.context['qr_warning'], '', url)

    @override_settings(LABEL_BASE_URL='http://lifteam.taile9b605.ts.net')
    def test_the_link_in_the_code_points_at_the_name_not_the_address(self):
        resp = self.client_http.get(f'/parts/{self.part.pk}/label/')

        self.assertEqual(
            resp.context['qr_url'],
            f'http://lifteam.taile9b605.ts.net/p/{self.part.pk}')

    @override_settings(LABEL_BASE_URL='http://' + 'x' * 60 + '.example.org')
    def test_the_warning_is_not_printed_on_the_sticker(self):
        resp = self.client_http.get(f'/parts/{self.part.pk}/label/')

        content = resp.content.decode()
        self.assertIn('alert alert-warning no-print', content)


class QrLinkVisibilityTests(TestCase):
    """Куда ведёт код, должно быть видно на экране, а не только со сканером."""

    def setUp(self):
        self.admin = Employee.objects.create_superuser(
            username='admin_qrvis', full_name='Админ', password='pass')
        self.client_http = TestClient()
        self.client_http.force_login(self.admin)

        self.part = SparePart.objects.create(
            part_number='VIS-1', name='Диод', current_stock=5)
        self.cell = StorageCell.objects.create(
            cabinet=Cabinet.objects.get_or_create(number=2)[0], row_number=1, cell_row=1)
        self.order = RepairOrder.objects.create(
            client=ClientModel.objects.create(name='ООО «Видно»'))
        self.roe = RepairOrderEquipment.objects.create(
            repair_order=self.order,
            equipment=Equipment.objects.create(
                model=EquipmentModel.objects.create(name='VIS-модель'),
                serial_number='VIS-SN'),
        )

    @override_settings(LABEL_BASE_URL='http://lifteam.taile9b605.ts.net')
    def test_single_label_pages_show_the_exact_link(self):
        pages = {
            f'/parts/{self.part.pk}/label/':
                f'http://lifteam.taile9b605.ts.net/p/{self.part.pk}',
            f'/storage-cells/{self.cell.pk}/label/':
                f'http://lifteam.taile9b605.ts.net/c/{self.cell.pk}',
            f'/repair-orders/{self.order.pk}/equipment/{self.roe.pk}/label/':
                f'http://lifteam.taile9b605.ts.net/o/{self.order.pk}',
        }
        for url, expected in pages.items():
            resp = self.client_http.get(url)

            self.assertContains(resp, expected, msg_prefix=url)

    @override_settings(LABEL_BASE_URL='http://lifteam.taile9b605.ts.net')
    def test_batch_pages_show_the_base(self):
        for url in (f'/parts/labels/?ids={self.part.pk}',
                    '/storage-cells/labels/?cabinet=2'):
            resp = self.client_http.get(url)

            self.assertEqual(resp.context['qr_base'],
                             'http://lifteam.taile9b605.ts.net', url)
            self.assertContains(resp, 'Код ведёт на', msg_prefix=url)

    @override_settings(LABEL_BASE_URL='http://192.168.1.50')
    def test_a_local_address_left_in_env_is_visible_on_the_page(self):
        """Именно так это и ловится: настройка из .env перебивает умолчание."""
        resp = self.client_http.get(f'/parts/{self.part.pk}/label/')

        self.assertContains(resp, 'http://192.168.1.50/p/')

    @override_settings(LABEL_BASE_URL='http://lifteam.taile9b605.ts.net')
    def test_the_link_is_not_printed_on_the_sticker(self):
        resp = self.client_http.get(f'/parts/{self.part.pk}/label/')

        self.assertContains(resp, 'text-muted small no-print')


class CabinetTests(TestCase):
    """Кассетницы: своя раскладка у каждой, ряды разной ширины."""

    def setUp(self):
        self.warehouse = Employee.objects.create_user(
            username='sklad_cab', full_name='Кладовщик', password='pass',
            role='warehouse')
        self.manager = Employee.objects.create_user(
            username='mgr_cab', full_name='Менеджер', password='pass',
            role='repair_manager')
        self.client_http = TestClient()
        self.client_http.force_login(self.warehouse)

    def _create(self, **overrides):
        data = {'number': 1, 'name': 'Резисторы', 'note': '',
                'layout': '8, 8, 8, 4, 4'}
        data.update(overrides)
        return self.client_http.post('/storage-cells/cabinets/create/', data)

    def test_a_cabinet_gets_exactly_the_cells_of_its_layout(self):
        self._create()

        cabinet = Cabinet.objects.get(number=1)
        self.assertEqual(cabinet.layout(), [8, 8, 8, 4, 4])
        self.assertEqual(cabinet.cell_count, 32)

    def test_rows_may_differ_in_width(self):
        """Ряд из четырёх — это четыре крупных ящика, а не четыре из восьми."""
        self._create(layout='8, 2')

        cabinet = Cabinet.objects.get(number=1)
        self.assertEqual(cabinet.layout(), [8, 2])
        self.assertEqual(
            sorted(cabinet.cells.filter(row_number=2).values_list('cell_row', flat=True)),
            [1, 2])

    def test_the_layout_is_parsed_from_any_separator(self):
        for text in ('8,8,4', '8 8 4', '8, 8, 4', '8х8х4'):
            self.assertEqual(parse_layout(text), [8, 8, 4], text)

    def test_the_address_follows_the_cabinet_number(self):
        self._create(number=7)
        cell = Cabinet.objects.get(number=7).cells.get(row_number=1, cell_row=1)

        self.assertEqual(cell.address, 'К7-Р1-Я1')

    def test_the_next_number_is_suggested(self):
        self._create(number=3)

        self.assertEqual(Cabinet.next_number(), 4)

    def test_growing_the_layout_adds_cells_and_keeps_the_old_ones(self):
        self._create(layout='4')
        cabinet = Cabinet.objects.get(number=1)
        kept = cabinet.cells.get(row_number=1, cell_row=1)

        self.client_http.post(f'/storage-cells/cabinets/{cabinet.pk}/edit/', {
            'number': 1, 'name': 'Резисторы', 'note': '', 'layout': '4, 4'})

        self.assertEqual(cabinet.layout(), [4, 4])
        self.assertTrue(StorageCell.objects.filter(pk=kept.pk).exists())

    def test_shrinking_removes_empty_cells(self):
        self._create(layout='4, 4')
        cabinet = Cabinet.objects.get(number=1)

        self.client_http.post(f'/storage-cells/cabinets/{cabinet.pk}/edit/', {
            'number': 1, 'name': '', 'note': '', 'layout': '4'})

        self.assertEqual(cabinet.layout(), [4])
        self.assertEqual(cabinet.cell_count, 4)

    def test_shrinking_refuses_to_drop_cells_with_parts(self):
        """Иначе деталь на складе есть, а где она лежит — уже неизвестно."""
        self._create(layout='4, 4')
        cabinet = Cabinet.objects.get(number=1)
        part = SparePart.objects.create(part_number='CAB-1', name='Резистор',
                                        current_stock=10)
        cabinet.cells.get(row_number=2, cell_row=1).parts.add(part)

        resp = self.client_http.post(f'/storage-cells/cabinets/{cabinet.pk}/edit/', {
            'number': 1, 'name': '', 'note': '', 'layout': '4'})

        self.assertContains(resp, 'К1-Р2-Я1')
        self.assertEqual(cabinet.layout(), [4, 4])

    def test_an_empty_cabinet_can_be_deleted(self):
        self._create()
        cabinet = Cabinet.objects.get(number=1)

        self.client_http.post(f'/storage-cells/cabinets/{cabinet.pk}/delete/')

        self.assertFalse(Cabinet.objects.filter(pk=cabinet.pk).exists())
        self.assertEqual(StorageCell.objects.count(), 0)

    def test_a_cabinet_with_parts_is_not_deleted(self):
        self._create()
        cabinet = Cabinet.objects.get(number=1)
        part = SparePart.objects.create(part_number='CAB-2', name='Диод',
                                        current_stock=5)
        cabinet.cells.first().parts.add(part)

        self.client_http.post(f'/storage-cells/cabinets/{cabinet.pk}/delete/')

        self.assertTrue(Cabinet.objects.filter(pk=cabinet.pk).exists())

    def test_an_empty_layout_is_refused(self):
        resp = self._create(layout='')

        self.assertIn('layout', resp.context['form'].errors)
        self.assertEqual(Cabinet.objects.count(), 0)

    def test_too_many_cells_in_a_row_are_refused(self):
        resp = self._create(layout='100')

        self.assertIn('layout', resp.context['form'].errors)
        self.assertEqual(Cabinet.objects.count(), 0)

    def test_the_number_stays_unique(self):
        self._create(number=1)

        resp = self._create(number=1)

        self.assertEqual(Cabinet.objects.count(), 1)
        self.assertEqual(resp.status_code, 200)

    def test_the_grid_draws_rows_by_their_real_width(self):
        self._create(layout='8, 2')

        resp = self.client_http.get('/storage-cells/?cabinet=1')

        # Восемь в ряду — по 12,5% ширины, два — по 50%
        self.assertContains(resp, '12.5000%')
        self.assertContains(resp, '50.0000%')

    def test_the_width_is_written_with_a_dot_not_a_comma(self):
        """Локаль ru-ru печатает дробь через запятую, а «calc(12,5% - 4px)» —
        невалидный CSS: ячейки остались бы без ширины."""
        self._create(layout='8, 2')

        resp = self.client_http.get('/storage-cells/?cabinet=1')

        self.assertNotContains(resp, 'calc(12,5')

    def test_the_grid_survives_an_empty_base(self):
        """Свежая установка без кассетниц не должна падать."""
        resp = self.client_http.get('/storage-cells/')

        self.assertEqual(resp.status_code, 200)

    def test_the_grid_falls_back_to_the_first_cabinet(self):
        self._create(number=5)

        resp = self.client_http.get('/storage-cells/?cabinet=99')

        self.assertEqual(resp.context['cabinet'].number, 5)

    def test_the_repair_manager_does_not_rebuild_the_warehouse(self):
        self.client_http.force_login(self.manager)

        resp = self.client_http.get('/storage-cells/cabinets/')

        self.assertEqual(resp.status_code, 302)

    def test_the_list_shows_the_layout_and_what_is_occupied(self):
        self._create(layout='4, 4')
        cabinet = Cabinet.objects.get(number=1)
        part = SparePart.objects.create(part_number='CAB-3', name='Кондёр',
                                        current_stock=3)
        cabinet.cells.first().parts.add(part)

        resp = self.client_http.get('/storage-cells/cabinets/')

        self.assertContains(resp, '4, 4')
        self.assertContains(resp, 'Резисторы')

    def test_init_cells_creates_cabinets_with_the_given_layout(self):
        out = io.StringIO()
        call_command('init_cells', cabinets=2, layout='3,3', stdout=out)

        self.assertEqual(Cabinet.objects.count(), 2)
        self.assertEqual(Cabinet.objects.get(number=1).layout(), [3, 3])
        self.assertEqual(StorageCell.objects.count(), 12)

    def test_init_cells_does_not_recut_existing_cabinets(self):
        """Раскладку могли поменять руками — повторный запуск её не трогает."""
        self._create(number=1, layout='2')
        call_command('init_cells', cabinets=1, layout='8,8', stdout=io.StringIO())

        self.assertEqual(Cabinet.objects.get(number=1).layout(), [2])


class CommonLabelLayoutTests(TestCase):
    """Этикетка детали и этикетка ячейки печатаются одним шаблоном.

    Раньше разметок было две, и одна и та же деталь на пакете и на ячейке
    выглядела по-разному: где артикул, где название, адрес то сверху,
    то снизу.
    """

    def setUp(self):
        self.admin = Employee.objects.create_superuser(
            username='admin_common_label', full_name='Админ', password='pass'
        )
        self.client_http = TestClient()
        self.client_http.force_login(self.admin)

        self.cabinet = Cabinet.objects.create(number=7)
        self.cabinet.apply_layout([4])
        self.cell = self.cabinet.cells.first()

        self.part = SparePart.objects.create(
            part_number='CL-1', name='Резистор 10к', component_type='Резистор',
            package='0805', resistance=10, resistance_unit='кОм',
            description='Ставится в блок питания БУАД',
        )
        self.cell.parts.add(self.part)

    def _both_pages(self):
        return (
            f'/parts/{self.part.pk}/label/',
            f'/storage-cells/{self.cell.pk}/label/',
        )

    def test_both_labels_use_the_same_template(self):
        for page in self._both_pages():
            with self.subTest(page=page):
                response = self.client_http.get(page)
                self.assertTemplateUsed(response, 'core/_label_part.html')
                self.assertTemplateUsed(response, 'core/_label_part_styles.html')

    def test_a_single_part_prints_the_same_on_both(self):
        """Ячейка с одной деталью и пакет с ней же — одно и то же содержимое."""
        for page in self._both_pages():
            with self.subTest(page=page):
                response = self.client_http.get(page)
                self.assertEqual(response.context['title'], 'CL-1')
                self.assertEqual(response.context['package'], '0805')
                self.assertEqual(response.context['address'], self.cell.address)
                self.assertEqual(
                    response.context['description'], 'Ставится в блок питания БУАД'
                )

    def test_the_package_is_printed_below_and_stands_out(self):
        """По корпусу подбирают замену: 0805 вместо 1206 на плату не встанет."""
        for page in self._both_pages():
            with self.subTest(page=page):
                response = self.client_http.get(page)
                self.assertContains(response, 'label-package')
                self.assertContains(response, '0805')

    def test_the_specs_go_next_to_the_article(self):
        response = self.client_http.get(self._both_pages()[0])

        self.assertEqual(response.context['specs'], 'Резистор · 10кОм')

    def test_a_name_repeating_the_article_is_not_printed_twice(self):
        """Артикул уже стоит сверху и самым крупным шрифтом."""
        twin = SparePart.objects.create(part_number='CL-TWIN', name='CL-TWIN',
                                        component_type='Диод')

        response = self.client_http.get(f'/parts/{twin.pk}/label/')

        self.assertEqual(response.context['title'], 'CL-TWIN')
        self.assertEqual(response.context['description'], '')

    def test_a_part_without_a_cell_says_so(self):
        loose = SparePart.objects.create(part_number='CL-LOOSE', name='Ничья деталь')

        response = self.client_http.get(f'/parts/{loose.pk}/label/')

        self.assertEqual(response.context['address'], 'нет ячейки')

    def test_a_set_of_one_type_is_named_by_the_type(self):
        """Перечислять одинаковые названия на этикетке незачем — важны номиналы."""
        for number, value in (('CL-2', 4.7), ('CL-3', 1)):
            self.cell.parts.add(SparePart.objects.create(
                part_number=number, name=f'Резистор {value}к',
                component_type='Резистор', package='0805',
                resistance=value, resistance_unit='кОм',
            ))

        response = self.client_http.get(f'/storage-cells/{self.cell.pk}/label/')

        self.assertEqual(response.context['title'], 'Набор резисторов')
        self.assertEqual(response.context['items'], ['10кОм', '4.7кОм', '1кОм'])
        # Корпус общий на всю ячейку — он у всех один
        self.assertEqual(response.context['package'], '0805')

    def test_what_a_set_has_in_common_is_printed_once(self):
        """«0.125Вт» у каждого номинала — это шум, а место на этикетке жёсткое."""
        self.part.power, self.part.power_unit = 0.125, 'Вт'
        self.part.save(update_fields=['power', 'power_unit'])
        for number, value in (('CS-1', 4.7), ('CS-2', 22)):
            self.cell.parts.add(SparePart.objects.create(
                part_number=number, name=f'Резистор {value}к',
                component_type='Резистор', package='0805',
                resistance=value, resistance_unit='кОм',
                power=0.125, power_unit='Вт',
            ))

        response = self.client_http.get(f'/storage-cells/{self.cell.pk}/label/')

        self.assertEqual(response.context['specs'], '0.125Вт')
        self.assertEqual(response.context['items'], ['10кОм', '4.7кОм', '22кОм'])

    def test_values_of_two_kinds_stay_in_one_cell_of_the_grid(self):
        """У конденсатора различаются два числа сразу; сплошной строкой
        они слились бы в список, где не понять, что к чему относится."""
        self.cell.parts.clear()
        for number, farad, volt in (('CD-1', 47, 35), ('CD-2', 1000, 50)):
            self.cell.parts.add(SparePart.objects.create(
                part_number=number, name=f'Конденсатор {number}',
                component_type='Конденсатор',
                capacitance=farad, capacitance_unit='мкФ',
                voltage=volt, voltage_unit='В',
            ))

        response = self.client_http.get(f'/storage-cells/{self.cell.pk}/label/')

        self.assertEqual(response.context['items'], ['35В, 47мкФ', '50В, 1000мкФ'])

    def test_a_set_falls_back_to_articles_when_the_values_repeat(self):
        """У россыпи резисторов заполнена только мощность, а номинал живёт
        в артикуле: «2Вт, 2Вт, 2Вт» не различает ничего."""
        self.cell.parts.clear()
        for number in ('RS-1', 'RS-2', 'RS-3'):
            self.cell.parts.add(SparePart.objects.create(
                part_number=number, name=f'Резисторы {number}',
                component_type='Резистор', power=2, power_unit='Вт',
            ))

        response = self.client_http.get(f'/storage-cells/{self.cell.pk}/label/')

        self.assertEqual(response.context['specs'], '2Вт')
        self.assertEqual(response.context['items'], ['RS-1', 'RS-2', 'RS-3'])

    def test_a_mixed_set_keeps_the_common_package_out(self):
        """«0805» на ячейке, где половина деталей в 1206, хуже, чем ничего."""
        self.cell.parts.add(SparePart.objects.create(
            part_number='CL-4', name='Резистор 1к', component_type='Резистор',
            package='1206', resistance=1, resistance_unit='кОм',
        ))

        response = self.client_http.get(f'/storage-cells/{self.cell.pk}/label/')

        self.assertEqual(response.context['package'], '')

    def test_different_types_are_listed_by_article(self):
        self.cell.parts.add(SparePart.objects.create(
            part_number='CL-D', name='Диод', component_type='Диод',
        ))

        response = self.client_http.get(f'/storage-cells/{self.cell.pk}/label/')

        self.assertEqual(response.context['title'], 'Разные детали')
        self.assertEqual(response.context['items'], ['CL-1', 'CL-D'])

    def test_a_list_of_parts_is_printed_as_a_grid(self):
        """Сплошная строка переносилась посреди номинала: «10кОм, 4.7к»
        и «Ом» на следующей строке."""
        self.cell.parts.add(SparePart.objects.create(
            part_number='CG-1', name='Диод', component_type='Диод'))

        response = self.client_http.get(f'/storage-cells/{self.cell.pk}/label/')

        self.assertContains(response, 'class="label-items"')
        self.assertContains(response, '<span class="label-item">CL-1</span>', html=False)
        self.assertContains(response, '<span class="label-item">CG-1</span>', html=False)

    def test_a_single_part_has_no_grid(self):
        """У одной детали перечислять нечего — печатается описание."""
        response = self.client_http.get(f'/parts/{self.part.pk}/label/')

        self.assertNotContains(response, 'class="label-items"')
        self.assertContains(response, 'label-description')

    def test_an_empty_cell_says_it_is_empty(self):
        empty = self.cabinet.cells.last()

        response = self.client_http.get(f'/storage-cells/{empty.pk}/label/')

        self.assertEqual(response.context['title'], '')
        self.assertEqual(response.context['description'], 'Ячейка пуста')
        self.assertEqual(response.context['address'], empty.address)

    def test_every_label_carries_the_same_qr_size(self):
        """Размер кода один на всех этикетках — сканер подносят одинаково."""
        pages = self._both_pages() + (
            f'/parts/labels/?ids={self.part.pk}',
            f'/storage-cells/labels/?cabinet={self.cabinet.number}',
        )
        for page in pages:
            with self.subTest(page=page):
                self.assertContains(self.client_http.get(page), '12.3mm')

    def test_the_cell_label_fits_its_font(self):
        """Длинное описание должно ужиматься, а не обрезаться на полуслове."""
        response = self.client_http.get(f'/storage-cells/{self.cell.pk}/label/')

        self.assertContains(response, 'label-fit.js')


class PluralGenitiveTests(TestCase):
    """«Набор резисторов» — форму выводим правилами: список типов
    компонентов ведёт человек, готового справочника в программе нет."""

    def test_common_component_types(self):
        cases = {
            'Резистор': 'Резисторов',
            'Диод': 'Диодов',
            'Транзистор': 'Транзисторов',
            'Конденсатор': 'Конденсаторов',
            'Предохранитель': 'Предохранителей',
            'Микросхема': 'Микросхем',
            'Батарея': 'Батарей',
            'Реле': 'Реле',
        }
        for word, expected in cases.items():
            with self.subTest(word=word):
                self.assertEqual(plural_genitive(word), expected)

    def test_an_empty_type_gives_an_empty_string(self):
        self.assertEqual(plural_genitive(''), '')
        self.assertEqual(plural_genitive(None), '')


class SpecFormattingTests(TestCase):
    """Характеристики хранятся с шестью знаками после точки, и Django
    дописывает нули при каждой записи. «0.150000 А» читается как точность
    до микроампера, которой ни у кого нет."""

    def setUp(self):
        self.admin = Employee.objects.create_superuser(
            username='admin_spec', full_name='Админ', password='pass'
        )
        self.client_http = TestClient()
        self.client_http.force_login(self.admin)
        self.part = SparePart.objects.create(
            part_number='SP-1', name='Диод', component_type='Диод',
            current=Decimal('0.15'), current_unit='А',
            voltage=Decimal('100'), voltage_unit='В',
        )

    def test_format_spec_drops_the_trailing_zeros(self):
        cases = {
            Decimal('0.150000'): '0.15',
            Decimal('100.000000'): '100',
            Decimal('4700.000000'): '4700',
            Decimal('0.125000'): '0.125',
            Decimal('2.200000'): '2.2',
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(format_spec(value), expected)

    def test_format_spec_of_nothing_is_an_empty_string(self):
        self.assertEqual(format_spec(None), '')

    def test_the_part_card_shows_the_short_form(self):
        response = self.client_http.get(f'/parts/{self.part.pk}/')

        self.assertContains(response, '0.15')
        self.assertNotContains(response, '0.150000')

    def test_the_edit_form_shows_the_short_form(self):
        """Иначе при каждом сохранении в поле остаётся «0.150000»."""
        response = self.client_http.get(f'/parts/{self.part.pk}/edit/')

        self.assertContains(response, 'value="0.15"')
        self.assertNotContains(response, 'value="0.150000"')

    def test_the_label_shows_the_short_form(self):
        self.assertEqual(self.part.specs_display, '100В, 0.15А')


class ImportUnitTests(TestCase):
    """Единица измерения без значения — мусор: «Ом» у диода не значит ничего."""

    def setUp(self):
        self.admin = Employee.objects.create_superuser(
            username='admin_import_unit', full_name='Админ', password='pass'
        )
        self.client_http = TestClient()
        self.client_http.force_login(self.admin)

    def _import(self, rows, update_existing=False):
        wb = openpyxl.Workbook()
        ws = wb.active
        headers = ['part_number', 'name', 'component_type', 'voltage', 'voltage_unit',
                   'resistance', 'resistance_unit', 'capacitance', 'capacitance_unit']
        ws.append(headers)
        for row in rows:
            ws.append([row.get(header, '') for header in headers])
        buffer = io.BytesIO()
        wb.save(buffer)
        buffer.seek(0)
        buffer.name = 'parts.xlsx'
        data = {'file': buffer}
        if update_existing:
            data['update_existing'] = 'on'
        return self.client_http.post('/parts/import/', data, follow=True)

    def test_a_unit_without_a_value_is_not_imported(self):
        """В присланных файлах единицы проставлены во всех строках подряд,
        независимо от того, есть ли что измерять."""
        self._import([{
            'part_number': 'IU-1', 'name': 'Диод', 'component_type': 'Диод',
            'voltage': 100, 'voltage_unit': 'В',
            'resistance_unit': 'Ом', 'capacitance_unit': 'Ф',
        }])

        part = SparePart.objects.get(part_number='IU-1')
        self.assertEqual(part.voltage_unit, 'В')
        self.assertEqual(part.resistance_unit, '')
        self.assertEqual(part.capacitance_unit, '')

    def test_an_empty_unit_does_not_wipe_the_one_typed_by_hand(self):
        SparePart.objects.create(
            part_number='IU-2', name='Резистор', component_type='Резистор',
            resistance=Decimal('10'), resistance_unit='кОм',
        )

        self._import([{
            'part_number': 'IU-2', 'name': 'Резистор', 'component_type': 'Резистор',
            'resistance': 10,
        }], update_existing=True)

        self.assertEqual(SparePart.objects.get(part_number='IU-2').resistance_unit, 'кОм')


class PartBulkDeleteTests(TestCase):
    """Удаление отмеченных деталей списком: после загрузки каталога лишние
    позиции удаляют десятками."""

    def setUp(self):
        self.admin = Employee.objects.create_superuser(
            username='admin_bulk_del', full_name='Админ', password='pass'
        )
        self.client_http = TestClient()
        self.client_http.force_login(self.admin)
        self.parts = [
            SparePart.objects.create(part_number=f'BD-{index}', name=f'Деталь {index}',
                                     component_type='Диод', current_stock=index)
            for index in range(3)
        ]

    def _ids(self, parts):
        return '&'.join(f'ids={part.pk}' for part in parts)

    def test_the_page_lists_what_will_be_deleted(self):
        response = self.client_http.get(f'/parts/delete-selected/?{self._ids(self.parts[:2])}')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'BD-0')
        self.assertContains(response, 'BD-1')
        self.assertNotContains(response, 'BD-2')

    def test_nothing_is_deleted_by_opening_the_page(self):
        """GET только показывает — удаляет подтверждение."""
        self.client_http.get(f'/parts/delete-selected/?{self._ids(self.parts)}')

        self.assertEqual(SparePart.objects.count(), 3)

    def test_confirmation_deletes_the_selected_parts(self):
        response = self.client_http.post(
            '/parts/delete-selected/', {'ids': [self.parts[0].pk, self.parts[2].pk]}
        )

        self.assertRedirects(response, '/parts/')
        self.assertEqual(
            list(SparePart.objects.values_list('part_number', flat=True)), ['BD-1']
        )

    def test_the_page_warns_about_stock_and_orders(self):
        """Удаление уносит историю движений и записи о деталях в заказах."""
        order = RepairOrder.objects.create(
            client=ClientModel.objects.create(name='ООО Тест', inn='7700000123')
        )
        RepairOrderDetail.objects.create(
            repair_order=order, part=self.parts[1], quantity_used=2
        )

        response = self.client_http.get(f'/parts/delete-selected/?{self._ids(self.parts)}')

        self.assertEqual(response.context['in_orders'], [self.parts[1]])
        self.assertEqual(response.context['in_stock'], self.parts[1:])

    def test_an_empty_selection_deletes_nothing(self):
        response = self.client_http.get('/parts/delete-selected/')

        self.assertEqual(response.context['parts'], [])
        self.assertEqual(SparePart.objects.count(), 3)

    def test_the_list_has_the_button(self):
        response = self.client_http.get('/parts/')

        self.assertContains(response, 'formaction="/parts/delete-selected/"')

    def test_a_repair_manager_cannot_delete_parts(self):
        """Склад ведут кладовщики; удаление списком тем более не для всех."""
        manager = Employee.objects.create_user(
            username='manager_bulk', full_name='Мастер', password='pass', role='repair_manager'
        )
        client = TestClient()
        client.force_login(manager)

        response = client.post('/parts/delete-selected/', {'ids': [self.parts[0].pk]})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(SparePart.objects.count(), 3)


class RepairOrderLabelsBatchTests(TestCase):
    """Пачка этикеток заказов: печатается не на заказ, а на единицу
    оборудования внутри него — заказ с двумя единицами даёт две этикетки."""

    def setUp(self):
        self.admin = Employee.objects.create_superuser(
            username='admin_orders_lbl', full_name='Админ', password='pass'
        )
        self.client_http = TestClient()
        self.client_http.force_login(self.admin)

        client_obj = ClientModel.objects.create(name='ООО Дельта')
        model = EquipmentModel.objects.create(name='БУАД-5')

        self.order1 = RepairOrder.objects.create(client=client_obj)
        RepairOrderEquipment.objects.create(
            repair_order=self.order1,
            equipment=Equipment.objects.create(model=model, serial_number='SN-101'),
        )
        RepairOrderEquipment.objects.create(
            repair_order=self.order1,
            equipment=Equipment.objects.create(model=model, serial_number='SN-102'),
        )

        self.order2 = RepairOrder.objects.create(client=client_obj)
        RepairOrderEquipment.objects.create(
            repair_order=self.order2,
            equipment=Equipment.objects.create(model=model, serial_number='SN-201'),
        )

    def _ids(self, orders):
        return '&'.join(f'ids={order.pk}' for order in orders)

    def test_checked_orders_get_one_label_per_unit(self):
        response = self.client_http.get(f'/repair-orders/labels/?{self._ids([self.order1, self.order2])}')

        self.assertEqual(len(response.context['labels']), 3)

    def test_no_duplicate_or_missing_labels(self):
        response = self.client_http.get(f'/repair-orders/labels/?{self._ids([self.order1])}')
        serials = sorted(label['roe'].equipment.serial_number for label in response.context['labels'])

        self.assertEqual(serials, ['SN-101', 'SN-102'])

    def test_no_selection_falls_back_to_the_current_filter(self):
        """Без отметок — печать по тому же отбору, что и в списке (та же
        логика, что уже используется у этикеток деталей)."""
        response = self.client_http.get(f'/repair-orders/labels/?q={self.order2.order_number}')

        self.assertEqual(len(response.context['labels']), 1)
        self.assertEqual(response.context['labels'][0]['order'], self.order2)

    def test_positions_are_counted_per_order(self):
        response = self.client_http.get(f'/repair-orders/labels/?{self._ids([self.order1])}')
        positions = {
            label['roe'].equipment.serial_number: label['position']
            for label in response.context['labels']
        }

        self.assertEqual(positions['SN-101'], 1)
        self.assertEqual(positions['SN-102'], 2)

    def test_query_count_does_not_grow_with_selected_orders(self):
        """Больше отмеченных заказов не должно означать пропорционально
        больше обращений к базе — иначе полсотни этикеток тянули бы
        полсотню отдельных запросов вместо select_related/prefetch_related."""
        model = self.order1.order_equipments.first().equipment.model
        extra_order = RepairOrder.objects.create(client=self.order1.client)
        RepairOrderEquipment.objects.create(
            repair_order=extra_order,
            equipment=Equipment.objects.create(model=model, serial_number='SN-301'),
        )

        with CaptureQueriesContext(connection) as few:
            self.client_http.get(f'/repair-orders/labels/?{self._ids([self.order1])}')
        with CaptureQueriesContext(connection) as many:
            self.client_http.get(f'/repair-orders/labels/?{self._ids([self.order1, self.order2, extra_order])}')

        self.assertEqual(len(few.captured_queries), len(many.captured_queries))

    def test_the_list_has_the_buttons(self):
        response = self.client_http.get('/repair-orders/')

        self.assertContains(response, 'action="/repair-orders/labels/"')
        self.assertContains(response, 'formaction="/repair-orders/bulk-status/"')


@override_settings(NOTIFY_CLIENTS=True)
class RepairOrderBulkStatusTests(TestCase):
    """Массовая отгрузка отмеченных заказов — единственный переход,
    разрешённый массовой смене статуса: «Готов к отгрузке» → «Отгружен»."""

    def setUp(self):
        self.admin = Employee.objects.create_superuser(
            username='admin_bulk_status', full_name='Админ', password='pass'
        )
        self.client_http = TestClient()
        self.client_http.force_login(self.admin)

        client_obj = ClientModel.objects.create(name='ООО Эпсилон', email='eps@example.com')
        model = EquipmentModel.objects.create(name='БУАД-7')

        self.ready1 = RepairOrder.objects.create(client=client_obj, status='ready_for_shipment')
        RepairOrderEquipment.objects.create(
            repair_order=self.ready1,
            equipment=Equipment.objects.create(model=model, serial_number='SN-501'),
        )
        self.ready2 = RepairOrder.objects.create(client=client_obj, status='ready_for_shipment')
        RepairOrderEquipment.objects.create(
            repair_order=self.ready2,
            equipment=Equipment.objects.create(model=model, serial_number='SN-502'),
        )
        self.in_repair = RepairOrder.objects.create(client=client_obj, status='repair')

    def _ids(self, orders):
        return [order.pk for order in orders]

    def test_confirmation_page_changes_nothing(self):
        response = self.client_http.get(
            '/repair-orders/bulk-status/', {'ids': self._ids([self.ready1, self.in_repair])}
        )

        self.assertEqual(response.status_code, 200)
        self.ready1.refresh_from_db()
        self.in_repair.refresh_from_db()
        self.assertEqual(self.ready1.status, 'ready_for_shipment')
        self.assertEqual(self.in_repair.status, 'repair')
        self.assertEqual(response.context['eligible'], [self.ready1])
        self.assertEqual([row['order'] for row in response.context['ineligible']], [self.in_repair])

    def test_opening_the_confirmation_page_does_not_ship_anything(self):
        """Действие не должно выполняться без прохождения шага подтверждения."""
        self.client_http.get(
            '/repair-orders/bulk-status/', {'ids': self._ids([self.ready1, self.ready2])}
        )

        self.assertFalse(RepairOrder.objects.filter(status='shipped').exists())

    def test_post_ships_the_eligible_orders(self):
        response = self.client_http.post(
            '/repair-orders/bulk-status/', {'ids': self._ids([self.ready1, self.ready2])}
        )

        self.ready1.refresh_from_db()
        self.ready2.refresh_from_db()
        self.assertEqual(self.ready1.status, 'shipped')
        self.assertEqual(self.ready2.status, 'shipped')
        self.assertIsNotNone(self.ready1.shipping_date)
        self.assertIsNotNone(self.ready1.date_completed)
        self.assertEqual(response.context['applied'], [self.ready1, self.ready2])

    def test_each_shipped_order_gets_its_own_history_entry(self):
        self.client_http.post(
            '/repair-orders/bulk-status/', {'ids': self._ids([self.ready1, self.ready2])}
        )

        self.assertTrue(self.ready1.status_history.filter(status='shipped').exists())
        self.assertTrue(self.ready2.status_history.filter(status='shipped').exists())

    def test_each_shipped_order_is_notified(self):
        self.client_http.post(
            '/repair-orders/bulk-status/', {'ids': self._ids([self.ready1, self.ready2])}
        )

        self.assertEqual(
            Notification.objects.filter(
                event='order_status', repair_order__in=[self.ready1, self.ready2]
            ).count(),
            2,
        )

    def test_partial_failure_skips_ineligible_orders_with_a_reason(self):
        response = self.client_http.post(
            '/repair-orders/bulk-status/', {'ids': self._ids([self.ready1, self.in_repair])}
        )

        self.ready1.refresh_from_db()
        self.in_repair.refresh_from_db()
        self.assertEqual(self.ready1.status, 'shipped')
        self.assertEqual(self.in_repair.status, 'repair')
        self.assertEqual(response.context['applied'], [self.ready1])
        skipped = response.context['skipped']
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]['order'], self.in_repair)
        self.assertIn('Ремонт', skipped[0]['reason'])

    def test_an_empty_selection_ships_nothing(self):
        response = self.client_http.post('/repair-orders/bulk-status/', {})

        self.assertEqual(response.status_code, 302)
        self.assertFalse(RepairOrder.objects.filter(status='shipped').exists())

    def test_the_confirmation_page_lists_who_will_be_skipped_and_why(self):
        response = self.client_http.get(
            '/repair-orders/bulk-status/', {'ids': self._ids([self.ready1, self.in_repair])}
        )

        self.assertContains(response, self.in_repair.order_number)
        self.assertContains(response, 'Ремонт')


class ApplicationFieldTests(TestCase):
    """Применимость — отдельное поле, а не строка в описании: её печатают
    на этикетке своим местом и по ней ищут."""

    def setUp(self):
        self.admin = Employee.objects.create_superuser(
            username='admin_appl', full_name='Админ', password='pass'
        )
        self.client_http = TestClient()
        self.client_http.force_login(self.admin)
        self.cabinet = Cabinet.objects.create(number=9)
        self.cabinet.apply_layout([2])
        self.cell = self.cabinet.cells.first()
        self.part = SparePart.objects.create(
            part_number='AP-1', name='Оптопара', component_type='Оптрон',
            package='DIP-8', application='Otis',
        )
        self.cell.parts.add(self.part)

    def test_the_label_prints_it_between_the_package_and_the_address(self):
        for page in (f'/parts/{self.part.pk}/label/', f'/storage-cells/{self.cell.pk}/label/'):
            with self.subTest(page=page):
                response = self.client_http.get(page)
                self.assertEqual(response.context['application'], 'Otis')
                content = response.content.decode()
                self.assertLess(content.index('label-package"'), content.index('label-application"'))
                self.assertLess(content.index('label-application"'), content.index('label-address"'))

    def test_a_cell_shows_it_only_when_every_part_agrees(self):
        """«Otis» на ячейке, где половина деталей от ABB, вводит в заблуждение."""
        self.cell.parts.add(SparePart.objects.create(
            part_number='AP-2', name='Драйвер', component_type='Оптрон', application='ABB',
        ))

        response = self.client_http.get(f'/storage-cells/{self.cell.pk}/label/')

        self.assertEqual(response.context['application'], '')

    def test_the_card_and_the_form_show_it(self):
        card = self.client_http.get(f'/parts/{self.part.pk}/')
        form = self.client_http.get(f'/parts/{self.part.pk}/edit/')

        self.assertContains(card, 'Otis')
        self.assertContains(form, 'name="application"')
        self.assertContains(form, 'value="Otis"')

    def test_import_and_export_carry_the_field(self):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(['part_number', 'name', 'component_type', 'application'])
        ws.append(['AP-3', 'Реле', 'Реле', 'Altivar'])
        buffer = io.BytesIO()
        wb.save(buffer)
        buffer.seek(0)
        buffer.name = 'parts.xlsx'
        self.client_http.post('/parts/import/', {'file': buffer}, follow=True)

        self.assertEqual(SparePart.objects.get(part_number='AP-3').application, 'Altivar')

        export = self.client_http.get('/parts/export/')
        sheet = openpyxl.load_workbook(io.BytesIO(export.content)).active
        headers = [cell.value for cell in sheet[1]]
        self.assertIn('application', headers)
        values = [row[headers.index('application')] for row in sheet.iter_rows(min_row=2, values_only=True)]
        self.assertIn('Altivar', values)

    def test_the_migration_splits_it_out_of_the_description(self):
        """Так её записывали каталоги, из которых детали загружали:
        «Оптопара DIP-8 | Применение: Otis»."""
        from importlib import import_module

        from django.apps import apps as installed_apps

        migration = import_module('core.migrations.0023_spare_part_application')
        legacy = SparePart.objects.create(
            part_number='AP-OLD', name='Оптопара', component_type='Оптрон',
            description='Оптопара с логическим выходом | Применение: ABB',
        )

        migration.split_application(installed_apps, None)

        legacy.refresh_from_db()
        self.assertEqual(legacy.application, 'ABB')
        self.assertEqual(legacy.description, 'Оптопара с логическим выходом')


class TestRunnerTests(SimpleTestCase):
    """Быстрый хешер паролей — только для тестов и нигде больше."""

    def test_tests_run_with_the_fast_hasher(self):
        from core.test_runner import FAST_HASHER

        self.assertEqual(settings.PASSWORD_HASHERS, [FAST_HASHER])

    def test_the_project_settings_do_not_weaken_hashing(self):
        """Если ускорение однажды перенесут в настройки, пароли сотрудников
        станут храниться этим хешером по-настоящему."""
        from lifteam import settings as project_settings

        self.assertFalse(hasattr(project_settings, 'PASSWORD_HASHERS'))


class OrderEditLabelButtonTests(TestCase):
    """Этикетку печатают сразу после правки серийника или пломб —
    возвращаться для этого в карточку заказа незачем."""

    def setUp(self):
        self.admin = Employee.objects.create_superuser(
            username='admin_edit_label', full_name='Админ', password='pass'
        )
        self.client_http = TestClient()
        self.client_http.force_login(self.admin)

        model = EquipmentModel.objects.create(name='БУАД-3')
        self.order = RepairOrder.objects.create(
            client=ClientModel.objects.create(name='ООО Лифт', inn='7700000456')
        )
        self.roe = RepairOrderEquipment.objects.create(
            repair_order=self.order,
            equipment=Equipment.objects.create(model=model, serial_number='SN-EDIT-1'),
        )

    def test_the_edit_page_offers_the_label(self):
        response = self.client_http.get(f'/repair-orders/{self.order.pk}/edit/')

        self.assertContains(
            response,
            f'/repair-orders/{self.order.pk}/equipment/{self.roe.pk}/label/',
        )

    def test_a_new_order_has_nothing_to_print_yet(self):
        """У несохранённой единицы нет ни номера, ни ссылки для кода."""
        response = self.client_http.get('/repair-orders/create/')

        self.assertNotContains(response, '/label/')

    def test_the_label_itself_still_opens(self):
        response = self.client_http.get(
            f'/repair-orders/{self.order.pk}/equipment/{self.roe.pk}/label/'
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'SN-EDIT-1')


class NetworkErrorHintTests(TestCase):
    """«CERTIFICATE_VERIFY_FAILED» само по себе не говорит, что чинить."""

    def test_a_certificate_failure_names_the_missing_root(self):
        from core.net import explain

        reason = ('[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: '
                  'unable to get local issuer certificate (_ssl.c:1006)')

        result = explain(reason)

        self.assertIn('CERTIFICATE_VERIFY_FAILED', result)
        self.assertIn('НУЦ Минцифры', result)
        self.assertIn('DEPLOY.md', result)

    def test_other_reasons_are_left_alone(self):
        from core.net import explain

        self.assertEqual(explain('[Errno -3] Temporary failure in name resolution'),
                         '[Errno -3] Temporary failure in name resolution')

    @override_settings(MAX_BOT_TOKEN='secret-token')
    def test_the_messenger_error_carries_the_hint(self):
        """Текст попадает в очередь оповещений — там его и читают."""
        from urllib import error

        broken = error.URLError('[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed')
        with patch('core.messengers.request.urlopen', side_effect=broken):
            with self.assertRaises(messengers.MaxError) as caught:
                messengers.send_max_message('user:1', 'текст')

        self.assertIn('НУЦ Минцифры', str(caught.exception))

    @override_settings(TBANK_TOKEN='secret-token')
    def test_the_bank_error_carries_the_hint(self):
        from urllib import error

        from core import tbank

        broken = error.URLError('[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed')
        with patch('core.tbank.request.urlopen', side_effect=broken):
            with self.assertRaises(tbank.TBankError) as caught:
                tbank.get_accounts()

        self.assertIn('НУЦ Минцифры', str(caught.exception))


class DryRunOutputTests(TestCase):
    """`--dry-run` ничего не отправляет, и сказать об этом нужно словами:
    один раз это стоило вечера поисков причины, по которой «письма не уходят»."""

    def setUp(self):
        self.note = Notification.objects.create(
            event='low_stock', recipient='wh@example.com',
            subject='Дефицит', body='Текст',
        )

    @override_settings(NOTIFICATIONS_ENABLED=True,
                       EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
    def test_the_dry_run_says_that_nothing_was_sent(self):
        from django.core import mail

        out = io.StringIO()
        call_command('send_notifications', dry_run=True, stdout=out, stderr=io.StringIO())
        output = out.getvalue()

        self.assertIn('НИЧЕГО НЕ ОТПРАВЛЕНО', output)
        self.assertIn('без --dry-run', output)
        self.assertEqual(len(mail.outbox), 0)
        self.note.refresh_from_db()
        self.assertEqual(self.note.status, 'pending')


class FaultTemplateApplyTests(TestCase):
    """Типовая неисправность предлагает рецепт деталей — заказ дополняется
    им, а не переписывается заново."""

    def setUp(self):
        self.admin = Employee.objects.create_superuser(
            username='admin_faults', full_name='Админ', password='pass'
        )
        self.client_http = TestClient()
        self.client_http.force_login(self.admin)

        self.model_a = EquipmentModel.objects.create(name='БУАД-3')
        self.model_b = EquipmentModel.objects.create(name='ШУНЛ-2')

        self.part1 = SparePart.objects.create(part_number='FP-1', name='Транзистор', current_stock=10)
        self.part2 = SparePart.objects.create(part_number='FP-2', name='Резистор', current_stock=5)
        self.part3 = SparePart.objects.create(part_number='FP-3', name='Конденсатор', current_stock=2)

        self.fault1 = FaultType.objects.create(equipment_model=self.model_a, name='Не запускается')
        FaultTypePart.objects.create(fault_type=self.fault1, part=self.part1, quantity=2)
        FaultTypePart.objects.create(fault_type=self.fault1, part=self.part2, quantity=1)

        # part2 сознательно повторяется в обоих рецептах — проверка слияния
        self.fault2 = FaultType.objects.create(equipment_model=self.model_a, name='Шумит')
        FaultTypePart.objects.create(fault_type=self.fault2, part=self.part2, quantity=3)
        FaultTypePart.objects.create(fault_type=self.fault2, part=self.part3, quantity=1)

        self.fault_other_model = FaultType.objects.create(equipment_model=self.model_b, name='Неисправность другой модели')
        FaultTypePart.objects.create(fault_type=self.fault_other_model, part=self.part1, quantity=1)

        self.equipment = Equipment.objects.create(model=self.model_a, serial_number='SN-FAULT-1')
        self.order = RepairOrder.objects.create(
            client=ClientModel.objects.create(name='ООО Лифт', inn='7700000789')
        )
        self.roe = RepairOrderEquipment.objects.create(repair_order=self.order, equipment=self.equipment)

    def _apply(self, fault_ids):
        return self.client_http.post(
            f'/repair-orders/{self.order.pk}/apply-fault-template/',
            {'fault_ids': fault_ids},
        )

    def test_applying_one_fault_creates_its_recipe(self):
        response = self._apply([self.fault1.pk])
        data = response.json()

        self.assertTrue(data['success'])
        details = {d.part_id: d.quantity_used for d in self.order.details.all()}
        self.assertEqual(details, {self.part1.pk: 2, self.part2.pk: 1})

        self.part1.refresh_from_db()
        self.part2.refresh_from_db()
        self.assertEqual(self.part1.current_stock, 8)
        self.assertEqual(self.part2.current_stock, 4)

    def test_applying_adds_to_an_existing_manual_line_without_replacing_it(self):
        """Ручная строка по part1 уже есть — применение шаблона добавляет
        свою собственную, а не трогает и не сливается с существующей."""
        RepairOrderDetail.objects.create(repair_order=self.order, part=self.part1, quantity_used=5)

        self._apply([self.fault1.pk])

        details = list(self.order.details.all())
        self.assertEqual(len(details), 3)
        part1_lines = sorted(d.quantity_used for d in details if d.part_id == self.part1.pk)
        self.assertEqual(part1_lines, [2, 5])

    def test_several_faults_merge_the_same_part_into_one_line(self):
        """part2 встречается в обоих рецептах (1 и 3) — в заказе должна
        появиться одна строка на 4, а не две частичные."""
        response = self._apply([self.fault1.pk, self.fault2.pk])
        data = response.json()

        self.assertTrue(data['success'])
        details = list(self.order.details.all())
        part2_lines = [d for d in details if d.part_id == self.part2.pk]
        self.assertEqual(len(part2_lines), 1)
        self.assertEqual(part2_lines[0].quantity_used, 4)
        self.assertEqual(
            {d.part_id: d.quantity_used for d in details},
            {self.part1.pk: 2, self.part2.pk: 4, self.part3.pk: 1}
        )

    def test_a_shortage_is_reported_but_the_detail_is_still_added(self):
        fault3 = FaultType.objects.create(equipment_model=self.model_a, name='Сильный дефицит')
        FaultTypePart.objects.create(fault_type=fault3, part=self.part3, quantity=10)  # остаток всего 2

        response = self._apply([fault3.pk])
        data = response.json()

        self.assertTrue(data['success'])
        self.assertIn('не хватило', data['message'])
        self.part3.refresh_from_db()
        self.assertEqual(self.part3.current_stock, -8)
        self.assertEqual(self.order.details.get(part=self.part3).quantity_used, 10)

    def test_applying_nothing_fails_without_touching_the_order(self):
        response = self._apply([])
        data = response.json()

        self.assertFalse(data['success'])
        self.assertEqual(self.order.details.count(), 0)

    def test_the_order_form_lists_only_this_equipments_model_faults(self):
        response = self.client_http.get(f'/repair-orders/{self.order.pk}/edit/')

        self.assertContains(response, self.fault1.name)
        self.assertContains(response, self.fault2.name)
        self.assertNotContains(response, self.fault_other_model.name)

    def test_the_faults_ajax_endpoint_is_scoped_to_the_equipment_model(self):
        response = self.client_http.get(f'/ajax/equipment/{self.equipment.pk}/faults/')
        data = response.json()

        names = {f['name'] for f in data['faults']}
        self.assertEqual(names, {self.fault1.name, self.fault2.name})

    def test_the_equipment_form_rejects_a_fault_from_another_model(self):
        form = RepairOrderEquipmentForm(data={
            'equipment': self.equipment.pk,
            'fault_description': '',
            'faults': [self.fault_other_model.pk],
            'work_performed': '', 'seal_numbers': '', 'initial_condition': '',
            'repair_cost': '', 'yandex_disk_folder': '',
        })

        self.assertFalse(form.is_valid())
        self.assertIn('faults', form.errors)

    def test_the_other_option_is_not_a_database_choice(self):
        """«Другое» в списке — сигнал для интерфейса, а не id неисправности:
        его выбор не должен ронять валидацию формы."""
        form = RepairOrderEquipmentForm(data={
            'equipment': self.equipment.pk,
            'fault_description': 'своими словами, чего в справочнике ещё нет',
            'faults': ['other'],
            'work_performed': '', 'seal_numbers': '', 'initial_condition': '',
            'repair_cost': '', 'yandex_disk_folder': '',
        })

        self.assertTrue(form.is_valid())
        self.assertEqual(list(form.cleaned_data['faults']), [])


class FaultTypeAdminTests(TestCase):
    """Справочник типовых неисправностей — права те же, что у EquipmentModel:
    создание и правка открыты любому авторизованному, удаление — только
    складу и мастеру."""

    def setUp(self):
        self.model = EquipmentModel.objects.create(name='БУАД-9')
        self.part = SparePart.objects.create(part_number='FTP-1', name='Диод')
        self.fault = FaultType.objects.create(equipment_model=self.model, name='Проверочная неисправность')

    @staticmethod
    def _formset_data(prefix='parts', total=0):
        return {
            f'{prefix}-TOTAL_FORMS': str(total),
            f'{prefix}-INITIAL_FORMS': '0',
            f'{prefix}-MIN_NUM_FORMS': '0',
            f'{prefix}-MAX_NUM_FORMS': '1000',
        }

    def test_a_repair_manager_can_create_a_fault_type(self):
        manager = Employee.objects.create_user(
            username='manager_faults', full_name='Мастер', password='pass', role='repair_manager'
        )
        client = TestClient()
        client.force_login(manager)

        data = {'equipment_model': self.model.pk, 'name': 'Новая неисправность', 'description': ''}
        data.update(self._formset_data())
        response = client.post('/faults/create/', data)

        self.assertEqual(response.status_code, 302)
        self.assertTrue(FaultType.objects.filter(name='Новая неисправность').exists())

    def test_creating_a_fault_type_together_with_its_recipe(self):
        manager = Employee.objects.create_user(
            username='manager_recipe', full_name='Мастер', password='pass', role='repair_manager'
        )
        client = TestClient()
        client.force_login(manager)

        data = {'equipment_model': self.model.pk, 'name': 'С рецептом', 'description': ''}
        data.update(self._formset_data(total=1))
        data['parts-0-part'] = self.part.pk
        data['parts-0-quantity'] = '3'
        response = client.post('/faults/create/', data)

        self.assertEqual(response.status_code, 302)
        fault = FaultType.objects.get(name='С рецептом')
        self.assertEqual(fault.parts.count(), 1)
        self.assertEqual(fault.parts.first().quantity, 3)

    def test_an_accountant_cannot_delete_a_fault_type(self):
        accountant = Employee.objects.create_user(
            username='accountant_faults', full_name='Бухгалтер', password='pass', role='accountant'
        )
        client = TestClient()
        client.force_login(accountant)

        response = client.post(f'/faults/{self.fault.pk}/delete/')

        self.assertEqual(response.status_code, 302)
        self.assertTrue(FaultType.objects.filter(pk=self.fault.pk).exists())

    def test_warehouse_can_delete_a_fault_type(self):
        warehouse = Employee.objects.create_user(
            username='warehouse_faults', full_name='Кладовщик', password='pass', role='warehouse'
        )
        client = TestClient()
        client.force_login(warehouse)

        response = client.post(f'/faults/{self.fault.pk}/delete/')

        self.assertRedirects(response, '/faults/')
        self.assertFalse(FaultType.objects.filter(pk=self.fault.pk).exists())

    def test_an_anonymous_user_is_sent_to_login(self):
        client = TestClient()

        response = client.get('/faults/create/')

        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response.url)


class StockAllocationTests(TestCase):
    """Распределение расхода по партиям прихода (FIFO): каждый расход должен
    ссылаться на конкретную партию, из которой он физически списан."""

    def setUp(self):
        self.part = SparePart.objects.create(part_number='BATCH-1', name='Диод')

    def _incoming(self, quantity, price):
        return StockMovement.objects.create(
            part=self.part, quantity=quantity, movement_type='incoming', unit_price=price
        )

    def _outgoing(self, quantity):
        return StockMovement.objects.create(
            part=self.part, quantity=quantity, movement_type='outgoing'
        )

    def test_a_single_batch_covers_the_request(self):
        batch = self._incoming(5, Decimal('10'))
        outgoing = self._outgoing(3)

        fully_covered = StockAllocation.allocate(outgoing)

        self.assertTrue(fully_covered)
        allocations = list(outgoing.allocations.all())
        self.assertEqual(len(allocations), 1)
        self.assertEqual(allocations[0].incoming, batch)
        self.assertEqual(allocations[0].quantity, 3)
        self.assertEqual(batch.remaining_in_batch, 2)

    def test_a_request_bigger_than_the_oldest_batch_spills_into_the_next(self):
        """Запрошенное количество больше самой старой партии — распределяется
        по датам поступления, старейшая партия расходуется первой."""
        old_batch = self._incoming(5, Decimal('10'))
        new_batch = self._incoming(5, Decimal('20'))
        outgoing = self._outgoing(8)

        fully_covered = StockAllocation.allocate(outgoing)

        self.assertTrue(fully_covered)
        by_batch = {a.incoming_id: a.quantity for a in outgoing.allocations.all()}
        self.assertEqual(by_batch, {old_batch.pk: 5, new_batch.pk: 3})
        self.assertEqual(old_batch.remaining_in_batch, 0)
        self.assertEqual(new_batch.remaining_in_batch, 2)

    def test_insufficient_stock_across_all_batches_leaves_a_remainder(self):
        """Партий вместе не хватает — известная часть распределяется как
        обычно, а на недостачу партии не хватает ни одной."""
        batch = self._incoming(5, Decimal('10'))
        outgoing = self._outgoing(8)

        fully_covered = StockAllocation.allocate(outgoing)

        self.assertFalse(fully_covered)
        allocations = list(outgoing.allocations.all())
        self.assertEqual(len(allocations), 1)
        self.assertEqual(allocations[0].incoming, batch)
        self.assertEqual(allocations[0].quantity, 5)

    def test_a_later_call_in_the_same_transaction_does_not_see_the_batch_as_still_full(self):
        """Два расхода подряд (как при нескольких вызовах
        `_use_repair_order_part` в одной транзакции) не должны оба списаться
        с полного остатка партии — второй обязан увидеть то, что уже забрал
        первый."""
        batch = self._incoming(5, Decimal('10'))

        with transaction.atomic():
            first = self._outgoing(3)
            StockAllocation.allocate(first)
            second = self._outgoing(2)
            StockAllocation.allocate(second)

        self.assertEqual(batch.remaining_in_batch, 0)
        self.assertEqual(StockAllocation.objects.filter(incoming=batch).count(), 2)
        self.assertEqual(
            sorted(StockAllocation.objects.filter(incoming=batch).values_list('quantity', flat=True)),
            [2, 3],
        )


class OrderCostFromAllocationsTests(TestCase):
    """`_use_repair_order_part` заводит `OrderCost` (category='parts') по
    фактически задействованным партиям — не по средней/текущей цене."""

    def setUp(self):
        self.user = Employee.objects.create_user(
            username='wh_cost', full_name='Кладовщик', password='pass', role='warehouse'
        )
        self.client_http = TestClient()
        self.client_http.force_login(self.user)
        self.client_obj = ClientModel.objects.create(name='ООО Себестоимость')
        self.order = RepairOrder.objects.create(client=self.client_obj)
        self.part = SparePart.objects.create(part_number='COST-1', name='Стабилизатор')

    def _incoming(self, quantity, price):
        return StockMovement.objects.create(
            part=self.part, quantity=quantity, movement_type='incoming', unit_price=price
        )

    def _add_detail(self, quantity):
        return self.client_http.post(
            f'/repair-orders/{self.order.pk}/add-detail/',
            {'part': self.part.pk, 'quantity_used': quantity},
        )

    def test_cost_from_a_single_priced_batch(self):
        self._incoming(10, Decimal('50'))

        self._add_detail(4)

        cost = OrderCost.objects.get(repair_order=self.order, category='parts')
        self.assertEqual(cost.amount, Decimal('200'))

    def test_cost_split_across_two_differently_priced_batches(self):
        self._incoming(3, Decimal('50'))
        self._incoming(10, Decimal('80'))

        self._add_detail(5)

        cost = OrderCost.objects.get(repair_order=self.order, category='parts')
        # 3 шт. по 50 из первой партии + 2 шт. по 80 из второй
        self.assertEqual(cost.amount, Decimal('150') + Decimal('160'))

    def test_cost_is_unknown_when_a_batch_has_no_price(self):
        self._incoming(10, None)

        self._add_detail(4)

        cost = OrderCost.objects.get(repair_order=self.order, category='parts')
        self.assertIsNone(cost.amount)

    def test_cost_is_unknown_not_partial_when_stock_runs_short(self):
        """Недостаточно остатка — известная часть распределяется, но сумма
        затраты — None целиком, а не по покрытой части."""
        self._incoming(3, Decimal('50'))

        self._add_detail(5)  # спишется в минус, предупреждение уже проверено в других тестах

        cost = OrderCost.objects.get(repair_order=self.order, category='parts')
        self.assertIsNone(cost.amount)
        # Известная часть партий всё равно распределена
        self.assertEqual(StockAllocation.objects.filter(outgoing__repair_order=self.order).count(), 1)

    def test_repeated_calls_in_one_transaction_do_not_double_dip_a_batch(self):
        """Применение шаблона неисправности несколькими деталями подряд
        вызывает `_use_repair_order_part` несколько раз в одной транзакции —
        распределение по партиям должно учитывать уже списанное предыдущим
        вызовом, а не увидеть партию как ещё полную."""
        self._incoming(5, Decimal('10'))

        with transaction.atomic():
            views._use_repair_order_part(self.order, self.part, 3, self.user, 'первый вызов')
            views._use_repair_order_part(self.order, self.part, 2, self.user, 'второй вызов')

        costs = list(OrderCost.objects.filter(repair_order=self.order, category='parts'))
        self.assertEqual(len(costs), 2)
        self.assertEqual({c.amount for c in costs}, {Decimal('30'), Decimal('20')})

    def test_manual_stock_outgoing_allocates_but_creates_no_order_cost(self):
        """Ручное списание не привязано к заказу — партии распределяются
        (для будущей себестоимости), но `OrderCost` не заводится."""
        self._incoming(10, Decimal('50'))

        self.client_http.post(
            f'/parts/{self.part.pk}/stock-outgoing/',
            {'quantity': 4, 'reason': 'consumption', 'notes': '', 'document_number': ''},
        )

        movement = StockMovement.objects.get(part=self.part, movement_type='outgoing')
        self.assertEqual(movement.allocations.count(), 1)
        self.assertEqual(movement.allocations.first().quantity, 4)
        self.assertEqual(OrderCost.objects.count(), 0)


class RepairOrderPartsCostAndProfitTests(TestCase):
    """`RepairOrder.parts_cost` / `.profit` — прибыль = поступившие платежи
    минус себестоимость списанных деталей (не выставленная стоимость
    ремонта)."""

    def setUp(self):
        self.client_obj = ClientModel.objects.create(name='ООО Прибыль')
        self.order = RepairOrder.objects.create(client=self.client_obj)

    def test_an_order_with_no_cost_records_has_zero_parts_cost(self):
        self.assertEqual(self.order.parts_cost, Decimal('0'))
        self.assertEqual(self.order.profit, Decimal('0'))

    def test_parts_cost_sums_known_amounts(self):
        OrderCost.objects.create(repair_order=self.order, category='parts', amount=Decimal('100'))
        OrderCost.objects.create(repair_order=self.order, category='parts', amount=Decimal('50'))

        self.assertEqual(self.order.parts_cost, Decimal('150'))

    def test_a_single_unknown_cost_record_makes_the_whole_parts_cost_unknown(self):
        OrderCost.objects.create(repair_order=self.order, category='parts', amount=Decimal('100'))
        OrderCost.objects.create(repair_order=self.order, category='parts', amount=None)

        self.assertIsNone(self.order.parts_cost)
        self.assertIsNone(self.order.profit)

    def test_profit_is_payments_minus_parts_cost_not_the_quoted_repair_price(self):
        model = EquipmentModel.objects.create(name='БУАД-профит')
        RepairOrderEquipment.objects.create(
            repair_order=self.order,
            equipment=Equipment.objects.create(model=model, serial_number='SN-PROFIT'),
            repair_cost=Decimal('10000'),
        )
        Payment.objects.create(repair_order=self.order, amount=Decimal('4000'))
        Payment.objects.create(repair_order=self.order, amount=Decimal('1000'))
        OrderCost.objects.create(repair_order=self.order, category='parts', amount=Decimal('1200'))

        # Оплачено всего 5000 из выставленных 10000 — прибыль считается
        # именно от поступивших денег, а не от total_repair_cost
        self.assertEqual(self.order.paid_amount, Decimal('5000'))
        self.assertEqual(self.order.profit, Decimal('3800'))


class ProfitReportTests(TestCase):
    """Отчёт «Прибыль по заказам»: агрегаты за период и разбивка по
    заказчику, доступ только бухгалтерии."""

    def setUp(self):
        self.accountant = Employee.objects.create_user(
            username='buh_profit', full_name='Бухгалтер', password='pass', role='accountant'
        )
        self.warehouse = Employee.objects.create_user(
            username='wh_profit', full_name='Кладовщик', password='pass', role='warehouse'
        )
        self.client_http = TestClient()
        self.client_http.force_login(self.accountant)

        self.today = datetime.date.today()

        self.client_a = ClientModel.objects.create(name='ООО Альфа')
        self.client_b = ClientModel.objects.create(name='ООО Бета')
        self.order_a = RepairOrder.objects.create(client=self.client_a)
        self.order_b = RepairOrder.objects.create(client=self.client_b)

        Payment.objects.create(repair_order=self.order_a, amount=Decimal('5000'), payment_date=self.today)
        Payment.objects.create(repair_order=self.order_b, amount=Decimal('3000'), payment_date=self.today)
        OrderCost.objects.create(repair_order=self.order_a, category='parts', amount=Decimal('1200'))
        OrderCost.objects.create(repair_order=self.order_b, category='parts', amount=None)

    def test_role_without_accounting_access_is_denied(self):
        client = TestClient()
        client.force_login(self.warehouse)

        response = client.get('/reports/profit/', follow=True)

        self.assertRedirects(response, '/')

    def test_an_anonymous_user_is_sent_to_login(self):
        response = TestClient().get('/reports/profit/')

        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response.url)

    def test_totals_and_the_unknown_cost_count(self):
        response = self.client_http.get('/reports/profit/')

        self.assertEqual(response.context['revenue_total'], Decimal('8000'))
        self.assertEqual(response.context['known_cost_total'], Decimal('1200'))
        self.assertEqual(response.context['unknown_cost_count'], 1)
        self.assertEqual(response.context['profit_total'], Decimal('6800'))

    def test_breakdown_by_client(self):
        response = self.client_http.get('/reports/profit/')

        rows = {row['client']: row for row in response.context['rows']}
        self.assertEqual(rows['ООО Альфа']['revenue'], Decimal('5000'))
        self.assertEqual(rows['ООО Альфа']['profit'], Decimal('3800'))
        self.assertEqual(rows['ООО Бета']['revenue'], Decimal('3000'))
        self.assertEqual(rows['ООО Бета']['unknown_cost_count'], 1)
        # Известная часть себестоимости у Беты — 0, платёж известен полностью
        self.assertEqual(rows['ООО Бета']['profit'], Decimal('3000'))

    def test_a_period_outside_the_range_is_excluded(self):
        old_date = self.today - datetime.timedelta(days=90)
        Payment.objects.create(repair_order=self.order_a, amount=Decimal('9999'), payment_date=old_date)

        response = self.client_http.get('/reports/profit/')

        # 90 дней назад — за пределами окна по умолчанию (30 дней)
        self.assertEqual(response.context['revenue_total'], Decimal('8000'))

    def test_export_is_accountant_only(self):
        client = TestClient()
        client.force_login(self.warehouse)

        response = client.get('/reports/profit/export/', follow=True)

        self.assertRedirects(response, '/')

    def test_export_produces_a_workbook_with_two_sheets(self):
        response = self.client_http.get('/reports/profit/export/')

        self.assertEqual(response.status_code, 200)
        wb = openpyxl.load_workbook(io.BytesIO(response.content))
        self.assertEqual(wb.sheetnames, ['Итог за период', 'По заказчикам'])


class InventorySessionTests(TestCase):
    """Инвентаризация кассетницы: старт по одной кассетнице (в проекте нет
    понятия «весь склад» или «зона»), ввод посчитанного, применение
    расхождений. Недостача — обычное списание, распределённое по партиям
    FIFO (`StockAllocation.allocate`), как и любой другой расход в проекте
    — ручного выбора партии под недостачу сознательно нет. Избыток —
    приход без цены (находка — не покупка) с обязательным комментарием."""

    def setUp(self):
        self.employee = Employee.objects.create_user(
            username='wh_inv', full_name='Кладовщик', password='pass', role='warehouse'
        )
        self.client_http = TestClient()
        self.client_http.force_login(self.employee)

        self.cabinet = Cabinet.objects.create(number=901, name='Инвентаризация')
        self.cabinet.apply_layout([3])
        self.cell_a, self.cell_b, self.cell_c = list(self.cabinet.cells.order_by('cell_row'))

        # current_stock=10/5 соответствует одной партии прихода на каждую —
        # так expected_quantity на старте сессии совпадает с тем, что реально
        # можно списать/распределить по партиям
        self.part_a = SparePart.objects.create(
            part_number='INV-A', name='Деталь A', current_stock=10, min_stock=0)
        self.part_b = SparePart.objects.create(
            part_number='INV-B', name='Деталь B', current_stock=5, min_stock=0)
        self.cell_a.parts.add(self.part_a)
        self.cell_b.parts.add(self.part_b)
        # cell_c намеренно остаётся пустой — деталей в ней нет, строки инвентаризации быть не должно

        StockMovement.objects.create(
            part=self.part_a, quantity=10, movement_type='incoming', unit_price=Decimal('3.00'))
        StockMovement.objects.create(
            part=self.part_b, quantity=5, movement_type='incoming', unit_price=Decimal('7.00'))

    def _start(self, follow=True):
        response = self.client_http.post(f'/inventory/start/{self.cabinet.pk}/', follow=follow)
        session = InventorySession.objects.filter(cabinet=self.cabinet, status='in_progress').first()
        return session, response

    def _count(self, session, counted):
        data = {f'counted_{line.pk}': str(qty) for line, qty in counted}
        return self.client_http.post(f'/inventory/{session.pk}/count/', data, follow=True)

    def _confirm(self, session, comments=None):
        data = {}
        if comments:
            for line, text in comments.items():
                data[f'comment_{line.pk}'] = text
        return self.client_http.post(f'/inventory/{session.pk}/confirm/', data, follow=True)

    # --- старт сессии ---

    def test_anonymous_user_is_redirected_to_login(self):
        response = TestClient().get('/inventory/')

        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response.url)

    def test_start_creates_lines_only_for_parts_placed_in_a_cell(self):
        session, response = self._start()

        self.assertRedirects(response, f'/inventory/{session.pk}/count/')
        parts_in_lines = set(session.lines.values_list('part__part_number', flat=True))
        self.assertEqual(parts_in_lines, {'INV-A', 'INV-B'})

    def test_expected_quantity_is_a_snapshot_of_current_stock_at_start(self):
        session, _ = self._start()

        line_a = session.lines.get(part=self.part_a)
        self.assertEqual(line_a.expected_quantity, 10)

    def test_starting_again_redirects_to_the_already_running_session(self):
        """Не даём открыть вторую параллельную сессию на ту же кассетницу —
        ведём к уже идущей."""
        first_session, _ = self._start()

        response = self.client_http.post(f'/inventory/start/{self.cabinet.pk}/', follow=False)

        self.assertEqual(InventorySession.objects.filter(cabinet=self.cabinet).count(), 1)
        self.assertRedirects(response, f'/inventory/{first_session.pk}/count/')

    # --- расхождение как число ---

    def test_discrepancy_is_counted_minus_expected(self):
        session, _ = self._start()
        line_a = session.lines.get(part=self.part_a)

        line_a.counted_quantity = 7
        self.assertEqual(line_a.discrepancy, -3)

        line_a.counted_quantity = 13
        self.assertEqual(line_a.discrepancy, 3)

        line_a.counted_quantity = None
        self.assertIsNone(line_a.discrepancy)

    # --- применение недостачи ---

    def test_deficit_allocates_from_the_oldest_batch_and_reflects_its_price(self):
        """Недостача списывается как обычный расход и уходит по FIFO —
        сначала со старейшей партии; себестоимость расхождения — это
        себестоимость именно задействованной партии."""
        part = SparePart.objects.create(part_number='INV-FIFO', name='Деталь FIFO', current_stock=8, min_stock=0)
        self.cell_c.parts.add(part)
        old_batch = StockMovement.objects.create(
            part=part, quantity=5, movement_type='incoming', unit_price=Decimal('10'))
        StockMovement.objects.create(
            part=part, quantity=3, movement_type='incoming', unit_price=Decimal('20'))

        session, _ = self._start()
        line = session.lines.get(part=part)
        self._count(session, [(line, 3)])  # посчитано 3 при учтённых 8 → недостача 5

        response = self._confirm(session)
        self.assertEqual(response.status_code, 200)

        part.refresh_from_db()
        line.refresh_from_db()
        self.assertEqual(part.current_stock, 3)
        movement = line.movement
        self.assertIsNotNone(movement)
        self.assertEqual(movement.movement_type, 'outgoing')
        self.assertEqual(movement.quantity, 5)
        self.assertIn('Инвентаризация', movement.notes)

        allocations = {a.incoming_id: a.quantity for a in movement.allocations.all()}
        self.assertEqual(allocations, {old_batch.pk: 5})  # только старая партия, новую не трогает
        self.assertEqual(views._cost_from_allocations(movement), Decimal('50'))  # 5 шт. по цене старой партии

    def test_deficit_does_not_require_a_comment(self):
        session, _ = self._start()
        line_a = session.lines.get(part=self.part_a)
        self._count(session, [(line_a, 6)])  # недостача 4

        response = self._confirm(session)  # без комментария

        self.assertEqual(response.status_code, 200)
        session.refresh_from_db()
        self.assertEqual(session.status, InventorySession.STATUS_COMPLETED)
        line_a.refresh_from_db()
        self.assertIsNotNone(line_a.movement)

    # --- применение избытка ---

    def test_surplus_creates_incoming_movement_without_price_and_does_not_touch_part_price(self):
        self.part_b.price = Decimal('7.00')
        self.part_b.save(update_fields=['price'])

        session, _ = self._start()
        line_b = session.lines.get(part=self.part_b)
        self._count(session, [(line_b, 8)])  # излишек 3

        response = self._confirm(session, comments={line_b: 'Найден лишний пакет на полке'})
        self.assertEqual(response.status_code, 200)

        self.part_b.refresh_from_db()
        line_b.refresh_from_db()
        self.assertEqual(self.part_b.current_stock, 8)
        self.assertEqual(self.part_b.price, Decimal('7.00'))  # цену не тронули — находка не покупка
        movement = line_b.movement
        self.assertEqual(movement.movement_type, 'incoming')
        self.assertIsNone(movement.unit_price)
        self.assertIn('Найден лишний пакет на полке', movement.notes)
        self.assertIn('Инвентаризация', movement.notes)

    def test_surplus_without_a_comment_blocks_the_whole_apply(self):
        """Избыток без причины не проходит — и не проходит целиком: даже
        строка с недостачей в той же сессии не применяется, пока не
        заполнен комментарий у строки с избытком."""
        session, _ = self._start()
        line_a = session.lines.get(part=self.part_a)
        line_b = session.lines.get(part=self.part_b)
        self._count(session, [(line_a, 6), (line_b, 8)])  # a: недостача 4, b: излишек 3

        response = self._confirm(session)  # комментарий для b не передан

        self.assertEqual(response.status_code, 200)
        session.refresh_from_db()
        self.assertEqual(session.status, InventorySession.STATUS_IN_PROGRESS)
        line_a.refresh_from_db()
        line_b.refresh_from_db()
        self.assertIsNone(line_a.movement)
        self.assertIsNone(line_b.movement)
        self.part_a.refresh_from_db()
        self.part_b.refresh_from_db()
        self.assertEqual(self.part_a.current_stock, 10)
        self.assertEqual(self.part_b.current_stock, 5)
        self.assertFalse(StockMovement.objects.filter(notes__icontains='Инвентаризация').exists())

        # Заполнив комментарий, ту же сессию можно применить следующим запросом
        ok_response = self._confirm(session, comments={line_b: 'Пересортица при прошлой поставке'})
        self.assertEqual(ok_response.status_code, 200)
        session.refresh_from_db()
        self.assertEqual(session.status, InventorySession.STATUS_COMPLETED)

    # --- расхождение против живого остатка, а не снимка на старте (снос по времени) ---

    def test_apply_uses_live_stock_at_confirm_time_not_the_stale_start_snapshot(self):
        """Между стартом сессии и подтверждением остаток мог измениться
        другим движением (второй сотрудник на складе). Показанное и
        применённое расхождение должно быть против ЖИВОГО остатка —
        после применения current_stock равен ровно посчитанному, а не
        тому, что предполагал снимок на начало сессии."""
        session, _ = self._start()
        line_a = session.lines.get(part=self.part_a)
        self.assertEqual(line_a.expected_quantity, 10)

        self._count(session, [(line_a, 10)])  # сотрудник вводит число, совпадающее со снимком

        # Пока считали, кто-то успел списать 2 шт. этой же детали в заказ —
        # против снимка (10) расхождения нет, но против живого остатка (8) есть
        self.part_a.current_stock = 8
        self.part_a.save(update_fields=['current_stock'])

        confirm_page = self.client_http.get(f'/inventory/{session.pk}/confirm/')
        row = next(r for r in confirm_page.context['rows'] if r['line'].pk == line_a.pk)
        self.assertTrue(row['drifted'])
        self.assertEqual(row['discrepancy'], 2)  # 10 (посчитано) − 8 (живой остаток)

        blocked = self._confirm(session)  # излишек против живого остатка, комментария ещё нет
        session.refresh_from_db()
        self.assertEqual(session.status, InventorySession.STATUS_IN_PROGRESS)

        applied = self._confirm(session, comments={line_a: 'Расхождение из-за параллельного списания'})
        self.assertEqual(applied.status_code, 200)

        self.part_a.refresh_from_db()
        line_a.refresh_from_db()
        session.refresh_from_db()
        self.assertEqual(self.part_a.current_stock, 10)  # ровно посчитанное
        self.assertEqual(session.status, InventorySession.STATUS_COMPLETED)
        self.assertEqual(line_a.movement.movement_type, 'incoming')
        self.assertEqual(line_a.movement.quantity, 2)  # разница против живого (8), а не против снимка (10)

    # --- непосчитанные строки и завершение сессии ---

    def test_uncounted_lines_stay_untouched_and_session_still_completes(self):
        session, _ = self._start()
        line_a = session.lines.get(part=self.part_a)
        line_b = session.lines.get(part=self.part_b)
        self._count(session, [(line_a, 10)])  # только A посчитана, расхождения нет; B не тронута

        response = self._confirm(session)

        self.assertEqual(response.status_code, 200)
        session.refresh_from_db()
        self.assertEqual(session.status, InventorySession.STATUS_COMPLETED)
        line_b.refresh_from_db()
        self.assertIsNone(line_b.counted_quantity)
        self.assertIsNone(line_b.movement)
        self.part_b.refresh_from_db()
        self.assertEqual(self.part_b.current_stock, 5)

    # --- удаление черновика ---

    def test_draft_session_can_be_deleted_before_anything_is_applied(self):
        session, _ = self._start()

        response = self.client_http.post(f'/inventory/{session.pk}/delete/', follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(InventorySession.objects.filter(pk=session.pk).exists())

    def test_session_with_applied_movements_cannot_be_deleted(self):
        session, _ = self._start()
        line_a = session.lines.get(part=self.part_a)
        self._count(session, [(line_a, 6)])
        self._confirm(session)
        session.refresh_from_db()
        self.assertEqual(session.status, InventorySession.STATUS_COMPLETED)

        response = self.client_http.post(f'/inventory/{session.pk}/delete/', follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(InventorySession.objects.filter(pk=session.pk).exists())

    # --- история ---

    def test_history_list_reports_surplus_and_deficit_line_counts(self):
        session, _ = self._start()
        line_a = session.lines.get(part=self.part_a)
        line_b = session.lines.get(part=self.part_b)
        self._count(session, [(line_a, 6), (line_b, 8)])
        self._confirm(session, comments={line_b: 'Излишек при пересчёте'})

        response = self.client_http.get('/inventory/')

        self.assertEqual(response.status_code, 200)
        row = next(s for s in response.context['sessions'] if s.pk == session.pk)
        self.assertEqual(row.deficit_count, 1)
        self.assertEqual(row.surplus_count, 1)
