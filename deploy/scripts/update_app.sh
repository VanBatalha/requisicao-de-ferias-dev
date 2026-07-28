#!/bin/sh
set -eu

APP_DIR=${APP_DIR:-/opt/app-ferias}
cd "$APP_DIR"

git pull --ff-only
/usr/bin/docker compose build --pull app
/usr/bin/docker compose up -d
/usr/bin/docker compose ps
