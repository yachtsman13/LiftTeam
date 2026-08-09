#!/bin/bash
# Выполняет обновление LiftTeam по заявке из веб-интерфейса.
#
# УСТАНАВЛИВАЕТСЯ В /usr/local/sbin/lifteam-update, ВЛАДЕЛЕЦ root, ПРАВА 755.
#
# Класть этот файл в /opt/lifteam нельзя. Каталог приложения принадлежит
# пользователю lifteam, от которого работает веб-приложение. Если бы служба
# root запускала скрипт оттуда, то любой, кто получил доступ к приложению,
# подменил бы содержимое скрипта и выполнил произвольные команды от root.
#
# От root здесь выполняется только перезапуск службы. Всё остальное —
# git, pip, manage.py — выполняется от lifteam.

set -uo pipefail

APP_DIR="/opt/lifteam"
APP_USER="lifteam"
VENV="${APP_DIR}/venv"
SETTINGS="lifteam.settings_pi"
SERVICE="lifteam"
REQUEST_FILE="${APP_DIR}/.update-request"
STATUS_FILE="${APP_DIR}/.update-status"

LOG_LINES=()

write_status() {
    local state="$1" message="$2"
    local log_json
    log_json=$(printf '%s\n' "${LOG_LINES[@]:-}" | python3 -c 'import json,sys; print(json.dumps([l for l in sys.stdin.read().splitlines() if l], ensure_ascii=False))')
    python3 - "$STATUS_FILE" "$state" "$message" "$log_json" <<'PY'
import json, sys
path, state, message, log_json = sys.argv[1:5]
with open(path, 'w', encoding='utf-8') as f:
    json.dump({'state': state, 'message': message, 'log': json.loads(log_json)},
              f, ensure_ascii=False)
PY
    chown "${APP_USER}:${APP_USER}" "$STATUS_FILE" 2>/dev/null || true
}

step() {
    LOG_LINES+=("$1")
    write_status running "$1"
    echo "$1"
}

fail() {
    LOG_LINES+=("ОШИБКА: $1")
    write_status failed "$1"
    echo "ОШИБКА: $1" >&2
    exit 1
}

run_as_app() {
    sudo -u "$APP_USER" "$@"
}

[[ -f "$REQUEST_FILE" ]] || exit 0

# Читаем заявку и сразу удаляем её, чтобы служба не сработала повторно
TARGET=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["target"])' "$REQUEST_FILE" 2>/dev/null || echo "")
rm -f "$REQUEST_FILE"

# Проверяем значение из заявки. Файл пишет веб-приложение, поэтому доверять
# его содержимому нельзя: без проверки сюда можно было бы передать что угодно.
if [[ "$TARGET" != "latest" ]] && ! [[ "$TARGET" =~ ^[0-9a-f]{7,40}$ ]]; then
    fail "Недопустимый идентификатор версии в заявке"
fi

cd "$APP_DIR" || fail "Каталог ${APP_DIR} недоступен"

PREVIOUS=$(run_as_app git rev-parse HEAD 2>/dev/null) || fail "Это не репозиторий git"

step "Резервная копия базы"
run_as_app "${VENV}/bin/python" manage.py backup_db --settings="${SETTINGS}" >/dev/null \
    || fail "Не удалось создать копию базы, обновление отменено"

step "Загрузка изменений"
run_as_app git fetch origin main || fail "Не удалось связаться с GitHub"

if [[ "$TARGET" == "latest" ]]; then
    step "Переход на последнюю версию"
    run_as_app git merge --ff-only origin/main || fail "История разошлась, требуется вмешательство по SSH"
else
    # Откат: убеждаемся, что коммит действительно принадлежит истории проекта
    run_as_app git merge-base --is-ancestor "$TARGET" origin/main 2>/dev/null \
        || fail "Версия $TARGET не найдена в истории проекта"
    step "Откат к версии $TARGET"
    run_as_app git reset --hard "$TARGET" || fail "Не удалось переключить версию"
fi

step "Установка зависимостей"
run_as_app "${VENV}/bin/pip" install -q -r requirements-pi.txt || fail "Не удалось установить зависимости"

step "Применение миграций"
run_as_app "${VENV}/bin/python" manage.py migrate --settings="${SETTINGS}" >/dev/null \
    || fail "Миграции не применились. Приложение остановлено, версия ${PREVIOUS:0:7} в резерве"

step "Сборка статики"
run_as_app "${VENV}/bin/python" manage.py collectstatic --noinput --settings="${SETTINGS}" >/dev/null \
    || fail "Не удалось собрать статику"

NEW_VERSION=$(run_as_app git rev-parse --short HEAD)
LOG_LINES+=("Готово, версия ${NEW_VERSION}")
write_status finished "Обновление завершено, приложение перезапускается"

# Перезапуск — единственное действие, которому нужны права root.
# Выполняется последним: он обрывает соединение с браузером.
systemctl restart "$SERVICE"
