@echo off
chcp 65001 >nul
cd /d "%%~dp0"

echo.
echo   ================================================
echo    LiftTeam v2.78.0 — Push to GitHub
echo   ================================================
echo.

set REPO_URL=https://github.com/yachtsman13/LiftTeam.git

:: 1. Проверка Git
where git >nul 2>nul
if errorlevel 1 (
    echo   [ERROR] Git не найден
    pause
    exit /b 1
)

:: 2. Проверка интернет-соединения
ping -n 1 -w 3000 github.com >nul 2>nul
if errorlevel 1 (
    echo   [WARNING] GitHub недоступен. Проверьте интернет-соединение.
    echo   Продолжить локальный коммит? (Y/N)
    set /p continue=
    if /I not "%%continue%%"=="Y" exit /b 1
)

:: 3. Проверка репозитория
if not exist ".git" (
    echo   [1/5] Инициализация репозитория...
    git init
    git branch -m main
    git remote add origin %%REPO_URL%%
) else (
    echo   [OK] Репозиторий найден
)

:: 4. Настройка remote
echo   [2/5] Настройка remote...
git remote remove origin 2>nul
git remote add origin %%REPO_URL%%

:: 5. Проверка изменений
echo   [3/5] Проверка изменений...
git status --short

:: 6. Коммит
echo   [4/5] Создание коммита...
git add -A
git commit -m "LiftTeam v2.78.0 update" 2>nul

:: 7. Push
echo   [5/5] Push на GitHub...
git push -u origin main --force
if errorlevel 1 (
    echo   [ERROR] Push не удался
    echo   Проверьте токен и права доступа
    pause
    exit /b 1
)

echo.
echo   ================================================
echo    [OK] LiftTeam v2.78.0 успешно отправлен на GitHub
echo   ================================================
echo.
echo   URL: %%REPO_URL%%
echo.
pause
