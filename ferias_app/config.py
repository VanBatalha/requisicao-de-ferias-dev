from __future__ import annotations

import os
from dataclasses import dataclass

def _env(name: str, default: str = "") -> str:
    v = os.getenv(name)
    return v if v is not None and v != "" else default

@dataclass(frozen=True)
class Settings:
    # Flask
    secret_key: str = _env("FLASK_SECRET_KEY", "uma_chave_bem_grande_e_fixa_aqui")

    # Smartsheet OAuth
    client_id: str = _env("SMARTSHEET_CLIENT_ID", "")
    client_secret: str = _env("SMARTSHEET_CLIENT_SECRET", "")
    redirect_uri: str = _env("SMARTSHEET_REDIRECT_URI", "http://localhost:5000/callback")
    scopes: str = _env("SMARTSHEET_SCOPES", "READ_SHEETS WRITE_SHEETS")

    # Smartsheet sheet IDs
    id_folha_cadastro: int = int(_env("ID_FOLHA_CADASTRO", "0") or "0")
    id_folha_solicitacoes: int = int(_env("ID_FOLHA_SOLICITACOES", "0") or "0")

    # Runtime
    environment: str = _env("ENVIRONMENT", _env("FLASK_ENV", "production"))

def get_settings() -> Settings:
    return Settings()
