"""
Восстановление базы данных SQLite из резервной копии.

Перед заменой текущая база сохраняется рядом с меткой времени, поэтому
ошибочное восстановление всегда можно откатить.
"""
import gzip
import shutil
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Восстанавливает базу SQLite из копии (сервер должен быть остановлен)'

    def add_arguments(self, parser):
        parser.add_argument(
            'backup', nargs='?', default=None,
            help='Путь к копии. Если не указан — берётся самая свежая из backups/',
        )
        parser.add_argument(
            '--list', action='store_true',
            help='Показать доступные копии и выйти',
        )
        parser.add_argument(
            '--yes', action='store_true',
            help='Не запрашивать подтверждение (для скриптов)',
        )

    def handle(self, *args, **options):
        db_settings = settings.DATABASES['default']
        if 'sqlite3' not in db_settings['ENGINE']:
            raise CommandError('Команда рассчитана на SQLite.')

        backup_dir = Path(settings.BASE_DIR) / 'backups'
        available = sorted(backup_dir.glob('db_*.sqlite3*')) if backup_dir.exists() else []

        if options['list']:
            if not available:
                self.stdout.write('Копий не найдено.')
                return
            self.stdout.write('Доступные копии:')
            for path in available:
                size_kb = path.stat().st_size / 1024
                self.stdout.write(f'  {path.name}  ({size_kb:.0f} КБ)')
            return

        if options['backup']:
            backup_path = Path(options['backup'])
        elif available:
            backup_path = available[-1]
            self.stdout.write(f'Выбрана самая свежая копия: {backup_path.name}')
        else:
            raise CommandError('Копии не найдены. Укажите путь явно или проверьте каталог backups/')

        if not backup_path.exists():
            raise CommandError(f'Файл не найден: {backup_path}')

        # Распаковываем, если копия сжата
        temp_file = None
        if backup_path.suffix == '.gz':
            temp_file = tempfile.NamedTemporaryFile(suffix='.sqlite3', delete=False)
            temp_file.close()
            with gzip.open(backup_path, 'rb') as f_in, open(temp_file.name, 'wb') as f_out:
                shutil.copyfileobj(f_in, f_out)
            source_path = Path(temp_file.name)
        else:
            source_path = backup_path

        try:
            # Проверяем копию до того, как трогать рабочую базу
            check = sqlite3.connect(f'file:{source_path}?mode=ro', uri=True)
            try:
                integrity = check.execute('PRAGMA integrity_check;').fetchone()[0]
                if integrity != 'ok':
                    raise CommandError(f'Копия повреждена, восстановление отменено: {integrity}')
                tables = check.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table';"
                ).fetchone()[0]
            except sqlite3.DatabaseError as exc:
                raise CommandError(
                    f'Файл не является базой SQLite или повреждён, '
                    f'восстановление отменено: {exc}'
                )
            finally:
                check.close()

            target_path = Path(db_settings['NAME'])
            self.stdout.write(
                f'Копия: {backup_path.name} (таблиц: {tables}, integrity_check: ok)\n'
                f'Будет заменена рабочая база: {target_path}'
            )

            if not options['yes']:
                answer = input('Продолжить? Введите "yes" для подтверждения: ')
                if answer.strip().lower() != 'yes':
                    self.stdout.write('Отменено.')
                    return

            # Текущую базу не удаляем, а отставляем в сторону — на случай отката
            if target_path.exists():
                stamp = datetime.now().strftime('%Y-%m-%d_%H%M%S')
                replaced_path = target_path.with_name(f'{target_path.name}.replaced_{stamp}')
                shutil.move(str(target_path), str(replaced_path))
                self.stdout.write(f'Прежняя база сохранена: {replaced_path.name}')

            # WAL и shm от старой базы удаляем, иначе они не соответствуют новому файлу
            for suffix in ('-wal', '-shm'):
                stale = target_path.with_name(target_path.name + suffix)
                if stale.exists():
                    stale.unlink()

            shutil.copy2(source_path, target_path)
            self.stdout.write(self.style.SUCCESS(f'База восстановлена из {backup_path.name}'))
        finally:
            if temp_file:
                Path(temp_file.name).unlink(missing_ok=True)
