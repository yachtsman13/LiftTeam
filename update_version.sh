#!/bin/bash
# LiftTeam — Update Version (Linux/macOS)

cd "$(dirname "$0")"

if [ -z "$1" ]; then
    echo "  Использование: update_version.sh [ВЕРСИЯ]"
    echo "  Пример: update_version.sh v2.8.0"
    exit 1
fi

NEW_VERSION="$1"

echo ""
echo "  ================================================"
echo "   LiftTeam — Update Version to $NEW_VERSION"
echo "  ================================================"
echo ""

# CHANGELOG.md намеренно НЕ обрабатывается: замена по всему файлу переписала бы
# заголовки всех прошлых выпусков на новый номер и уничтожила историю версий.
# Запись о новой версии добавляется в него вручную.

# Заменяется НЕ «любой номер версии», а ровно текущий. Раньше здесь стояло
# s/v[0-9]+\.[0-9]+\.[0-9]+/НОВАЯ/g — и каждый выпуск переписывал заодно все
# упоминания прошлых версий в пояснениях: «до v2.50.0 поля назывались иначе»
# превращалось в «до v2.56.0», то есть в неправду. Историю приходилось
# восстанавливать руками после каждого выпуска.
# Старая версия берётся из кода, а не из CHANGELOG: запись о выпуске туда
# дописывают до запуска скрипта, и верхним заголовком там уже стоит новый
# номер — искать по нему старый бесполезно.
OLD_VERSION=$(grep -m1 -oE 'v[0-9]+\.[0-9]+\.[0-9]+(\.[0-9]+)*' lifteam/settings.py)

if [ -z "$OLD_VERSION" ]; then
    echo "  [ОШИБКА] В lifteam/settings.py не найден номер текущей версии."
    exit 1
fi

if [ "$OLD_VERSION" = "$NEW_VERSION" ]; then
    echo "  [ОШИБКА] В файлах уже стоит $NEW_VERSION — менять нечего."
    exit 1
fi

echo "  Замена: $OLD_VERSION -> $NEW_VERSION"
echo ""

# Замену делает Python, а не sed: правило требует посмотреть, что стоит
# ПЕРЕД номером, а такого sed не умеет.
#
# Беда, ради которой это написано. Раньше здесь менялся каждый найденный
# номер, и каждый выпуск переписывал заодно пояснения в тексте: «до v2.58.0
# версия не шла в документы» превращалось в «до v2.59.0», то есть в неправду
# ровно про то, что этот выпуск и сделал. За несколько выпусков так испортилось
# семь мест, и все пришлось восстанавливать руками по CHANGELOG.
#
# Правило: пояснения написаны по-русски, метки версии — нет. Если прямо перед
# номером (через пробел или скобку) стоит русская буква, это фраза о прошлом,
# и трогать её нельзя. «LiftTeam v2.59.0», «Launcher v2.59.0», «>v2.59.0<»
# и номер в начале строки — метки, их и меняем.

python3 - "$OLD_VERSION" "$NEW_VERSION" <<'PYEOF'
import pathlib
import re
import sys

old, new = sys.argv[1], sys.argv[2]
pattern = re.compile(r'(?<![\w.])' + re.escape(old) + r'(?![\w.])')
cyrillic = re.compile(r'[а-яёА-ЯЁ]')

FILES = ['lifteam_launcher.py', 'README.md', 'PROMPT.md', 'TZ.md', 'start.bat',
         'start.sh', 'push_to_github.bat', 'push_to_github.sh']
SUFFIXES = ('.py', '.html', '.css', '.js')

def is_prose(line, at):
    """Перед номером стоит русский текст — значит это фраза о прошлом."""
    head = line[:at].rstrip('([«"\' ')
    return bool(head) and bool(cyrillic.search(head[-1]))

def convert(path):
    text = path.read_text(encoding='utf-8')
    if old not in text:
        return 0
    changed = 0
    out = []
    for line in text.split('\n'):
        pieces, last = [], 0
        for m in pattern.finditer(line):
            if is_prose(line, m.start()):
                continue
            pieces.append(line[last:m.start()]); pieces.append(new)
            last = m.end(); changed += 1
        pieces.append(line[last:])
        out.append(''.join(pieces))
    if changed:
        path.write_text('\n'.join(out), encoding='utf-8')
    return changed

targets = [pathlib.Path(name) for name in FILES]
for root in ('core', 'lifteam'):
    targets += [p for p in pathlib.Path(root).rglob('*') if p.suffix in SUFFIXES]

total = kept = 0
for path in targets:
    if path.is_file():
        total += convert(path)

# Сколько упоминаний старого номера осталось — это пояснения, и так и надо
for path in targets:
    if path.is_file():
        for line in path.read_text(encoding='utf-8').split('\n'):
            for m in pattern.finditer(line):
                kept += 1

print(f'  Заменено меток: {total}. Оставлено пояснений о прошлом: {kept}.')
PYEOF

echo "  [OK] Версия обновлена до $NEW_VERSION."




