#!/bin/bash
# LiftTeam v2.43.0 — Push to GitHub (единый скрипт)

cd "$(dirname "$0")"

REPO_URL="https://github.com/yachtsman13/Lifteam.git"

echo ""
echo "  ================================================"
echo "   LiftTeam v2.43.0 — Push to GitHub"
echo "  ================================================"
echo ""

# 1. Проверка Git
if ! command -v git &> /dev/null; then
    echo "  [ERROR] Git не найден"
    exit 1
fi

# 2. Проверка интернет-соединения
if ! ping -c 1 -W 3 github.com &> /dev/null; then
    echo "  [WARNING] GitHub недоступен. Проверьте интернет-соединение."
    read -p "  Продолжить локальный коммит? (y/N): " continue
    if [[ ! "$continue" =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

# 3. Проверка репозитория
if [ ! -d ".git" ]; then
    echo "  [1/5] Инициализация репозитория..."
    git init
    git branch -m main
    git remote add origin "$REPO_URL"
else
    echo "  [OK] Репозиторий найден"
fi

# 4. Настройка remote
echo "  [2/5] Настройка remote..."
git remote remove origin 2>/dev/null || true
git remote add origin "$REPO_URL"

# 5. Проверка изменений
echo "  [3/5] Проверка изменений..."
git status --short

# 6. Коммит
echo "  [4/5] Создание коммита..."
git add -A
git commit -m "LiftTeam v2.43.0 update" 2>/dev/null || true

# 7. Push
echo "  [5/5] Push на GitHub..."
if git push -u origin main --force; then
    echo ""
    echo "  ================================================"
    echo "   [OK] LiftTeam v2.43.0 успешно отправлен на GitHub"
    echo "  ================================================"
    echo ""
    echo "  URL: $REPO_URL"
    echo ""
else
    echo "  [ERROR] Push не удался"
    echo "  Проверьте токен и права доступа"
    exit 1
fi
