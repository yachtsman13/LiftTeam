#!/bin/bash
# Ежедневное резервное копирование LiftTeam.
# Запускается по расписанию из lifteam-backup.timer.
#
# Копия на том же устройстве, где база, спасает от ошибки оператора,
# но не от отказа диска и не от кражи. Поэтому вторым шагом копии
# отправляются на внешнее хранилище.
#
# Официального клиента Яндекс.Диска под ARM нет — на Raspberry Pi
# используйте rclone (он умеет Яндекс.Диск и собран под arm64/armhf):
#   sudo apt install rclone
#   sudo -u lifteam RCLONE_CONFIG=/opt/lifteam/rclone.conf rclone config
# Конфигурация должна лежать в /opt/lifteam, а не в домашнем каталоге:
# сервис запускается с ProtectHome=true и до /home не достучится.
# Либо смонтируйте Диск по WebDAV через davfs2 и копируйте обычным cp.

set -euo pipefail

APP_DIR="/opt/lifteam"
VENV="${APP_DIR}/venv"
KEEP_DAYS=30

# Куда отправлять копии наружу. Пусто — внешняя выгрузка отключена.
# Пример для rclone: RCLONE_REMOTE="yandex:LiftTeam/backups"
RCLONE_REMOTE="${LIFTEAM_RCLONE_REMOTE:-}"

# Снимки к шагам технологических карт лежат отдельно от базы: в базе
# только имя файла. Копия базы без них восстановит карты без картинок,
# и заметно это станет у стола, когда картинка понадобится.
#
# По умолчанию — подкаталог того же места, куда уходят копии базы:
# так настроенная выгрузка начинает забирать снимки сама, без правки
# юнита на уже работающей установке.
RCLONE_MEDIA_REMOTE="${LIFTEAM_RCLONE_MEDIA_REMOTE:-${RCLONE_REMOTE:+${RCLONE_REMOTE}/media}}"

cd "${APP_DIR}"

echo "[$(date '+%F %T')] Создание резервной копии"
DJANGO_SETTINGS_MODULE=lifteam.settings_pi \
    "${VENV}/bin/python" manage.py backup_db --keep "${KEEP_DAYS}"

if [[ -n "${RCLONE_REMOTE}" ]]; then
    if ! command -v rclone >/dev/null 2>&1; then
        echo "ОШИБКА: задан RCLONE_REMOTE, но rclone не установлен" >&2
        exit 1
    fi
    echo "[$(date '+%F %T')] Выгрузка на ${RCLONE_REMOTE}"
    # copy, а не sync: sync зеркалит удаления, и чистка старых копий на этом
    # устройстве стирала бы их и в облаке. Тогда внешняя копия не спасает
    # ровно в том случае, ради которого она нужна — когда данные пропали здесь.
    # Копия весит сотни килобайт, накопление за годы несущественно.
    rclone copy "${APP_DIR}/backups" "${RCLONE_REMOTE}" --checksum

    # Снимки — тем же copy: файл, удалённый здесь по ошибке, обязан
    # остаться в облаке. Каталога может не быть вовсе — карт со снимками
    # ещё не завели, и это не повод падать с ошибкой.
    if [[ -d "${APP_DIR}/media" ]]; then
        echo "[$(date '+%F %T')] Выгрузка снимков на ${RCLONE_MEDIA_REMOTE}"
        rclone copy "${APP_DIR}/media" "${RCLONE_MEDIA_REMOTE}" --checksum
    else
        echo "[$(date '+%F %T')] Каталог media отсутствует — снимков пока нет"
    fi

    echo "[$(date '+%F %T')] Выгрузка завершена"
else
    echo "ВНИМАНИЕ: внешняя выгрузка не настроена — копии только на этом устройстве" >&2
fi

echo "[$(date '+%F %T')] Готово"
