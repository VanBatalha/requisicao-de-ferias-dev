#!/usr/bin/env python3
"""Executa sincronização Cadastro Smartsheet -> PostgreSQL.

Uso local/Render Cron Job:
    python sync_cadastro_smartsheet.py

Variáveis necessárias no ambiente:
    DATABASE_URL
    SMARTSHEET_ACCESS_TOKEN
    ID_FOLHA_CADASTRO_PRINCIPAL=1745799836133252
    DB_SCHEMA=app_ferias
"""
from __future__ import annotations

import os
from pathlib import Path
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env", override=True)
from ferias_app import create_app
from ferias_app.services.smartsheet_sync_service import sync_cadastro_from_smartsheet


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "sim", "s", "yes", "y"}


def main() -> int:
    # V28: por padrão, se ID_FOLHA_SOLICITACOES estiver configurado,
    # sincroniza também solicitações/ajustes e recalcula saldos/períodos.
    include_solicitacoes = _truthy(os.getenv("INCLUDE_SOLICITACOES", "true")) and bool(os.getenv("ID_FOLHA_SOLICITACOES"))
    recalculate = _truthy(os.getenv("RECALCULATE_SALDOS", "true"))

    print("Configuração da sincronização:")
    print(f"- DB_TARGET={os.getenv('DB_TARGET') or 'auto'}")
    print(f"- DB_SCHEMA={os.getenv('DB_SCHEMA') or 'app_ferias'}")
    print(f"- INCLUDE_SOLICITACOES={include_solicitacoes}")
    print(f"- RECALCULATE_SALDOS={recalculate}")
    print(f"- SYNC_REFERENCE_DATE={os.getenv('SYNC_REFERENCE_DATE') or 'data atual do ambiente'}")

    app = create_app(run_db_migrations=True)
    with app.app_context():
        result = sync_cadastro_from_smartsheet(
            triggered_by="cron",
            actor_email="cron",
            recalculate=recalculate,
            include_solicitacoes=include_solicitacoes,
        )
        print("Sincronização concluída.")
        print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
