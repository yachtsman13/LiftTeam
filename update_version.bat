@echo off
chcp 65001 >nul
cd /d "%~dp0"

if "%~1"=="" (
    echo   Использование: update_version.bat [ВЕРСИЯ]
    echo   Пример: update_version.bat v2.8.0
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
::
:: Файлы читаются и пишутся как UTF-8 без BOM явно. Set-Content по умолчанию
:: пишет в кодировке системы, и русский текст в шаблонах превратился бы
:: в нечитаемый набор символов.
powershell -NoProfile -Command ^
  "$ver = '%NEW_VERSION%';" ^
  "$pattern = 'v[0-9]+\.[0-9]+\.[0-9]+(\.[0-9]+)*';" ^
  "$enc = New-Object System.Text.UTF8Encoding($false);" ^
  "$files = @('lifteam_launcher.py','README.md','PROMPT.md','TZ.md','start.bat','start.sh','push_to_github.bat','push_to_github.sh') | Where-Object { Test-Path $_ } | ForEach-Object { (Resolve-Path $_).Path };" ^
  "$files += Get-ChildItem -Path 'core','lifteam' -Recurse -Include '*.py','*.html','*.css','*.js' | ForEach-Object { $_.FullName };" ^
  "foreach ($f in $files) {" ^
  "  $text = [System.IO.File]::ReadAllText($f);" ^
  "  $new = [regex]::Replace($text, $pattern, $ver);" ^
  "  if ($new -ne $text) { [System.IO.File]::WriteAllText($f, $new, $enc) }" ^
  "}"

echo   [OK] Версия обновлена до %NEW_VERSION%.
pause
