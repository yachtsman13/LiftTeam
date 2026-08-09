#!/bin/bash
# LiftTeam — Setup Git Repository (Linux/macOS)

cd "$(dirname "$0")"

REPO_URL="https://github.com/yachtsman13/Lifteam.git"

echo ""
echo "  ================================================"
echo "   LiftTeam — Setup Git Repository"
echo "  ================================================"
echo ""

if ! command -v git &> /dev/null; then
    echo "  [ERROR] Git не найден"
    exit 1
fi

if [ ! -d ".git" ]; then
    echo "  [1/3] Инициализация репозитория..."
    git init
    git branch -m main
else
    echo "  [OK] Репозиторий уже инициализирован"
fi

echo "  [2/3] Настройка remote..."
git remote remove origin 2>/dev/null || true
git remote add origin "$REPO_URL"

git config user.name "yachtsman13"
git config user.email "yachtsman13@github.com"

echo "  [3/3] Первый коммит и push..."
git add -A
git commit -m "LiftTeam v2.6.3 initial commit" 2>/dev/null || true
git push -u origin main --force

echo ""
echo "  [OK] Готово. Репозиторий настроен."




