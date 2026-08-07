# Развёртывание LiftTeam на Raspberry Pi 4

Инструкция для установки в офисе: Raspberry Pi 4 (4 ГБ), SQLite, доступ
локально и удалённо через интернет при сером IP.

Все команды выполняются на самом Pi — по SSH или с подключённой клавиатурой.

> **Выполняйте команды по одной, дожидаясь завершения каждой.** Если вставить
> в терминал несколько строк сразу, следующая команда попадёт в буфер, пока
> предыдущая ещё работает, и будет потеряна — а установка продолжится с
> пропущенным шагом, что выяснится позже и не очевидным образом.

> **Проверено на Windows, не на Pi.** Конфигурации написаны по документации,
> но на живом устройстве обкатываются впервые. Раздел «Если что-то не
> работает» — в конце.

---

## 1. Подготовка железа

### Загрузка с SSD вместо microSD

Самое важное решение во всей установке. microSD изнашивается от постоянной
записи и плохо переносит отключение питания — это основная причина потери
данных на Raspberry Pi. SSD на 120 ГБ через USB 3.0 стоит недорого и снимает
проблему.

1. Запишите Raspberry Pi OS Lite (64-bit) на SSD через Raspberry Pi Imager
2. В настройках Imager сразу задайте имя пользователя, пароль и включите SSH
3. Подключите SSD к синему порту USB 3.0, извлеките microSD
4. Pi 4 с актуальной прошивкой загрузится с USB автоматически

Берите Lite-версию без графической оболочки: она экономит память и ресурс
диска, а рабочий стол на сервере не нужен.

### Питание

Отключение электричества в момент записи в базу — реальный сценарий.
Минимум — источник бесперебойного питания на Pi и роутер. Даже недорогой
UPS, дающий пару минут, позволяет корректно завершить работу.

---

## 2. Подготовка системы

Обновите систему:

```bash
sudo apt update && sudo apt upgrade -y
```

Установите пакеты:

```bash
sudo apt install -y python3-venv python3-pip nginx git sqlite3
```

Создайте пользователя для приложения — работать от `root` или от вашей
учётной записи не нужно:

```bash
sudo useradd --system --home-dir /opt/lifteam --shell /bin/bash lifteam
```

```bash
sudo mkdir -p /opt/lifteam && sudo chown lifteam:lifteam /opt/lifteam
```

Каталог создаётся отдельно, без `--create-home`: иначе `useradd` положит
туда служебные файлы, и клонирование репозитория в непустой каталог
завершится ошибкой.

---

## 3. Установка приложения

```bash
sudo -u lifteam git clone https://github.com/yachtsman13/Lifteam.git /opt/lifteam
```

```bash
cd /opt/lifteam
```

```bash
sudo -u lifteam python3 -m venv venv
```

```bash
sudo -u lifteam venv/bin/pip install --upgrade pip
```

Установка зависимостей — самый долгий шаг, несколько минут:

```bash
sudo -u lifteam venv/bin/pip install -r requirements-pi.txt
```

Используйте `requirements-pi.txt`, а не `requirements.txt`: во втором есть
драйверы PostgreSQL и Redis, которые при SQLite не нужны, а на ARM ставятся
долго и иногда требуют компиляции.

---

## 4. Настройка

Сгенерируйте ключ приложения:

```bash
sudo -u lifteam venv/bin/python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
```

Скопируйте полученную строку и создайте файл `.env`:

```bash
sudo -u lifteam nano /opt/lifteam/.env
```

Содержимое:

```ini
SECRET_KEY=сюда-вставить-сгенерированный-ключ
DEBUG=False
ALLOWED_HOSTS=192.168.1.50,lifteam.ваша-сеть.ts.net,localhost
TIME_ZONE=Europe/Moscow
```

