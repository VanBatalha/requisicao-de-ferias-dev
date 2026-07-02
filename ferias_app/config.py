from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import quote_plus


def _strip_outer_quotes(value: str) -> str:
    text = str(value).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        return text[1:-1]
    return text


def _env(name: str, default: str = "") -> str:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return default
    return _strip_outer_quotes(str(v))


def _env_int(name: str, default: int) -> int:
    raw = _env(name, str(default))
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        print(f"Aviso: variavel {name} com valor invalido {raw!r}; usando {default}.")
        return default


def _build_pg_url(prefix: str = "") -> str:
    """Monta a URL PostgreSQL a partir de PG_* ou TEST_PG_*.

    Prefixo vazio usa PG_HOST/PG_PORT/PG_DB/PG_USER/PG_PASSWORD.
    Prefixo TEST_ usa TEST_PG_HOST/TEST_PG_PORT/TEST_PG_DB/TEST_PG_USER/TEST_PG_PASSWORD.
    """
    host = _env(f"{prefix}PG_HOST", _env("PG_HOST", "75.119.139.205") if not prefix else "")
    port = _env(f"{prefix}PG_PORT", _env("PG_PORT", "5532") if not prefix else "5432")
    db = _env(f"{prefix}PG_DB", _env("PG_DB", "db_appsheet") if not prefix else "")
    user = _env(f"{prefix}PG_USER", _env(f"{prefix}PG_USERNAME", _env("PG_USER", "") if not prefix else ""))
    password = _env(f"{prefix}PG_PASSWORD", _env("PG_PASSWORD", "") if not prefix else "")
    sslmode = _env(f"{prefix}PG_SSLMODE", _env("PG_SSLMODE", ""))

    if not all([host, port, db, user, password]):
        return ""

    url = f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{quote_plus(db)}"
    if sslmode:
        url += f"?sslmode={quote_plus(sslmode)}"
    return url


def _resolve_database_url() -> str:
    """Resolve a conexão do banco por perfil.

    DB_TARGET/oficial      -> usa PG_HOST/PG_PORT/PG_DB/PG_USER/PG_PASSWORD.
    DB_TARGET=teste_url    -> usa TEST_DATABASE_URL, com fallback para DATABASE_URL.
    DB_TARGET=teste_pg     -> usa TEST_PG_HOST/TEST_PG_PORT/TEST_PG_DB/TEST_PG_USER/TEST_PG_PASSWORD.
    DB_TARGET=database_url -> usa DATABASE_URL.

    Sem DB_TARGET mantém compatibilidade: usa DATABASE_URL se existir; caso contrário usa PG_*.
    """
    target = _env("DB_TARGET", "").strip().lower()

    if target in {"oficial", "official", "prod", "production", "pg", "pg_vars"}:
        return _build_pg_url("")
    if target in {"teste_url", "test_url", "render", "render_test", "render_teste"}:
        return _env("TEST_DATABASE_URL", _env("DATABASE_URL", ""))
    if target in {"teste_pg", "test_pg", "test_pg_vars", "teste_pg_vars"}:
        return _build_pg_url("TEST_")
    if target in {"database_url", "url", "legacy"}:
        return _env("DATABASE_URL", "")
    if target in {"teste", "test"}:
        return _env("TEST_DATABASE_URL", "") or _build_pg_url("TEST_") or _env("DATABASE_URL", "")

    return _env("DATABASE_URL", "") or _build_pg_url("")

@dataclass(frozen=True)
class Settings:
    # Flask
    secret_key: str = _env("FLASK_SECRET_KEY", "uma_chave_bem_grande_e_fixa_aqui")

    # PostgreSQL
    # O DB_TARGET permite alternar com segurança entre banco oficial e bancos de teste.
    # Veja documentacao/SINCRONIZACAO_BANCOS_OFICIAL_E_TESTE.md.
    db_target: str = _env("DB_TARGET", "auto")
    database_url: str = _resolve_database_url()

    # Smartsheet (legado - mantido para compatibilidade, mas não usado mais)
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
    # Opcional: lista de filtros separados por ";" ou "," (tentados em ordem)
    ldap_user_filters: str = _env("LDAP_USER_FILTERS", "")
    # Opcional: formato para bind de autenticação (ex: "{username}@empresa.local" ou "EMPRESA\\{username}")
    ldap_auth_bind_format: str = _env("LDAP_AUTH_BIND_FORMAT", "")
    ldap_email_attr: str = _env("LDAP_EMAIL_ATTR", "mail")
    ldap_name_attr: str = _env("LDAP_NAME_ATTR", "displayName")
    ldap_memberof_attr: str = _env("LDAP_MEMBEROF_ATTR", "memberOf")
    ldap_verify_cert: str = _env("LDAP_VERIFY_CERT", "true")  # true/false
    ldap_starttls: str = _env("LDAP_STARTTLS", "false")  # true/false (para ldap:// na porta 389)

    # Smartsheet sheet IDs (legado - não mais usado com PostgreSQL)
    # Defaults iguais à versão estável (evita "sheet_id=0" quando a env não está setada no Render)
    # Você ainda pode sobrescrever no Render com as variáveis de ambiente.
    # Planilha principal de cadastro de colaboradores.
    # A sincronização atual usa a folha CADASTRO DE COLABORADORES (1745799836133252).
    # A folha CONTROLE_DP (3609445264215940) deixou de ser fonte cadastral;
    # permissões ficam no PostgreSQL em permissoes_usuario e saldos ficam em saldo_periodo.
    id_folha_cadastro_principal: int = _env_int("ID_FOLHA_CADASTRO_PRINCIPAL", 1745799836133252)
    # Mantido apenas por compatibilidade com trechos legados/fallbacks.
    id_folha_cadastro: int = _env_int("ID_FOLHA_CADASTRO", 1745799836133252)
    id_folha_solicitacoes: int = _env_int("ID_FOLHA_SOLICITACOES", 0)

    # Runtime
    environment: str = _env("ENVIRONMENT", _env("FLASK_ENV", "production"))

    # Timezone de negocio do app. O Render registra logs em UTC, mas as conexoes
    # PostgreSQL e o scheduler podem operar no fuso da empresa.
    app_timezone: str = _env("APP_TIMEZONE", "America/Fortaleza")

def get_settings() -> Settings:
    return Settings()