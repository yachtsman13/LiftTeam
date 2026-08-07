#!/bin/bash
# Обновление LiftTeam на сервере до последней версии из GitHub.
#
# Запуск:  sudo /opt/lifteam/deploy/update.sh
#
# Порядок важен: сначала копия базы, потом остановка службы, и только затем
# обновление кода и миграции. Если миграция упадёт на середине, служба
# останется остановленной, а не поднимется с несовпадающей схемой данных.

set -euo pipefail

APP_DIR="/opt/lifteam"
APP_USER="lifteam"
VENV="${APP_DIR}/venv"
SETTINGS="lifteam.settings_pi"
SERVICE="lifteam"

if [[ $EUID -ne 0 ]]; then
    echo "Запускать через sudo: sudo ${APP_DIR}/deploy/update.sh" >&2
    exit 1
fi

cd "${APP_DIR}"

run_as_app() {
    sudo -u "${APP_USER}" "$@"
}

echo "==> Резервная копия базы"
run_as_app "${VENV}/bin/python" manage.py backup_db --settings="${SETTINGS}"

PREVIOUS_COMMIT="$(run_as_app git rev-parse HEAD)"
echo "==> Текущая версия: ${PREVIOUS_COMMIT:0:7}"

echo "==> Остановка службы"
systemctl stop "${SERVICE}"

# Если что-то пойдёт не так после этой точки, служба останется остановленной,
# чтобы приложение не работало с несовпадающей схемой базы. Подсказываем,
# как вернуться к предыдущей версии.
on_failure() {
    echo "" >&2
    echo "ОБНОВЛЕНИЕ ПРЕРВАНО. Приложение остановлено." >&2
    echo "" >&2
    echo "Вернуться к предыдущей версии:" >&2
    echo "  cd ${APP_DIR}" >&2
    echo "  sudo -u ${APP_USER} git reset --hard ${PREVIOUS_COMMIT}" >&2
    echo "  sudo -u ${APP_USER} ${VENV}/bin/python manage.py restore_db --settings=${SETTINGS}" >&2
    echo "  sudo systemctl start ${SERVICE}" >&2
}
trap on_failure ERR

echo "==> Получение изменений"
# --ff-only: если история разошлась, команда сообщит об этом и остановится,
# вместо того чтобы создавать merge-коммит прямо на рабочем сервере
run_as_app git fetch origin main
run_as_app git merge --ff-only origin/main

echo "==> Зависимости"
run_as_app "${VENV}/bin/pip" install -q -r requirements-pi.txt

echo "==> Миграции"
run_as_app "${VENV}/bin/python" manage.py migrate --settings="${SETTINGS}"

echo "==> Статика"
run_as_app "${VENV}/bin/python" manage.py collectstatic --noinput --settings="${SETTINGS}"

trap - ERR

echo "==> Запуск службы"
systemctl start "${SERVICE}"

sleep 3
if systemctl is-active --quiet "${SERVICE}"; then
    echo ""
    echo "Готово. Версия: $(run_as_app git rev-parse --short HEAD)"
    echo "Логи: journalctl -u ${SERVICE} -f"
else
    echo ""
    echo "Служба не запустилась. Смотрите: journalctl -u ${SERVICE} -n 50" >&2
    exit 1
fi
