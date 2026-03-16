from __future__ import annotations

from typing import List, Dict, Any

from flask import session

from ..logging_config import get_logger
from ..utils import safe_lower
from .auth_service import get_access_token
from .cadastro_service import get_user_type as _get_user_type, subordinados_do_gestor

log = get_logger(__name__)

def get_user_type(email: str) -> str:
    token = get_access_token()
    if not token:
        return "USER"
    return _get_user_type(token, email)

def get_user_role(email: str) -> str:
    ut = get_user_type(email)
    if ut == "ADMIN":
        return "admin"
    if ut == "DP":
        return "DP"
    # gestor: se tiver subordinados ativos
    if is_gestor(email):
        return "gestor"
    return "user"

def tem_grupo(email: str, grupo: str) -> bool:
    grupo = (grupo or "").strip().lower()
    role = get_user_role(email)
    if grupo in ("administrador","admin"):
        return role == "admin"
    if grupo == "dp":
        return role in ("DP","admin")
    if grupo in ("gestor","gestores"):
        return role in ("gestor","DP","admin")
    return False

def is_gestor(email: str) -> bool:
    token = get_access_token()
    if not token:
        return False
    subs = subordinados_do_gestor(token, email)
    return len(subs) > 0

def get_subordinados(email: str) -> List[Dict[str, Any]]:
    token = get_access_token()
    if not token:
        return []
    return subordinados_do_gestor(token, email)
