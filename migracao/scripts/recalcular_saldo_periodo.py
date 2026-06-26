#!/usr/bin/env python3
"""Recalcula a tabela app_ferias.saldo_periodo e preenche solicitacoes_ferias.periodo_aquisitivo_origem.

Uso local/Render Shell:
    python recalcular_saldo_periodo.py

Variaveis relevantes:
    DATABASE_URL
    DB_SCHEMA=app_ferias
    APP_TIMEZONE=America/Fortaleza
    SYNC_REFERENCE_DATE=2026-06-24   # opcional
    SYNC_RECALC_BATCH_SIZE=25        # opcional
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")

from ferias_app import create_app
from ferias_app.services.smartsheet_sync_service import recalcular_saldo_periodo_from_db


def main() -> int:
    print("Recalculo de saldo_periodo")
    print(f"- DB_SCHEMA={os.getenv('DB_SCHEMA') or 'app_ferias'}")
    print(f"- SYNC_REFERENCE_DATE={os.getenv('SYNC_REFERENCE_DATE') or 'data atual do ambiente'}")
    print(f"- SYNC_RECALC_BATCH_SIZE={os.getenv('SYNC_RECALC_BATCH_SIZE') or '25'}")
    app = create_app()
    with app.app_context():
        result = recalcular_saldo_periodo_from_db()
        print("Recalculo concluido.")
        print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