В `ALLOWED_HOSTS` перечислите адреса, по которым будете открывать приложение:
локальный IP Pi в офисной сети и имя устройства в Tailscale (появится
на шаге 7). Если адрес не указан, Django ответит `400 Bad Request` — это
не ошибка, а защита от подмены заголовка `Host`.

Сохранить в nano: `Ctrl+O`, `Enter`, затем `Ctrl+X`.

Закройте файл от посторонних — в нём ключ приложения:

```bash
sudo chmod 600 /opt/lifteam/.env && sudo chown lifteam:lifteam /opt/lifteam/.env
```

---

## 5. База данных и администратор

```bash
cd /opt/lifteam
```

```bash
sudo -u lifteam venv/bin/python manage.py migrate --settings=lifteam.settings_pi
```

```bash
sudo -u lifteam venv/bin/python manage.py init_cells --settings=lifteam.settings_pi
```

```bash
sudo -u lifteam venv/bin/python manage.py collectstatic --noinput --settings=lifteam.settings_pi
```

Флаг `--settings` указывается в каждой команде намеренно. `sudo` по умолчанию
очищает переменные окружения, поэтому заданный заранее `DJANGO_SETTINGS_MODULE`
до приложения не дойдёт, и команда молча выполнится с настройками для
разработки — с `DEBUG=True`.

Создайте администратора:

```bash
sudo -u lifteam venv/bin/python manage.py create_admin --username admin --name "Администратор" --password "временный" --settings=lifteam.settings_pi
```

Сразу задайте настоящий пароль — интерактивно, чтобы он не попал в историю
команд оболочки:

```bash
sudo -u lifteam venv/bin/python manage.py changepassword admin --settings=lifteam.settings_pi
```

Стандартную пару `admin` / `admin123` в офисе не оставляйте: она указана
в документации проекта и известна всем, кто видел репозиторий.

---

## 6. Автозапуск и веб-сервер

```bash
sudo cp /opt/lifteam/deploy/lifteam.service /etc/systemd/system/
```

```bash
sudo systemctl daemon-reload
```

```bash
sudo systemctl enable --now lifteam
```

```bash
sudo systemctl status lifteam
```

Должно быть `active (running)`. Выйти из просмотра — `q`.

Теперь nginx:

```bash
sudo cp /opt/lifteam/deploy/nginx-lifteam.conf /etc/nginx/sites-available/lifteam
```

```bash
sudo ln -sf /etc/nginx/sites-available/lifteam /etc/nginx/sites-enabled/lifteam
```

```bash
sudo rm -f /etc/nginx/sites-enabled/default
```

```bash
sudo nginx -t && sudo systemctl reload nginx
```

Узнайте адрес Pi:

```bash
hostname -I
```

Откройте `http://<этот-адрес>` с любого компьютера в офисной сети —
появится страница входа.

Закрепите за Pi постоянный адрес в настройках роутера Xiaomi, иначе после
перезагрузки он сменится и перестанет совпадать с `ALLOWED_HOSTS`.

---

## 7. Удалённый доступ через интернет

У вас серый IP: провайдер не выдаёт внешний адрес, поэтому проброс портов
на роутере не поможет — снаружи к нему не подключиться. Обходится через
Tailscale: это VPN на базе WireGuard, который сам устанавливает соединение
через NAT провайдера.

### На Raspberry Pi

```bash
curl -fsSL https://tailscale.com/install.sh | sh
```

```bash
sudo tailscale up
```

Команда покажет ссылку — откройте её в браузере и войдите в учётную запись.
Затем посмотрите выданное имя устройства:

```bash
tailscale status
```

Имя вида `lifteam.ваша-сеть.ts.net` добавьте в `ALLOWED_HOSTS` в файле `.env`:

```bash
sudo -u lifteam nano /opt/lifteam/.env
```

```bash
sudo systemctl restart lifteam
```

Чтобы авторизация не истекала через несколько месяцев, отключите срок
действия ключа для этого устройства в панели управления Tailscale
(Machines → устройство → Disable key expiry).

