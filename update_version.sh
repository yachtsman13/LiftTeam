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
sed -i "s/v[0-9]\+\.[0-9]\+\.[0-9]\+\(\.[0-9]\+\)*/$NEW_VERSION/g" lifteam_launcher.py
sed -i "s/v[0-9]\+\.[0-9]\+\.[0-9]\+\(\.[0-9]\+\)*/$NEW_VERSION/g" README.md
sed -i "s/v[0-9]\+\.[0-9]\+\.[0-9]\+\(\.[0-9]\+\)*/$NEW_VERSION/g" PROMPT.md
sed -i "s/v[0-9]\+\.[0-9]\+\.[0-9]\+\(\.[0-9]\+\)*/$NEW_VERSION/g" TZ.md
sed -i "s/v[0-9]\+\.[0-9]\+\.[0-9]\+\(\.[0-9]\+\)*/$NEW_VERSION/g" start.bat
sed -i "s/v[0-9]\+\.[0-9]\+\.[0-9]\+\(\.[0-9]\+\)*/$NEW_VERSION/g" start.sh
sed -i "s/v[0-9]\+\.[0-9]\+\.[0-9]\+\(\.[0-9]\+\)*/$NEW_VERSION/g" push_to_github.bat
sed -i "s/v[0-9]\+\.[0-9]\+\.[0-9]\+\(\.[0-9]\+\)*/$NEW_VERSION/g" push_to_github.sh

# Обновляем версию во всех .py, .html, .css, .js в core/ и lifteam/
# (в lifteam/ версия стоит в заголовках файлов и без этого оставалась старой)
find core lifteam -type f \( -name '*.py' -o -name '*.html' -o -name '*.css' -o -name '*.js' \) -exec sed -i "s/v[0-9]\+\.[0-9]\+\.[0-9]\+\(\.[0-9]\+\)*/$NEW_VERSION/g" {} +

echo "  [OK] Версия обновлена до $NEW_VERSION."




