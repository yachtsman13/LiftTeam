@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo.
echo   ================================================
echo    LiftTeam — Update from GitHub
echo   ================================================
echo.

git fetch origin main
git reset --hard origin/main

echo.
echo   [OK] Проект обновлён до последней версии с GitHub.
pause




