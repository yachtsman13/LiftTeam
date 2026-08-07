@echo off
chcp 65001 >nul
cd /d "%~dp0"

if "%~1"=="" (
    echo   Использование: update_version.bat [ВЕРСИЯ]
    echo   Пример: update_version.bat v2.6.0
    pause
    exit /b 1
)

set NEW_VERSION=%~1

echo.
echo   ================================================
echo    LiftTeam — Update Version to %NEW_VERSION%
echo   ================================================
echo.

:: CHANGELOG.md намеренно НЕ обрабатывается: замена по всему файлу переписала бы
:: заголовки всех прошлых выпусков на новый номер и уничтожила историю версий.
:: Запись о новой версии добавляется в него вручную.
powershell -Command "(Get-Content lifteam_launcher.py) -replace 'v[0-9]+\.[0-9]+\.[0-9]+([\.[0-9]+)*', '%NEW_VERSION%' | Set-Content lifteam_launcher.py"
powershell -Command "(Get-Content README.md) -replace 'v[0-9]+\.[0-9]+\.[0-9]+([\.[0-9]+)*', '%NEW_VERSION%' | Set-Content README.md"
powershell -Command "(Get-Content PROMPT.md) -replace 'v[0-9]+\.[0-9]+\.[0-9]+([\.[0-9]+)*', '%NEW_VERSION%' | Set-Content PROMPT.md"
powershell -Command "(Get-Content TZ.md) -replace 'v[0-9]+\.[0-9]+\.[0-9]+([\.[0-9]+)*', '%NEW_VERSION%' | Set-Content TZ.md"
powershell -Command "(Get-Content start.bat) -replace 'v[0-9]+\.[0-9]+\.[0-9]+([\.[0-9]+)*', '%NEW_VERSION%' | Set-Content start.bat"
powershell -Command "(Get-Content start.sh) -replace 'v[0-9]+\.[0-9]+\.[0-9]+([\.[0-9]+)*', '%NEW_VERSION%' | Set-Content start.sh"
powershell -Command "(Get-Content push_to_github.bat) -replace 'v[0-9]+\.[0-9]+\.[0-9]+([\.[0-9]+)*', '%NEW_VERSION%' | Set-Content push_to_github.bat"
powershell -Command "(Get-Content push_to_github.sh) -replace 'v[0-9]+\.[0-9]+\.[0-9]+([\.[0-9]+)*', '%NEW_VERSION%' | Set-Content push_to_github.sh"
powershell -Command "Get-ChildItem -Path 'core' -Recurse -Include '*.py','*.html','*.css','*.js' | ForEach-Object { (Get-Content $_.FullName) -replace 'v[0-9]+\.[0-9]+\.[0-9]+([\.[0-9]+)*', '%NEW_VERSION%' | Set-Content $_.FullName }"

echo   [OK] Версия обновлена до %NEW_VERSION%.
pause




