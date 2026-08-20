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

# Точки в номере экранируются, иначе они значат «любой знак».
OLD_PATTERN=$(printf '%s' "$OLD_VERSION" | sed 's/\./\\./g')

for FILE in lifteam_launcher.py README.md PROMPT.md TZ.md start.bat start.sh \
            push_to_github.bat push_to_github.sh; do
    [ -f "$FILE" ] && sed -i "s/$OLD_PATTERN/$NEW_VERSION/g" "$FILE"
done

# Версия в заголовках файлов в core/ и lifteam/
find core lifteam -type f \( -name '*.py' -o -name '*.html' -o -name '*.css' -o -name '*.js' \) \
    -exec sed -i "s/$OLD_PATTERN/$NEW_VERSION/g" {} +

echo "  [OK] Версия обновлена до $NEW_VERSION."




