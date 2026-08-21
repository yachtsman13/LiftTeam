@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo.
echo   ================================================
echo    LiftTeam v2.57.0 — Starting Docker Containers
echo   ================================================
echo.

where docker >nul 2>nul
if errorlevel 1 (
    echo   [ERROR] Docker not found
    echo   Install: https://docs.docker.com/engine/install/
    pause
    exit /b 1
)

docker info >nul 2>nul
if errorlevel 1 (
    echo   [ERROR] Docker daemon not running
    echo   Start Docker Desktop
    pause
    exit /b 1
)

if exist "docker-compose.yml" (
    set COMPOSE_FILE=docker-compose.yml
    echo   [OK] Using standard configuration
) else (
    echo   [ERROR] docker-compose.yml not found
    pause
    exit /b 1
)

echo   [1/4] Stopping old containers...
docker compose -f %COMPOSE_FILE% down --remove-orphans >nul 2>nul

echo   [2/4] Building and starting containers...
docker compose -f %COMPOSE_FILE% up --build -d
if errorlevel 1 (
    echo   [ERROR] Failed to start containers
    echo   Trying to force remove old containers...
    docker rm -f lifteam_db lifteam_redis lifteam_web lifteam_nginx >nul 2>nul
    docker compose -f %COMPOSE_FILE% up --build -d
)

echo   [3/4] Waiting for database...
set /a count=0
:wait_loop
docker compose -f %COMPOSE_FILE% exec -T db pg_isready -U lifteam >nul 2>nul
if not errorlevel 1 (
    echo         PostgreSQL ready
    goto db_ready
)
set /a count+=1
if %count% GTR 60 goto db_timeout
echo         attempt %count%/60
timeout /t 1 /nobreak >nul
goto wait_loop

:db_timeout
echo   [WARNING] DB timeout, continuing...

:db_ready
echo   [4/4] Applying migrations and init...
docker compose -f %COMPOSE_FILE% exec -T web python manage.py migrate --noinput >nul 2>nul
docker compose -f %COMPOSE_FILE% exec -T web python manage.py init_cells >nul 2>nul
docker compose -f %COMPOSE_FILE% exec -T web python manage.py create_admin --username admin --name "Administrator" --password admin123 --force >nul 2>nul
docker compose -f %COMPOSE_FILE% exec -T web python manage.py collectstatic --noinput >nul 2>nul

echo.
echo   ================================================
echo    [OK] LiftTeam v2.57.0 is running
echo   ================================================
echo.
echo   Access:   http://localhost/login/
echo   Login:    admin
echo   Password: admin123
echo.
pause





