#!/usr/bin/env python3
"""
LiftTeam Launcher v2.106.1 — standalone desktop app
Запускает Django сервер и открывает браузер автоматически
"""
import os
import sys
import subprocess
import webbrowser
import time
import threading


def run_server():
    """Запуск Django development server"""
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'lifteam.settings')
    from django.core.management import call_command
    try:
        call_command('runserver', '127.0.0.1:8000')
    except Exception as e:
        print(f"\n  [ERROR] Сервер остановился: {e}")
        import traceback
        traceback.print_exc()
        input("\n  Нажмите Enter для выхода...")
        sys.exit(1)


def init_database():
    """Инициализация базы данных и создание администратора"""
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'lifteam.settings')
    import django
    django.setup()

    from django.core.management import call_command
    from django.db import connection
    from core.models import Employee

    # 1. Создание и применение миграций
    print("  [DB] Создание миграций...")
    call_command('makemigrations', '--noinput')
    print("  [DB] Применение миграций...")
    call_command('migrate')

    # 2. Проверяем, что таблица Employee создана
    with connection.cursor() as cursor:
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='core_employee';")
        if not cursor.fetchone():
            print("  [ERROR] Таблица Employee не найдена. Убедитесь, что миграции созданы.")
            raise RuntimeError("Таблица core_employee отсутствует. Запустите 'makemigrations' вручную.")

    # 3. Ячейки
    print("  [DB] Инициализация ячеек хранения...")
    call_command('init_cells')

    # 4. Создание администратора с проверкой
    print("  [DB] Создание пользователей...")
    admin_created = False
    if not Employee.objects.filter(username='admin').exists():
        try:
            call_command('create_admin', '--username', 'admin', '--name', 'Administrator', '--password', 'admin123')
            admin_created = True
        except Exception as e:
            print(f"  [ERROR] Не удалось создать admin: {e}")
    else:
        admin = Employee.objects.get(username='admin')
        if not admin.is_active:
            admin.is_active = True
            admin.save()
            print("  [OK] Пользователь admin активирован")
        print("  [OK] Пользователь admin уже существует")

    # 5. Тестовый пользователь
    if not Employee.objects.filter(username='test').exists():
        try:
            Employee.objects.create_user(
                username='test', full_name='Test User', password='test', role='repair_manager'
            )
            print("  [OK] Тестовый пользователь test создан")
        except Exception as e:
            print(f"  [ERROR] Не удалось создать test: {e}")
    else:
        print("  [OK] Тестовый пользователь test уже существует")

    # 6. Диагностика
    print("\n  [DIAG] Пользователи в системе:")
    for user in Employee.objects.all():
        print(f"         - {user.username} | {user.full_name} | active={user.is_active} | staff={user.is_staff} | superuser={user.is_superuser}")

    return admin_created


def main():
    """Главная функция запуска"""
    print("=" * 56)
    print("  LiftTeam v2.106.1 — Система учёта ремонта лифтов")
    print("=" * 56)

    # Переходим в директорию скрипта
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    # Проверяем/устанавливаем зависимости
    print("\n[1/4] Проверка зависимостей...")
    try:
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', '-q', '-r', 'requirements.txt'])
    except subprocess.CalledProcessError as e:
        print(f"  [WARNING] Ошибка установки зависимостей: {e}")
        print("  Попытка продолжить...")

    # Создаём .env если нет
    if not os.path.exists('.env'):
        print("[2/4] Создание .env...")
        with open('.env', 'w', encoding='utf-8') as f:
            f.write('SECRET_KEY=standalone-secret-key-for-local-use-only\n')
            f.write('DEBUG=True\n')
            f.write('ALLOWED_HOSTS=localhost,127.0.0.1\n')

    # Инициализируем БД
    print("[3/4] Инициализация базы данных...")
    try:
        init_database()
    except Exception as e:
        print(f"\n  [ERROR] Ошибка инициализации БД: {e}")
        import traceback
        traceback.print_exc()
        print("\n  Попробуйте удалить db.sqlite3 и запустить снова.")
        input("  Нажмите Enter для выхода...")
        return

    # Запускаем сервер
    print("[4/4] Запуск сервера...")
    print("\n" + "=" * 56)
    print("  Сервер: http://127.0.0.1:8000")
    print("  Логин:  admin")
    print("  Пароль: admin123")
    print("=" * 56 + "\n")

    # Открываем браузер через 2 секунды
    def open_browser():
        time.sleep(2)
        webbrowser.open('http://127.0.0.1:8000/login/')

    threading.Thread(target=open_browser, daemon=True).start()

    # Запускаем сервер (блокирующий вызов)
    run_server()


if __name__ == '__main__':
    main()





