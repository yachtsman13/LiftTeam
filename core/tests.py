"""
Тесты для LiftTeam. Покрывают наиболее рискованную бизнес-логику:
генерацию номера заказа, движение склада, ячейки с несколькими деталями,
импорт/экспорт Excel, права доступа по ролям, маршрутизацию,
настройку SQLite и резервное копирование.
"""
import datetime
import io
import json
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import openpyxl
from django.conf import settings
from django.core.management import call_command
from django.db import connection
from django.test import TestCase, override_settings
from django.test import Client as TestClient
from django.urls import resolve
from django.utils import timezone

from .models import (
    Client as ClientModel, Employee, Equipment, EquipmentModel, RepairOrder,
    RepairOrderDetail, RepairOrderEquipment, SparePart, StockMovement, StorageCell,
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
        self.assertContains(response, 'Заказ не сохранён')

    def test_invalid_yandex_link_reports_error_and_creates_nothing(self):
        response = self._post(**{'equipments-0-yandex_disk_folder': 'папка на диске'})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(RepairOrder.objects.count(), 0)
        self.assertContains(response, 'Заказ не сохранён')


class StorageCellMultiPartTests(TestCase):
    def setUp(self):
        self.admin = Employee.objects.create_superuser(username='admin_t', full_name='Админ', password='pass')
        self.part1 = SparePart.objects.create(part_number='CELL-1', name='Деталь A')
        self.part2 = SparePart.objects.create(part_number='CELL-2', name='Деталь B')
        self.cell1 = StorageCell.objects.create(cabinet_number=1, row_number=1, cell_row=1)
        self.cell2 = StorageCell.objects.create(cabinet_number=1, row_number=1, cell_row=2)
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
        self.cell = StorageCell.objects.create(cabinet_number=2, row_number=3, cell_row=4)
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
            f'/storage-cells/?cabinet={self.cell.cabinet_number}&open_cell={self.cell.pk}',
        )

    def test_short_urls_require_login(self):
        anonymous = TestClient()
        response = anonymous.get(f'/p/{self.part.pk}/')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response['Location'])

    def test_label_base_url_setting_overrides_request_host(self):
        """Печать через Tailscale не должна класть в QR адрес,
        недоступный сканирующему телефону в офисе."""
        from core.views import label_base_url

        request = TestClient().get('/').wsgi_request
        with self.settings(LABEL_BASE_URL='http://192.168.31.169/'):
            self.assertEqual(label_base_url(request), 'http://192.168.31.169')


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

        self.assertEqual([r['Артикул'] for r in rows], ['LOW-1'])
        self.assertEqual(rows[0]['Не хватает'], 4)

    def test_purchase_plan_export_shows_cell_address(self):
        part = SparePart.objects.create(part_number='LOW-2', name='Деталь', current_stock=0, min_stock=3)
        cell = StorageCell.objects.create(cabinet_number=1, row_number=2, cell_row=3)
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
        self.assertEqual(float(total_row['Сумма, ₽']), 4000.0)

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


class EquipmentLabelLinkTests(TestCase):
    """Этикетка оборудования: в QR ссылка вместо JSON.

    Раньше в код клали {"id":…,"model":"БУАД","serial":…} — сканирование
    давало строку с кириллицей, которую всё равно искали руками, а сам код
    из-за кириллицы распухал.
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

    def test_label_encodes_short_link(self):
        with patch('core.views.generate_qr_image') as qr:
            qr.return_value = 'data:image/png;base64,x'
            self.client_http.get(f'/equipment/{self.equipment.pk}/label/')

        encoded = qr.call_args[0][0]
        self.assertTrue(encoded.endswith(f'/e/{self.equipment.pk}/'), encoded)
        self.assertNotIn('{', encoded)
        self.assertNotIn('БУАД', encoded)

    @override_settings(LABEL_BASE_URL='http://192.168.1.50')
    def test_label_uses_configured_base_url(self):
        """Печать через Tailscale, сканирование в офисе: адрес берётся
        из настройки, а не из того, как открыта страница печати."""
        with patch('core.views.generate_qr_image') as qr:
            qr.return_value = 'data:image/png;base64,x'
            self.client_http.get(f'/equipment/{self.equipment.pk}/label/')

        self.assertEqual(
            qr.call_args[0][0], f'http://192.168.1.50/e/{self.equipment.pk}/'
        )

    def test_label_still_shows_barcode_of_serial(self):
        """Штрихкод серийника остаётся — его читает сканер с клавиатурным вводом."""
        with patch('core.views.generate_barcode_image') as barcode:
            barcode.return_value = 'data:image/png;base64,x'
            resp = self.client_http.get(f'/equipment/{self.equipment.pk}/label/')

        self.assertEqual(resp.status_code, 200)
        barcode.assert_called_once_with('БУАД-1234')


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
        self.assertTrue(encoded.endswith(f'/o/{self.order.pk}/'), encoded)
        # Прежнее содержимое кода — «LT-2026-08-001/1» — больше не годится:
        # сканирование им ничего не открывало
        self.assertNotIn(self.order.order_number, encoded)

    @override_settings(LABEL_BASE_URL='http://192.168.1.50')
    def test_label_uses_configured_base_url(self):
        with patch('core.views.generate_qr_image') as qr:
            qr.return_value = 'data:image/png;base64,x'
            self.client_http.get(self._label_url())

        self.assertEqual(qr.call_args[0][0], f'http://192.168.1.50/o/{self.order.pk}/')

    def test_position_still_printed_on_the_label(self):
        """Позиция в ссылку не входит, поэтому она обязана остаться на бумаге —
        в строке с номером заказа, вида «LT-2026-08-001/1»."""
        resp = self.client_http.get(self._label_url())
        content = resp.content.decode()

        self.assertContains(resp, self.order.order_number)
        self.assertIn('/1</span>', content)

    def test_logo_has_no_inner_circle(self):
        """У логотипа осталась одна окружность: внутренняя поджимала QR."""
        content = self.client_http.get(self._label_url()).content.decode()
        logo = content[content.index('<svg class="label-logo"'):content.index('</svg>')]

        self.assertEqual(logo.count('<circle'), 1)


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
            cabinet_number=1, row_number=1, cell_row=StorageCell.objects.count() + 1
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

        self.filled = StorageCell.objects.create(cabinet_number=1, row_number=1, cell_row=1)
        self.filled.parts.add(self.resistor)
        self.empty = StorageCell.objects.create(cabinet_number=1, row_number=1, cell_row=2)
        StorageCell.objects.create(cabinet_number=2, row_number=1, cell_row=1)

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
        self.assertTrue(labels[0]['grouped'])
        self.assertEqual(labels[0]['group_type'], 'Резистор')

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
