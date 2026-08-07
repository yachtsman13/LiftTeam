# LiftTeam

Система учёта ремонта лифтового оборудования и склада радиодеталей.

## Возможности

- **Заказы на ремонт** — оборудование в заказе с отдельным описанием
  неисправности, стоимостью и статусами ремонта и оплаты, история изменений
  с указанием сотрудника
- **Склад радиодеталей** — приход, расход, списание по заказу, журнал движений,
  импорт и экспорт Excel, план закупок по деталям ниже минимального остатка
- **Ячейки хранения** — 12 кассетниц по 64 ячейки, визуальная сетка,
  перемещение деталей, несколько деталей в одной ячейке
- **Печать этикеток** 43×25 мм с QR-кодом — на оборудование в заказе и на ячейки
- **Уведомления об остатках** в реальном времени через WebSocket
- **Роли сотрудников** — администратор, кладовщик, менеджер по ремонту, бухгалтер
- **REST API** для всех сущностей

## Стек

Python 3.11+, Django 5.1, Django REST Framework, Django Channels, Bootstrap 5.
База — SQLite (по умолчанию) или PostgreSQL.

## Запуск для разработки

```bash
python -m venv venv
venv\Scripts\activate          # Windows
source venv/bin/activate       # Linux и macOS

pip install -r requirements.txt
cp .env.example .env           # и вписать SECRET_KEY

python manage.py migrate
python manage.py init_cells
python manage.py create_admin --username admin --name "Администратор" --password admin123

python manage.py runserver
```

Приложение откроется на http://127.0.0.1:8000

Быстрый вариант — `python lifteam_launcher.py`: проверит зависимости, создаст
базу и администратора, запустит сервер и откроет браузер.

## Развёртывание

Установка на Raspberry Pi в офисе, с удалённым доступом через интернет при
сером IP — **[DEPLOY.md](DEPLOY.md)**.

Вариант с Docker (PostgreSQL, Redis, Nginx) — `docker-compose.yml` и `start.sh`.

## Резервное копирование

```bash
python manage.py backup_db              # копия с проверкой целостности
python manage.py restore_db --list      # список копий
python manage.py restore_db             # восстановить последнюю
```

Копия снимается штатным механизмом SQLite и безопасна на работающем сервере.
Копировать `db.sqlite3` обычным `cp` нельзя: база работает в режиме WAL,
и такая копия окажется повреждённой.

На боевом сервере копирование выполняется по расписанию — см. DEPLOY.md.

## Тесты

```bash
python manage.py test core
```

## Документация

- [DEPLOY.md](DEPLOY.md) — развёртывание на Raspberry Pi
- [TZ.md](TZ.md) — техническое задание
- [CHANGELOG.md](CHANGELOG.md) — история версий
- [PROMPT.md](PROMPT.md) — правила работы над проектом

## Безопасность

Перед запуском в эксплуатацию:

- сгенерируйте свой `SECRET_KEY`
  (`python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"`)
- смените стандартный пароль `admin` / `admin123`
- не коммитьте `.env` и `db.sqlite3` — они исключены в `.gitignore`
