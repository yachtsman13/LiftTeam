@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo.
echo   ================================================
echo    LiftTeam — Fix Git Issues
echo   ================================================
echo.

echo   [1/4] Сброс safe.directory...
git config --global --unset-all safe.directory 2>nul

echo   [2/4] Добавление текущей папки...
git config --global --add safe.directory "%CD%"

echo   [3/4] Сброс remote...
git remote remove origin 2>nul
git remote add origin https://github.com/yachtsman13/Lifteam.git

echo   [4/4] Проверка...
git remote -v

echo.
echo   [OK] Git настройки исправлены.
pause