### На компьютерах, планшетах и телефонах

Установите клиент Tailscale и войдите в ту же учётную запись:

- Windows и macOS — с сайта tailscale.com
- Android — Google Play
- iPhone и iPad — App Store

После этого приложение открывается по адресу `http://lifteam.ваша-сеть.ts.net`
из любой точки, где есть интернет. В офисе работает и обычный локальный
адрес — Tailscale этому не мешает.

### Почему не публичный доступ

Через Tailscale приложение не видно из интернета: подключиться могут только
устройства вашей учётной записи, трафик шифруется. Открывать приложение
наружу означало бы, что его начнут находить сканеры и подбирать пароли —
при отсутствии HTTPS и двухфакторной аутентификации это плохая идея.

Если доступ понадобится с чужого устройства, куда VPN не поставить,
альтернатива — Cloudflare Tunnel, но там придётся отдельно заниматься
аутентификацией.

---

## 8. Резервное копирование

Копии базы делаются автоматически, ежедневно в 3:30.

```bash
sudo cp /opt/lifteam/deploy/lifteam-backup.service /etc/systemd/system/
```

```bash
sudo cp /opt/lifteam/deploy/lifteam-backup.timer /etc/systemd/system/
```

```bash
sudo systemctl daemon-reload
```

```bash
sudo systemctl enable --now lifteam-backup.timer
```

Проверьте, что расписание встало:

```bash
systemctl list-timers lifteam-backup.timer
```

Разовый запуск для проверки:

```bash
sudo systemctl start lifteam-backup
```

```bash
journalctl -u lifteam-backup -n 20
```

```bash
ls -la /opt/lifteam/backups/
```

### Копия за пределами устройства

Копии в `/opt/lifteam/backups/` лежат на том же диске, что и база. От ошибки
оператора это защищает, от отказа диска или кражи — нет.

Официального клиента Яндекс.Диска под ARM не существует, поэтому используем
`rclone` — он умеет Яндекс.Диск и работает на Pi:

```bash
sudo apt install -y rclone
```

```bash
sudo -u lifteam RCLONE_CONFIG=/opt/lifteam/rclone.conf rclone config
```

В диалоге: `n` (новый remote) → имя `yandex` → тип `yandex` → далее по
подсказкам, авторизация через браузер.

Затем включите выгрузку — откройте файл службы:

```bash
sudo nano /etc/systemd/system/lifteam-backup.service
```

Раскомментируйте строку (уберите `#` в начале):

```ini
Environment=LIFTEAM_RCLONE_REMOTE=yandex:LiftTeam/backups
```

```bash
sudo systemctl daemon-reload && sudo systemctl start lifteam-backup
```

```bash
journalctl -u lifteam-backup -n 20
```

Конфигурация rclone намеренно лежит в `/opt/lifteam`, а не в домашнем
каталоге: служба запускается с `ProtectHome=true` и до `/home` не достучится.

### Проверка восстановления

Бэкап, который ни разу не разворачивали, — это не бэкап, а надежда.
Проверьте хотя бы раз, а потом повторяйте раз в несколько месяцев.

Список копий:

```bash
cd /opt/lifteam && sudo -u lifteam venv/bin/python manage.py restore_db --list --settings=lifteam.settings_pi
```

Безопасная проверка на копии, без риска для рабочих данных:

```bash
sudo cp $(ls -t /opt/lifteam/backups/db_*.sqlite3 | head -1) /tmp/test.sqlite3
```

```bash
sqlite3 /tmp/test.sqlite3 "PRAGMA integrity_check; SELECT COUNT(*) FROM core_sparepart;"
```

```bash
rm /tmp/test.sqlite3
```

Если понадобится настоящее восстановление:

```bash
sudo systemctl stop lifteam
```

```bash
cd /opt/lifteam && sudo -u lifteam venv/bin/python manage.py restore_db --settings=lifteam.settings_pi
```

