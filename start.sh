#!/bin/bash
# LiftTeam v2.41.0 — Start production server (Linux / Raspberry Pi)

cd "$(dirname "$0")"

set -e

echo ""
echo "  ================================================"
echo "   LiftTeam v2.41.0 — Starting Docker Containers"
echo "  ================================================"
echo ""

if ! command -v docker &> /dev/null
then
    echo "  [ERROR] Docker not found"
    echo "  Install: https://docs.docker.com/engine/install/"
    exit 1
fi

if ! docker info &> /dev/null
then
    echo "  [ERROR] Docker daemon not running"
    echo "  Start: sudo systemctl start docker"
    exit 1
fi

echo "  [OK] Docker is running"
echo ""

if [ -f "docker-compose.raspberry.yml" ]
then
    COMPOSE_FILE="docker-compose.raspberry.yml"
    echo "  [OK] Using Raspberry Pi configuration"
elif [ -f "docker-compose.yml" ]
then
    COMPOSE_FILE="docker-compose.yml"
    echo "  [OK] Using standard configuration"
else
    echo "  [ERROR] docker-compose.yml not found"
    exit 1
fi

echo "  [1/4] Stopping old containers..."
docker compose -f "$COMPOSE_FILE" down &> /dev/null || true

echo "  [2/4] Building and starting containers..."
docker compose -f "$COMPOSE_FILE" up --build -d

echo "  [3/4] Waiting for database..."
for i in $(seq 1 60)
do
    if docker compose -f "$COMPOSE_FILE" exec -T db pg_isready -U lifteam &> /dev/null
    then
        echo "        PostgreSQL ready"
        break
    fi
    echo "        attempt $i/60"
    sleep 1
done

echo "  [4/4] Applying migrations and init..."
docker compose -f "$COMPOSE_FILE" exec -T web python manage.py migrate --noinput &> /dev/null || true
docker compose -f "$COMPOSE_FILE" exec -T web python manage.py init_cells &> /dev/null || true
docker compose -f "$COMPOSE_FILE" exec -T web python manage.py create_admin --username admin --name "Administrator" --password admin123 --force &> /dev/null || true
docker compose -f "$COMPOSE_FILE" exec -T web python manage.py collectstatic --noinput &> /dev/null || true

echo ""
echo "  ================================================"
echo "   [OK] LiftTeam v2.41.0 is running"
echo "  ================================================"
echo ""
echo "  Access:   http://localhost/login/"
echo "  Network:  http://$(hostname -I | awk '{print $1}')/login/"
echo ""
echo "  Login:    admin"
echo "  Password: admin123"
echo ""
echo "  Commands:"
echo "    Logs:   docker compose -f $COMPOSE_FILE logs -f"
echo "    Stop:   docker compose -f $COMPOSE_FILE down"
echo "    Backup: docker compose -f $COMPOSE_FILE exec db pg_dump -U lifteam lifteam > backup.sql"
echo




