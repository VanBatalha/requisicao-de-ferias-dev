#!/bin/sh
set -eu

APP_DIR=${APP_DIR:-/opt/app-ferias}
cd "$APP_DIR"

/usr/bin/flock -n /var/lock/app-ferias-periodos.lock \
  /usr/bin/docker compose exec -T app python daily_balance_accrual.py