```bash
sudo systemctl start lifteam
```

Команда проверяет копию до того, как трогать рабочую базу, а текущую
отставляет в сторону с меткой времени — ошибочное восстановление обратимо.

---

## 9. Обновление

Одной командой:

```bash
sudo /opt/lifteam/deploy/update.sh
```

Скрипт по порядку: делает копию базы, останавливает приложение, забирает
изменения из GitHub, ставит зависимости, применяет миграции, пересобирает
статику и запускает приложение обратно. В конце проверяет, что служба
действительно поднялась.

Если на каком-то шаге произойдёт ошибка, приложение останется остановленным —
это сделано намеренно, чтобы оно не работало со схемой базы, не совпадающей
с кодом. Скрипт выведет команды для возврата к предыдущей версии.

> Скрипт `update.sh` в корне проекта для сервера не подходит: он делает
> `git reset --hard` и на этом заканчивает — без копии базы, миграций
> и перезапуска службы.

### Вручную, если нужно по шагам

```bash
cd /opt/lifteam
```

```bash
sudo -u lifteam venv/bin/python manage.py backup_db --settings=lifteam.settings_pi
```

```bash
sudo systemctl stop lifteam
```

```bash
sudo -u lifteam git pull
```

```bash
sudo -u lifteam venv/bin/pip install -r requirements-pi.txt
```

```bash
sudo -u lifteam venv/bin/python manage.py migrate --settings=lifteam.settings_pi
```

```bash
sudo -u lifteam venv/bin/python manage.py collectstatic --noinput --settings=lifteam.settings_pi
```

```bash
sudo systemctl start lifteam
```

---

## 10. Если что-то не работает

Логи приложения:

```bash
journalctl -u lifteam -f
```

Логи резервного копирования:

```bash
journalctl -u lifteam-backup -n 50
```

Логи веб-сервера:

```bash
sudo tail -f /var/log/nginx/error.log
```

**`400 Bad Request`** — адрес, по которому вы открываете приложение, не указан
в `ALLOWED_HOSTS`. Добавьте его в `.env` и выполните `sudo systemctl restart lifteam`.

**`502 Bad Gateway`** — приложение не запущено. `systemctl status lifteam`
покажет причину.

**Служба не стартует, ошибка про путь в `ExecStart`** — в файле оказались
переводы строк Windows:

```bash
sudo sed -i 's/\r$//' /opt/lifteam/deploy/*.sh
```

**Остатки на складе не обновляются без перезагрузки страницы** — не проходит
WebSocket-соединение. Проверьте, что в конфигурации nginx есть секция
`location /ws/` с заголовками `Upgrade`, и перезагрузите nginx.

**`database is locked`** — не должно возникать, база работает в режиме WAL.
Проверить:

```bash
sqlite3 /opt/lifteam/db.sqlite3 "PRAGMA journal_mode;"
```

Ответ должен быть `wal`.

**Нет места на диске** — вероятно, накопились копии базы. Глубина хранения
задаётся параметром `KEEP_DAYS` в `deploy/backup.sh`:

```bash
du -sh /opt/lifteam/backups/
```

---

## Что не входит в эту установку

**Печать с планшетов и телефонов не заработает.** Этикетки печатаются через
диалог браузера, поэтому печатать может только устройство, к которому
подключён принтер. Для печати с мобильных нужна доработка — печать на стороне
сервера через CUPS.

**HTTPS не настраивается.** При доступе через Tailscale трафик шифруется
самим VPN, а в локальной сети сертификат для адреса вида `192.168.1.50`
получить негде. Если приложение когда-либо будет открыто в интернет,
HTTPS станет обязательным.

**Интерфейс не адаптирован под телефоны.** Сетка кассетниц рассчитана на
8×8 ячеек во всю высоту экрана — на смартфоне она будет мелкой. С планшета
работать удобно.
