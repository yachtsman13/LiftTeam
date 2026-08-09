"""
Обновление приложения из интерфейса.

Разделение прав — главное в этом модуле. Приложение работает от пользователя
`lifteam` и не может перезапускать службу: для этого нужен root. Давать
веб-приложению sudo нельзя — тогда любая уязвимость в нём означала бы root
на устройстве.

Поэтому здесь только безопасные операции: чтение версии, проверка обновлений
и запись файла-заявки. Саму работу выполняет служба systemd от root, которая
следит за появлением этого файла (deploy/lifteam-update.path). Скрипт, который
она запускает, лежит вне каталога приложения и принадлежит root — если бы он
лежал рядом с кодом, взломщик подменил бы его и получил выполнение от root.
"""
import json
import re
import subprocess
from pathlib import Path

from django.conf import settings

APP_DIR = Path(settings.BASE_DIR)
REQUEST_FILE = APP_DIR / '.update-request'
STATUS_FILE = APP_DIR / '.update-status'

GIT_TIMEOUT = 60
COMMIT_RE = re.compile(r'^[0-9a-f]{7,40}$')


def _git(*args, timeout=GIT_TIMEOUT):
    """Запуск git в каталоге приложения. Возвращает (успех, вывод)."""
    try:
        result = subprocess.run(
            ['git', *args],
            cwd=str(APP_DIR),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return False, 'git не установлен'
    except subprocess.TimeoutExpired:
        return False, f'git не ответил за {timeout} с'
    if result.returncode != 0:
        return False, (result.stderr or result.stdout).strip()
    return True, result.stdout.strip()


def is_git_checkout():
    """Развёрнуто ли приложение из репозитория. При установке из архива
    обновление через интерфейс невозможно."""
    return (APP_DIR / '.git').exists()


def current_version():
    """Текущая версия: номер из CHANGELOG.md и коммит из git."""
    version = 'неизвестно'
    changelog = APP_DIR / 'CHANGELOG.md'
    if changelog.exists():
        for line in changelog.read_text(encoding='utf-8').splitlines():
            match = re.match(r'^##\s+(v[\d.]+)', line.strip())
            if match:
                version = match.group(1)
                break

    ok, commit = _git('rev-parse', '--short', 'HEAD')
    ok_date, date = _git('log', '-1', '--format=%cd', '--date=format:%d.%m.%Y %H:%M')
    return {
        'version': version,
        'commit': commit if ok else '—',
        'date': date if ok_date else '',
    }


def fetch_remote():
    """Запрашивает сведения об обновлениях с GitHub. Ничего не меняет
    в рабочем каталоге — только обновляет данные о ветке origin."""
    return _git('fetch', 'origin', 'main')


def pending_updates():
    """Список коммитов, которых ещё нет в развёрнутой версии."""
    ok, output = _git(
        'log', 'HEAD..origin/main', '--format=%h|%cd|%s', '--date=format:%d.%m.%Y'
    )
    if not ok or not output:
        return []
    updates = []
    for line in output.splitlines():
        parts = line.split('|', 2)
        if len(parts) == 3:
            updates.append({'commit': parts[0], 'date': parts[1], 'subject': parts[2]})
    return updates


def recent_history(limit=10):
    """Последние установленные версии — для отката."""
    ok, output = _git(
        'log', '-n', str(limit), '--format=%h|%cd|%s', '--date=format:%d.%m.%Y %H:%M'
    )
    if not ok or not output:
        return []
    history = []
    for index, line in enumerate(output.splitlines()):
        parts = line.split('|', 2)
        if len(parts) == 3:
            history.append({
                'commit': parts[0],
                'date': parts[1],
                'subject': parts[2],
                'is_current': index == 0,
            })
    return history


def request_update(target='latest', requested_by=''):
    """Создаёт заявку на обновление. Саму работу выполнит служба от root.

    target — 'latest' либо хеш коммита для отката.
    """
    if target != 'latest' and not COMMIT_RE.match(target):
        raise ValueError('Недопустимый идентификатор версии')

    STATUS_FILE.write_text(
        json.dumps({
            'state': 'requested',
            'message': 'Заявка принята, ожидается запуск',
            'log': [],
        }, ensure_ascii=False),
        encoding='utf-8',
    )
    # Файл-заявку пишем последним: его появление запускает службу
    REQUEST_FILE.write_text(
        json.dumps({'target': target, 'requested_by': requested_by}, ensure_ascii=False),
        encoding='utf-8',
    )


def read_status():
    """Ход выполнения обновления, который пишет служба."""
    if not STATUS_FILE.exists():
        return None
    try:
        return json.loads(STATUS_FILE.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError):
        return {'state': 'unknown', 'message': 'Не удалось прочитать состояние', 'log': []}


def update_in_progress():
    status = read_status()
    return bool(status and status.get('state') in ('requested', 'running'))
