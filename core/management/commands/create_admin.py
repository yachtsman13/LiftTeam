"""
Management command: create_admin
LiftTeam v2.49.0
"""
from django.core.management.base import BaseCommand
from core.models import Employee


class Command(BaseCommand):
    help = 'Создание администратора системы'

    def add_arguments(self, parser):
        parser.add_argument('--username', type=str, required=True, help='Логин администратора')
        parser.add_argument('--name', type=str, required=True, help='ФИО администратора')
        parser.add_argument('--password', type=str, required=True, help='Пароль')
        parser.add_argument('--force', action='store_true', help='Пересоздать если существует')

    def handle(self, *args, **options):
        username = options['username']
        name = options['name']
        password = options['password']
        force = options.get('force', False)

        existing = Employee.objects.filter(username=username)
        if existing.exists():
            if force:
                existing.delete()
                self.stdout.write(self.style.WARNING(f'Пользователь {username} удалён для пересоздания'))
            else:
                self.stdout.write(self.style.WARNING(f'Пользователь {username} уже существует'))
                return

        user = Employee.objects.create_superuser(
            username=username,
            full_name=name,
            password=password
        )
        self.stdout.write(self.style.SUCCESS(f'Администратор {username} создан успешно'))




