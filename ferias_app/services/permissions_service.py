from __future__ import annotations

from typing import List, Dict, Any

from ..logging_config import get_logger
from ..utils import safe_lower
from .auth_service import get_access_token
from .cadastro_service import get_user_type as _get_user_type, subordinados_do_gestor

log = get_logger(__name__)


def _postgres_available() -> bool:
    try:
        from .postgres_compat_service import postgres_enabled
        return postgres_enabled()
    except Exception:
        return False


def get_user_type(email: str) -> str:
    """Retorna USER TYPE (ADMIN | DP | USER).

    Prioridade:
    1. PostgreSQL, quando DATABASE_URL está configurada.
    2. Smartsheet legado, como fallback.
    """
    email = safe_lower(email or "")
    if not email:
        return "USER"

    if _postgres_available():
        try:
            from .postgres_compat_service import get_user_type_postgres
            ut = get_user_type_postgres(email)
            log.info("Permissões(PostgreSQL): email=%s user_type=%s", email, ut)
            return ut
        except Exception as exc:
            log.warning("Falha ao consultar permissões no PostgreSQL para %s: %s", email, exc)

    token = get_access_token()
    if not token:
        log.warning("Permissões: SMARTSHEET_ACCESS_TOKEN ausente; usando USER para %s", email)
        return "USER"
    ut = _get_user_type(token, email)
    log.info("Permissões(Smartsheet): email=%s user_type=%s", email, ut)
    return ut


def get_user_role(email: str) -> str:
    """Role usada na UI/menus.

    - ADMIN -> admin
    - DP -> DP
    - caso contrário, se tiver subordinados -> gestor
    - senão -> user
    """
    ut = get_user_type(email)
    if ut == "ADMIN":
        return "admin"
    if ut == "DP":
        return "DP"
    if is_gestor(email):
        return "gestor"
    return "user"


def tem_grupo(email: str, grupo: str) -> bool:
    """Compat com o legado (grupos: Administrador, DP, Gestor)."""
    grupo = (grupo or "").strip().lower()
    role = get_user_role(email)

    if grupo in ("administrador", "admin"):
        return role == "admin"
    if grupo == "dp":
        return role in ("DP", "admin")
    if grupo in ("gestor", "gestores"):
        return role in ("gestor", "DP", "admin")

    return False

def _subordinados_emails(email: str) -> List[str]:
    """Retorna emails dos subordinados de um gestor (apenas ATIVOS)."""
    email = safe_lower(email or "")
    if not email:
        return []
    
    if _postgres_available():
        try:
            from .postgres_compat_service import subordinados_do_gestor_postgres
            subs = subordinados_do_gestor_postgres(email) or []
            out = []
            for s in subs:
                em = safe_lower((s or {}).get("EMAIL DA EMPRESA") or (s or {}).get("email") or "")
                status = safe_lower((s or {}).get("STATUS") or "ativo")
                
                # ⚠️ CRÍTICO: só considera subordinados ATIVOS
                if em and status in ("ativo", "atv"):
                    out.append(em)
            return sorted(set(out))
        except Exception as exc:
            log.warning("Falha ao consultar subordinados no PostgreSQL para %s: %s", email, exc)
    
    # Fallback para Smartsheet (mantém lógica antiga)
    token = get_access_token()
    if not token:
        return []
    subs = subordinados_do_gestor(token, email) or []
    out: List[str] = []
    for s in subs:
        try:
            if isinstance(s, dict):
                em = safe_lower(s.get("email") or s.get("EMAIL DA EMPRESA") or "")
                status = safe_lower(s.get("STATUS") or "ativo")
                if em and status in ("ativo", "atv"):
                    out.append(em)
        except Exception:
            continue
    return sorted(set(out))

def is_gestor(email: str) -> bool:
    return len(_subordinados_emails(email)) > 0


def get_subordinados(email: str) -> List[str]:
    """Emails dos subordinados (compat com a versão estável)."""
    return _subordinados_emails(email)
