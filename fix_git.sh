#!/bin/bash
# LiftTeam — Fix Git Issues (Linux/macOS)

cd "$(dirname "$0")"

echo ""
echo "  ================================================"
echo "   LiftTeam — Fix Git Issues"
echo "  ================================================"
echo ""

echo "  [1/4] Сброс safe.directory..."
git config --global --unset-all safe.directory 2>/dev/null || true

echo "  [2/4] Добавление текущей папки..."
git config --global --add safe.directory "$(pwd)"

echo "  [3/4] Сброс remote..."
git remote remove origin 2>/dev/null || true
git remote add origin https://github.com/yachtsman13/Lifteam.git

echo "  [4/4] Проверка..."
git remote -v

echo ""
echo "  [OK] Git настройки исправлены."




