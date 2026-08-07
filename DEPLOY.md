# Развёртывание LiftTeam на Raspberry Pi 4

Инструкция для установки в офисе: Raspberry Pi 4 (4 ГБ), SQLite, доступ
локально и удалённо через интернет при сером IP.

Все команды выполняются на самом Pi — по SSH или с подключённой клавиатурой.

> **Проверено на Windows, не на Pi.** Конфигурации написаны по документации,
> но на живом устройстве не обкатывались. При первом развёртывании что-то
> может потребовать правки — раздел «Если что-то не работает» в конце.

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

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3-venv python3-pip nginx git sqlite3
```

Создайте отдельного пользователя для приложения — работать от `root` или
от вашей учётной записи не нужно:

```bash
sudo useradd --system --home-dir /opt/lifteam --shell /bin/bash lifteam
sudo mkdir -p /opt/lifteam
sudo chown lifteam:lifteam /opt/lifteam
```

Каталог создаётся отдельно, без `--create-home`: иначе `useradd` положит
туда служебные файлы, и клонирование репозитория в непустой каталог
завершится ошибкой.

---

## 3. Установка приложения

```bash
sudo -u lifteam git clone https://github.com/yachtsman13/Lifteam.git /opt/lifteam
cd /opt/lifteam

sudo -u lifteam python3 -m venv venv
sudo -u lifteam venv/bin/pip install --upgrade pip
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

Создайте файл `/opt/lifteam/.env`:

```bash
sudo -u lifteam nano /opt/lifteam/.env
```

```ini
SECRET_KEY=сюда-вставить-сгенерированный-ключ
DEBUG=False
ALLOWED_HOSTS=192.168.1.50,lifteam.ваша-сеть.ts.net,localhost
TIME_ZONE=Europe/Moscow
```

В `ALLOWED_HOSTS` перечислите адреса, по которым будете открывать приложение:
локальный IP Pi в офисной сети и имя устройства в Tailscale (появится
после шага 7). Если адрес не указан, Django ответит `400 Bad Request` —
это не ошибка, а защита от подмены заголовка `Host`.

Закройте файл от посторонних — в нём ключ приложения:

```bash
sudo chmod 600 /opt/lifteam/.env
sudo chown lifteam:lifteam /opt/lifteam/.env
```

---

## 5. База данных и первый запуск

```bash
cd /opt/lifteam

sudo -u lifteam venv/bin/python manage.py migrate --settings=lifteam.settings_pi
sudo -u lifteam venv/bin/python manage.py init_cells --settings=lifteam.settings_pi
sudo -u lifteam venv/bin/python manage.py collectstatic --noinput --settings=lifteam.settings_pi
```

Флаг `--settings` указывается в каждой команде намеренно. `sudo` по умолчанию
очищает переменные окружения, поэтому заданный заранее `DJANGO_SETTINGS_MODULE`
до приложения не дойдёт, и команда молча выполнится с настройками для
разработки — с `DEBUG=True` и несобранной статикой.

Создайте администратора:

```bash
sudo -u lifteam venv/bin/python manage.py create_admin \
    --username admin --name "Администратор" --password "временный-пароль" \
    --settings=lifteam.settings_pi

# и сразу задайте настоящий пароль — интерактивно, чтобы он не попал
# в историю команд оболочки
sudo -u lifteam venv/bin/python manage.py changepassword admin --settings=lifteam.settings_pi
```

Стандартную пару `admin` / `admin123` в офисе не оставляйте: она указана
в документации проекта и известна всем, кто видел репозиторий.

---

## 6. Автозапуск и веб-сервер

```bash
# Приложение
sudo cp /opt/lifteam/deploy/lifteam.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lifteam
sudo systemctl status lifteam

# nginx
sudo cp /opt/lifteam/deploy/nginx-lifteam.conf /etc/nginx/sites-available/lifteam
sudo ln -sf /etc/nginx/sites-available/lifteam /etc/nginx/sites-enabled/lifteam
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

Проверьте с любого компьютера в офисной сети: `http://192.168.1.50`
(подставьте IP вашего Pi, узнать — `hostname -I`).

Рекомендую закрепить за Pi постоянный адрес в настройках роутера Xiaomi,
иначе после перезагрузки он сменится и перестанет совпадать с `ALLOWED_HOSTS`.

---

## 7. Удалённый доступ через интернет

У вас серый IP: провайдер не выдаёт внешний адрес, поэтому проброс портов
на роутере не поможет — снаружи к нему не подключиться. Обходится через
Tailscale: это VPN на базе WireGuard, который сам устанавливает соединение
через NAT провайдера.

### На Raspberry Pi

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Команда покажет ссылку — откройте её в браузере и войдите в учётную запись.
После подключения посмотрите выданное имя устройства:

```bash
tailscale status
```

Имя вида `lifteam.ваша-сеть.ts.net` впишите в `ALLOWED_HOSTS` в `.env`
и перезапустите приложение:

```bash
sudo systemctl restart lifteam
```

