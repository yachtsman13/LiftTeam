"""
Тесты для LiftTeam. Покрывают наиболее рискованную бизнес-логику:
генерацию номера заказа, движение склада, ячейки с несколькими деталями,
импорт/экспорт Excel, права доступа по ролям и маршрутизацию.
"""
import io
from unittest.mock import patch

import openpyxl
from django.test import TestCase
from django.test import Client as TestClient
from django.urls import resolve

from .models import (
    Client as ClientModel, Employee, RepairOrder, SparePart, StorageCell,
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

        self.assertRegex(order1.order_number, r'^LT-\d{4}-\d{2}-\d{5}$')
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


class UrlRoutingTests(TestCase):
    def test_management_users_not_shadowed_by_django_admin(self):
        """Регрессия: /admin/users/ раньше перехватывался встроенной админкой Django
        (path('admin/', admin.site.urls) match'ился раньше core.urls)."""
        match = resolve('/management/users/')
        self.assertEqual(match.func.__name__, 'admin_users')
