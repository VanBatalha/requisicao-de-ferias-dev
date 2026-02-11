from __future__ import annotations

from typing import Any, Dict, List, Optional

from flask import g

from ..config import get_settings
from ..logging_config import get_logger
from ..utils import safe_lower
from .smartsheet_service import get_sheet, columns_map

log = get_logger(__name__)

# Colunas esperadas no cadastro (títulos são tratados como case-insensitive)
COL_EMAIL = "EMAIL DA EMPRESA"
COL_GESTOR = "GESTOR"
COL_USER_TYPE = "USER TYPE"
COL_STATUS = "STATUS"

def _cached_get_cadastro_sheet(access_token: str):
    cached = getattr(g, "_cadastro_sheet_cache", None)
    if cached is not None:
        return cached
    s = get_settings()
    if not s.id_folha_cadastro:
        raise ValueError("ID_FOLHA_CADASTRO não configurado.")
    sheet = get_sheet(access_token, s.id_folha_cadastro)
    g._cadastro_sheet_cache = sheet
    return sheet

def listar_colaboradores(access_token: str) -> List[Dict[str, Any]]:
    sheet = _cached_get_cadastro_sheet(access_token)
    cmap = columns_map(sheet)

    def cell_value(row, col_name: str) -> str:
        cid = cmap.get(col_name.upper())
        if not cid:
            return ""
        for c in row.cells:
            if c.column_id == cid:
                return (c.display_value or c.value or "") if c is not None else ""
        return ""

    out: List[Dict[str, Any]] = []
    for r in sheet.rows:
        email = safe_lower(str(cell_value(r, COL_EMAIL)))
        if not email:
            continue
        out.append({
            "row_id": r.id,
            "email": email,
            "gestor": safe_lower(str(cell_value(r, COL_GESTOR))),
            "user_type": (str(cell_value(r, COL_USER_TYPE)) or "").strip().upper(),
            "status": (str(cell_value(r, COL_STATUS)) or "").strip().upper(),
        })
    return out

def get_user_row(access_token: str, email: str) -> Optional[Dict[str, Any]]:
    email = safe_lower(email)
    for c in listar_colaboradores(access_token):
        if safe_lower(c.get("email","")) == email:
            return c
    return None

def get_user_type(access_token: str, email: str) -> str:
    row = get_user_row(access_token, email)
    ut = (row.get("user_type") if row else "") or ""
    ut = ut.strip().upper()
    return ut if ut in ("ADMIN","DP","USER","GESTOR") else ut or "USER"

def is_ativo(access_token: str, email: str) -> bool:
    row = get_user_row(access_token, email)
    st = (row.get("status") if row else "") or ""
    st = st.strip().upper()
    return st in ("ATIVO","ACTIVE","1","SIM","YES","TRUE","OK","")

def subordinados_do_gestor(access_token: str, gestor_email: str) -> List[Dict[str, Any]]:
    gestor_email = safe_lower(gestor_email)
    return [c for c in listar_colaboradores(access_token) if safe_lower(c.get("gestor","")) == gestor_email and is_ativo(access_token, c.get("email",""))]
