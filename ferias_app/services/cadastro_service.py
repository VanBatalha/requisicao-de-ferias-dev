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
COL_USER_TYPE = "USER TYPE"
COL_STATUS = "STATUS"

# Relação gestor -> subordinados (conforme versão estável)
COL_GESTOR_DIRETO = "GESTOR DIRETO"
COL_GESTOR_SUPERIOR = "GESTOR SUPERIOR"
# fallback legado
COL_GESTOR = "GESTOR"


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


def _cell_value(sheet, row, cmap: Dict[str, int], col_name: str) -> str:
    cid = cmap.get((col_name or "").upper())
    if not cid:
        return ""
    for c in row.cells:
        if c.column_id == cid:
            return (c.display_value or c.value or "") if c is not None else ""
    return ""


def _email_local(email: str) -> str:
    """Retorna a parte antes do @ para heurísticas de matching."""
    email = safe_lower(email or "")
    if "@" in email:
        return email.split("@", 1)[0].strip()
    return email.strip()


def listar_colaboradores(access_token: str) -> List[Dict[str, Any]]:
    """Lista colaboradores a partir da planilha de cadastro.

    Obs.: Este serviço é usado principalmente para permissões (USER TYPE) e
    para relação Gestor->Subordinados (GESTOR DIRETO / GESTOR SUPERIOR).
    """
    sheet = _cached_get_cadastro_sheet(access_token)
    cmap = columns_map(sheet)

    out: List[Dict[str, Any]] = []
    for r in sheet.rows:
        email = safe_lower(str(_cell_value(sheet, r, cmap, COL_EMAIL)))
        if not email:
            continue

        ut = (str(_cell_value(sheet, r, cmap, COL_USER_TYPE)) or "").strip().upper()
        st = (str(_cell_value(sheet, r, cmap, COL_STATUS)) or "").strip().upper()

        gestor_direto = safe_lower(str(_cell_value(sheet, r, cmap, COL_GESTOR_DIRETO)))
        gestor_superior = safe_lower(str(_cell_value(sheet, r, cmap, COL_GESTOR_SUPERIOR)))
        gestor_fallback = safe_lower(str(_cell_value(sheet, r, cmap, COL_GESTOR)))

        out.append(
            {
                "row_id": r.id,
                "email": email,
                "email_local": _email_local(email),
                "user_type": ut,
                "status": st,
                "gestor_direto": gestor_direto,
                "gestor_superior": gestor_superior,
                "gestor": gestor_fallback,
            }
        )
    return out


def get_user_row(access_token: str, email: str) -> Optional[Dict[str, Any]]:
    """Localiza o usuário no cadastro.

    1) tenta match exato por email
    2) fallback por local-part (antes do @) caso LDAP devolva domínio diferente
    """
    email = safe_lower(email)
    if not email:
        return None

    wanted_local = _email_local(email)
    candidates = listar_colaboradores(access_token)

    # 1) exato
    for c in candidates:
        if safe_lower(c.get("email", "")) == email:
            return c

    # 2) fallback local-part
    if wanted_local:
        matches = [c for c in candidates if c.get("email_local") == wanted_local]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            # se houver múltiplos, tenta preferir o mesmo domínio do email informado
            dom = email.split("@", 1)[1] if "@" in email else ""
            for c in matches:
                em = c.get("email", "")
                if dom and em.endswith("@" + dom):
                    return c
            return matches[0]

    return None


def get_user_type(access_token: str, email: str) -> str:
    """Retorna o USER TYPE (ADMIN | DP | USER) baseado na coluna USER TYPE."""
    row = get_user_row(access_token, email)
    ut = (row.get("user_type") if row else "") or ""
    ut = ut.strip().upper()

    if ut in ("ADMIN", "DP", "USER"):
        return ut
    if ut in ("USUARIO", "USUÁRIO"):
        return "USER"
    return "USER"


def is_ativo(access_token: str, email: str) -> bool:
    row = get_user_row(access_token, email)
    st = (row.get("status") if row else "") or ""
    st = st.strip().upper()
    # comportamento da versão estável: se não existir status, assume ativo
    if st == "":
        return True
    return st in ("ATIVO", "ACTIVE", "1", "SIM", "YES", "TRUE", "OK")


def subordinados_do_gestor(access_token: str, gestor_email: str) -> List[Dict[str, Any]]:
    """Retorna subordinados do gestor seguindo a regra da versão estável:

    - Se usuário logado for DP e o colaborador tiver GESTOR SUPERIOR = "dp" -> entra
    - Se GESTOR SUPERIOR = gestor_email -> entra
    - Se GESTOR DIRETO (fallback GESTOR) = gestor_email -> entra
    - Filtra somente ativos
    """
    gestor_email = safe_lower(gestor_email)
    if not gestor_email:
        return []

    ut = get_user_type(access_token, gestor_email)
    is_dp_user = ut == "DP"

    out: List[Dict[str, Any]] = []
    seen = set()

    for c in listar_colaboradores(access_token):
        try:
            colab_email = safe_lower(c.get("email") or "")
            if not colab_email or colab_email == gestor_email:
                continue
            if colab_email in seen:
                continue

            gestor_direto = safe_lower(c.get("gestor_direto") or c.get("gestor") or "")
            gestor_superior = safe_lower(c.get("gestor_superior") or "")

            match = False
            if is_dp_user and gestor_superior == "dp":
                match = True
            elif gestor_superior and gestor_superior == gestor_email:
                match = True
            elif gestor_direto and gestor_direto == gestor_email:
                match = True

            if not match:
                continue

            if not is_ativo(access_token, colab_email):
                continue

            seen.add(colab_email)
            out.append(c)
        except Exception:
            continue

    out.sort(key=lambda x: x.get("email", ""))
    return out