Чтобы авторизация не истекала каждые несколько месяцев, отключите срок
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
sudo cp /opt/lifteam/deploy/lifteam-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lifteam-backup.timer

# проверить, что расписание встало
systemctl list-timers lifteam-backup.timer
```

Разовый запуск для проверки:

```bash
sudo systemctl start lifteam-backup
journalctl -u lifteam-backup -n 20
ls -la /opt/lifteam/backups/
```

### Копия за пределами устройства

Копии в `/opt/lifteam/backups/` лежат на том же диске, что и база. От ошибки
оператора это защищает, от отказа диска или кражи — нет. Нужна выгрузка наружу.

Официального клиента Яндекс.Диска под ARM не существует, поэтому используем
`rclone` — он умеет Яндекс.Диск и работает на Pi:

```bash
sudo apt install -y rclone
sudo -u lifteam RCLONE_CONFIG=/opt/lifteam/rclone.conf rclone config
```

В диалоге: `n` (новый remote) → имя `yandex` → тип `yandex` → далее по
подсказкам, авторизация через браузер.

Затем включите выгрузку в `/etc/systemd/system/lifteam-backup.service` —
раскомментируйте строку:

```ini
Environment=LIFTEAM_RCLONE_REMOTE=yandex:LiftTeam/backups
```

```bash
sudo systemctl daemon-reload
sudo systemctl start lifteam-backup
journalctl -u lifteam-backup -n 20
```

Конфигурация rclone намеренно лежит в `/opt/lifteam`, а не в домашнем
каталоге: служба запускается с `ProtectHome=true` и до `/home` не достучится.

### Проверка восстановления

Бэкап, который ни разу не разворачивали, — это не бэкап, а надежда.
Проверьте хотя бы раз, а потом повторяйте раз в несколько месяцев:

```bash
cd /opt/lifteam
sudo -u lifteam venv/bin/python manage.py restore_db --list --settings=lifteam.settings_pi
```

Полная проверка на копии базы, без риска для рабочих данных:

```bash
cd /tmp
sudo -u lifteam cp /opt/lifteam/backups/db_*.sqlite3 /tmp/test.sqlite3
sqlite3 /tmp/test.sqlite3 "PRAGMA integrity_check; SELECT COUNT(*) FROM core_sparepart;"
rm /tmp/test.sqlite3
```

Если понадобится настоящее восстановление — остановите приложение:

```bash
sudo systemctl stop lifteam
cd /opt/lifteam
sudo -u lifteam venv/bin/python manage.py restore_db --settings=lifteam.settings_pi
sudo systemctl start lifteam
```

Команда проверяет копию до того, как трогать рабочую базу, а текущую
отставляет в сторону с меткой времени — ошибочное восстановление обратимо.

---

## 9. Обновление

```bash
cd /opt/lifteam
sudo systemctl stop lifteam

# копия перед обновлением — если миграция пойдёт не так, будет откуда вернуться
sudo -u lifteam venv/bin/python manage.py backup_db --settings=lifteam.settings_pi

sudo -u lifteam git pull
sudo -u lifteam venv/bin/pip install -r requirements-pi.txt
sudo -u lifteam venv/bin/python manage.py migrate --settings=lifteam.settings_pi
sudo -u lifteam venv/bin/python manage.py collectstatic --noinput --settings=lifteam.settings_pi

sudo systemctl start lifteam
```

---

## 10. Если что-то не работает

**Смотреть логи:**

```bash
journalctl -u lifteam -f              # приложение
journalctl -u lifteam-backup -n 50    # бэкапы
sudo tail -f /var/log/nginx/error.log # веб-сервер
```

**`400 Bad Request`** — адрес, по которому вы открываете приложение, не указан
в `ALLOWED_HOSTS`. Добавьте его в `.env` и выполните `sudo systemctl restart lifteam`.

**`502 Bad Gateway`** — приложение не запущено. `systemctl status lifteam`
покажет причину.

**Служба не стартует, ошибка про путь в `ExecStart`** — в файле оказались
переводы строк Windows. Проверить: `file /opt/lifteam/deploy/backup.sh`
(должно быть без «CRLF»). Исправить: `sudo sed -i 's/\r$//' /opt/lifteam/deploy/*.sh`.

**Остатки на складе не обновляются без перезагрузки страницы** — не проходит
WebSocket-соединение. Проверьте, что в конфигурации nginx есть секция
`location /ws/` с заголовками `Upgrade`, и перезагрузите: `sudo nginx -t && sudo systemctl reload nginx`.

**`database is locked`** — не должно возникать, база работает в режиме WAL.
Если появилось, проверьте: `sqlite3 /opt/lifteam/db.sqlite3 "PRAGMA journal_mode;"`
— ответ должен быть `wal`.

**Нет места на диске** — вероятно, накопились копии базы.
Проверить: `du -sh /opt/lifteam/backups/`. Глубина хранения задаётся
параметром `KEEP_DAYS` в `deploy/backup.sh`.

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
