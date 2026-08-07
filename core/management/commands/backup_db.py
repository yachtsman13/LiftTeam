"""
Резервное копирование базы данных SQLite.

Использует официальный backup API SQLite, а не копирование файла: копия
снимается целостной даже при работающем сервере и активных транзакциях.
Обычный `cp` в режиме WAL даёт повреждённый файл, о чём выясняется
в худший момент — когда копия понадобилась.
"""
import gzip
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Создаёт целостную резервную копию базы SQLite (безопасно на работающем сервере)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--output', '-o', default=None,
            help='Каталог для копий (по умолчанию BASE_DIR/backups)',
        )
        parser.add_argument(
            '--keep', type=int, default=30,
            help='Сколько последних копий хранить; 0 — не удалять старые (по умолчанию 30)',
        )
        parser.add_argument(
            '--gzip', action='store_true',
            help='Сжать копию (для больших баз; на маленьких выигрыш невелик)',
        )

    def handle(self, *args, **options):
        db_settings = settings.DATABASES['default']
        if 'sqlite3' not in db_settings['ENGINE']:
            raise CommandError(
                'Команда рассчитана на SQLite. Текущий движок: '
                f'{db_settings["ENGINE"]}. Для PostgreSQL используйте pg_dump.'
            )

        source_path = Path(db_settings['NAME'])
        if not source_path.exists():
            raise CommandError(f'Файл базы не найден: {source_path}')

        backup_dir = Path(options['output']) if options['output'] else Path(settings.BASE_DIR) / 'backups'
        backup_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime('%Y-%m-%d_%H%M%S')
        target_path = backup_dir / f'db_{timestamp}.sqlite3'
        # Две копии в пределах одной секунды не должны молча затирать друг друга
        counter = 1
        while target_path.exists() or target_path.with_suffix('.sqlite3.gz').exists():
            target_path = backup_dir / f'db_{timestamp}_{counter}.sqlite3'
            counter += 1

        # backup API сам снимает согласованный срез, включая незакоммиченный
        # в основной файл WAL, — отдельный wal_checkpoint не нужен
        source = sqlite3.connect(str(source_path))
        try:
            target = sqlite3.connect(str(target_path))
            try:
                source.backup(target)
            finally:
                target.close()
        except sqlite3.DatabaseError as exc:
            target_path.unlink(missing_ok=True)
            raise CommandError(f'Не удалось прочитать базу {source_path}: {exc}')
        finally:
            source.close()

        # Проверяем, что копия читается и не повреждена — бэкап без проверки
        # это не бэкап, а надежда
        check = sqlite3.connect(str(target_path))
        try:
            result = check.execute('PRAGMA integrity_check;').fetchone()[0]
        except sqlite3.DatabaseError as exc:
            target_path.unlink(missing_ok=True)
            raise CommandError(f'Копия нечитаема, удалена: {exc}')
        finally:
            check.close()

        if result != 'ok':
            target_path.unlink(missing_ok=True)
            raise CommandError(f'Копия повреждена, удалена. integrity_check: {result}')

        if options['gzip']:
            gz_path = target_path.with_suffix(target_path.suffix + '.gz')
            with open(target_path, 'rb') as f_in, gzip.open(gz_path, 'wb') as f_out:
                shutil.copyfileobj(f_in, f_out)
            target_path.unlink()
            target_path = gz_path

        size_kb = target_path.stat().st_size / 1024
        self.stdout.write(self.style.SUCCESS(
            f'Копия создана: {target_path} ({size_kb:.0f} КБ, integrity_check: ok)'
        ))

        removed = self._prune(backup_dir, options['keep'])
        if removed:
            self.stdout.write(f'Удалено старых копий: {removed}')

    def _prune(self, backup_dir, keep):
        """Удаляет самые старые копии, оставляя keep последних.

        Служебные файлы SQLite (-wal, -shm) копиями не считаются: иначе одна
        копия занимала бы до трёх мест в квоте хранения.
        """
        if keep <= 0:
            return 0
        backups = sorted(
            [
                p for p in backup_dir.glob('db_*.sqlite3*')
                if p.is_file() and not p.name.endswith(('-wal', '-shm'))
            ],
            key=lambda p: p.name,
        )
        stale = backups[:-keep] if len(backups) > keep else []
        for path in stale:
            path.unlink()
            # заодно убираем осиротевшие служебные файлы этой копии
            for suffix in ('-wal', '-shm'):
                sidecar = path.with_name(path.name + suffix)
                if sidecar.exists():
                    sidecar.unlink()
        return len(stale)
