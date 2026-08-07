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
    # --checksum вместо сравнения по дате: копии создаются заново каждый раз,
    # и время изменения у них всегда новое
    rclone sync "${APP_DIR}/backups" "${RCLONE_REMOTE}" --checksum
    echo "[$(date '+%F %T')] Выгрузка завершена"
else
    echo "ВНИМАНИЕ: внешняя выгрузка не настроена — копии только на этом устройстве" >&2
fi

echo "[$(date '+%F %T')] Готово"
