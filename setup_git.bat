@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo.
echo   ================================================
echo    LiftTeam — Setup Git Repository
echo   ================================================
echo.

set REPO_URL=https://github.com/yachtsman13/Lifteam.git

where git >nul 2>nul
if errorlevel 1 (
    echo   [ERROR] Git не найден
    pause
    exit /b 1
)

if not exist ".git" (
    echo   [1/3] Инициализация репозитория...
    git init
    git branch -m main
) else (
    echo   [OK] Репозиторий уже инициализирован
)

echo   [2/3] Настройка remote...
git remote remove origin 2>nul
git remote add origin %REPO_URL%

git config user.name "yachtsman13"
git config user.email "yachtsman13@github.com"

echo   [3/3] Первый коммит и push...
git add -A
git commit -m "LiftTeam v2.6.0 initial commit" 2>nul
git push -u origin main --force

echo.
echo   [OK] Готово. Репозиторий настроен.
pause




