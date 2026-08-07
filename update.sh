#!/bin/bash
# LiftTeam — Update from GitHub (Linux/macOS)

cd "$(dirname "$0")"

echo ""
echo "  ================================================"
echo "   LiftTeam — Update from GitHub"
echo "  ================================================"
echo ""

git fetch origin main
git reset --hard origin/main

echo ""
echo "  [OK] Проект обновлён до последней версии с GitHub."




