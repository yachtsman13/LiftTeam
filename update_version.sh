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
OLD_VERSION=$(grep -m1 -oE '^## v[0-9]+\.[0-9]+\.[0-9]+(\.[0-9]+)*' CHANGELOG.md | sed 's/^## //')

if [ -z "$OLD_VERSION" ]; then
    echo "  [ОШИБКА] В CHANGELOG.md не найден заголовок текущей версии."
    echo "  Добавь запись о выпуске в CHANGELOG.md и повтори."
    exit 1
fi

if [ "$OLD_VERSION" = "$NEW_VERSION" ]; then
    echo "  [ОШИБКА] В CHANGELOG.md верхняя запись — уже $NEW_VERSION."
    echo "  Номер берётся из неё, менять нечего."
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




