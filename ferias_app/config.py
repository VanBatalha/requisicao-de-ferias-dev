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

    # Smartsheet
    # Método A (recomendado): backend usa um único token (conta de serviço)
    access_token: str = _env("SMARTSHEET_ACCESS_TOKEN", "")

    # OAuth (legado/opcional). Mantido para compatibilidade, mas pode ficar vazio.
    client_id: str = _env("SMARTSHEET_CLIENT_ID", "")
    client_secret: str = _env("SMARTSHEET_CLIENT_SECRET", "")
    redirect_uri: str = _env("SMARTSHEET_REDIRECT_URI", "http://localhost:5000/callback")
    scopes: str = _env("SMARTSHEET_SCOPES", "READ_SHEETS WRITE_SHEETS")

    # LDAP / Active Directory
    ldap_uri: str = _env("LDAP_URI", "")  # ex: ldap://10.0.0.10:389  |  ldaps://10.0.0.10:636
    ldap_base_dn: str = _env("LDAP_BASE_DN", "")  # ex: dc=empresa,dc=local
    ldap_bind_dn: str = _env("LDAP_BIND_DN", "")  # ex: cn=svc-app,ou=Users,dc=...,dc=...
    ldap_bind_password: str = _env("LDAP_BIND_PASSWORD", "")
    ldap_user_filter: str = _env("LDAP_USER_FILTER", "(sAMAccountName={username})")
    ldap_email_attr: str = _env("LDAP_EMAIL_ATTR", "mail")
    ldap_name_attr: str = _env("LDAP_NAME_ATTR", "displayName")
    ldap_memberof_attr: str = _env("LDAP_MEMBEROF_ATTR", "memberOf")
    ldap_verify_cert: str = _env("LDAP_VERIFY_CERT", "true")  # true/false

    # Smartsheet sheet IDs
    id_folha_cadastro: int = int(_env("ID_FOLHA_CADASTRO", "0") or "0")
    id_folha_solicitacoes: int = int(_env("ID_FOLHA_SOLICITACOES", "0") or "0")

    # Runtime
    environment: str = _env("ENVIRONMENT", _env("FLASK_ENV", "production"))

def get_settings() -> Settings:
    return Settings()
