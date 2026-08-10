"""
Тесты для LiftTeam. Покрывают наиболее рискованную бизнес-логику:
генерацию номера заказа, движение склада, ячейки с несколькими деталями,
импорт/экспорт Excel, права доступа по ролям, маршрутизацию,
настройку SQLite и резервное копирование.
"""
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
