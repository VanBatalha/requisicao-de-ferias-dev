from __future__ import annotations

from typing import List, Dict, Any

from ..logging_config import get_logger
from ..utils import safe_lower
from .auth_service import get_access_token
from .cadastro_service import get_user_type as _get_user_type, subordinados_do_gestor

log = get_logger(__name__)


def get_user_type(email: str) -> str:
    """USER TYPE vindo da planilha de cadastro (USER TYPE)."""
    token = get_access_token()
    if not token:
        return "USER"
    return _get_user_type(token, email)


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
    token = get_access_token()
    if not token:
        return []
    subs = subordinados_do_gestor(token, email) or []
    out: List[str] = []
    for s in subs:
        try:
            if isinstance(s, dict):
                em = safe_lower(s.get("email") or "")
            else:
                em = safe_lower(str(s))
            if em:
                out.append(em)
        except Exception:
            continue
    # unique + sorted
    return sorted(set(out))


def is_gestor(email: str) -> bool:
    return len(_subordinados_emails(email)) > 0


def get_subordinados(email: str) -> List[str]:
    """Emails dos subordinados (compat com a versão estável)."""
    return _subordinados_emails(email)
