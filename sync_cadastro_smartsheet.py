#!/usr/bin/env python3
"""Executa sincronização Cadastro Smartsheet -> PostgreSQL.

Uso local/Render Cron Job:
    python sync_cadastro_smartsheet.py

Variáveis necessárias no ambiente:
    DATABASE_URL
    SMARTSHEET_ACCESS_TOKEN
    ID_FOLHA_CADASTRO=3609445264215940
    DB_SCHEMA=ferias_app
"""
from __future__ import annotations

from ferias_app import create_app
from ferias_app.services.smartsheet_sync_service import sync_cadastro_from_smartsheet


def main() -> int:
    app = create_app()
    with app.app_context():
        result = sync_cadastro_from_smartsheet(triggered_by="cron", actor_email="cron", recalculate=True)
        print("Sincronização de cadastro concluída.")
        print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
