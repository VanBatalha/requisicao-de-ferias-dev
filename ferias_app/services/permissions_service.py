from __future__ import annotations

from typing import List

from ..logging_config import get_logger
from ..utils import safe_lower

log = get_logger(__name__)


def _postgres_available() -> bool:
    try:
        from .postgres_compat_service import postgres_enabled
        return postgres_enabled()
    except Exception:
        return False


def get_user_type(email: str) -> str:
    """Retorna USER TYPE (ADMIN | DP | USER) exclusivamente do PostgreSQL.

    A comunicação com o Smartsheet fica restrita ao botão de sincronização da
    aba ADMIN. Falhas de permissão não disparam consultas externas.
    """
    email = safe_lower(email or "")
    if not email or not _postgres_available():
        return "USER"
    try:
        from .postgres_compat_service import get_user_type_postgres
        ut = get_user_type_postgres(email)
        log.info("Permissões(PostgreSQL): email=%s user_type=%s", email, ut)
        return ut
    except Exception as exc:
        log.warning("Falha ao consultar permissões no PostgreSQL para %s: %s", email, exc)
        return "USER"


def get_user_role(email: str) -> str:
    ut = get_user_type(email)
    if ut == "ADMIN":
        return "admin"
    if ut == "DP":
        return "DP"
    if is_gestor(email):
        return "gestor"
    return "user"


def tem_grupo(email: str, grupo: str) -> bool:
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
    email = safe_lower(email or "")
    if not email or not _postgres_available():
        return []
    try:
        from .postgres_compat_service import subordinados_do_gestor_postgres
        subs = subordinados_do_gestor_postgres(email) or []
        out = []
        for item in subs:
            em = safe_lower((item or {}).get("EMAIL DA EMPRESA") or (item or {}).get("email") or "")
            if em:
                out.append(em)
        return sorted(set(out))
    except Exception as exc:
        log.warning("Falha ao consultar subordinados no PostgreSQL para %s: %s", email, exc)
        return []


def is_gestor(email: str) -> bool:
    return len(_subordinados_emails(email)) > 0


def get_subordinados(email: str) -> List[str]:
    return _subordinados_emails(email)
