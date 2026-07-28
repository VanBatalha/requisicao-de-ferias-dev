#!/bin/sh
set -eu

APP_DIR=${APP_DIR:-/opt/app-ferias}
BACKUP_DIR=${BACKUP_DIR:-$APP_DIR/backups}
RETENTION_DAYS=${RETENTION_DAYS:-30}

cd "$APP_DIR"
set -a
. ./.env
set +a

mkdir -p "$BACKUP_DIR"
STAMP=$(date +%Y%m%d_%H%M%S)
FILE="$BACKUP_DIR/ferias_app_${STAMP}.dump"

/usr/bin/docker compose exec -T db pg_dump \
  --format=custom \
  --no-owner \
  --no-privileges \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" > "$FILE"

find "$BACKUP_DIR" -type f -name 'ferias_app_*.dump' -mtime "+$RETENTION_DAYS" -delete
printf '%s\n' "$FILE"
