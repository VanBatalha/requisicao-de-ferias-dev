from __future__ import annotations

from typing import Any, Dict, List, Optional

from flask import g

from ..config import get_settings
from ..logging_config import get_logger
from ..utils import safe_lower
from .identity_service import email_local_part, emails_equivalentes, normalize_email_identity
from .normalization_service import norm_title
from .smartsheet_service import get_sheet, columns_map

log = get_logger(__name__)

# Colunas esperadas no cadastro (títulos são tratados como case-insensitive
# e, quando possível, normalizados para tolerar acentos/underscore).
COL_EMAIL = "EMAIL DA EMPRESA"
COL_USER_TYPE = "USER TYPE"
COL_STATUS = "STATUS"

EMAIL_ALIASES = ("EMAIL DA EMPRESA", "EMAIL", "E-MAIL", "EMAIL EMPRESA", "E-MAIL DA EMPRESA")
USER_TYPE_ALIASES = ("USER TYPE", "TIPO USUARIO", "TIPO USUÁRIO", "PERFIL")
STATUS_ALIASES = ("STATUS", "SITUAÇÃO", "SITUACAO")


def normalizar_user_type(value: object) -> str:
    """Normaliza a coluna USER TYPE para ADMIN | DP | USER.

    Fonte oficial de permissões: Smartsheet, planilha CONTROLE_DP, coluna
    USER TYPE. O LDAP autentica/identifica o usuário, mas não define o perfil
    funcional da aplicação.
    """
    raw = str(value or "").strip()
    if not raw:
        return "USER"

    n = norm_title(raw)

    if n in {"admin", "administrador", "administrator", "adm"}:
        return "ADMIN"
    if n in {
        "dp",
        "departamento pessoal",
        "pessoal",
        "rh",
        "recursos humanos",
        "human resources",
        "people",
        "people ops",
        "people operations",
    }:
        return "DP"
    if n in {"user", "usuario", "usuário", "colaborador", "colaboradora"}:
        return "USER"

    return "USER"


# Relação gestor -> subordinados (conforme versão estável)
COL_GESTOR_DIRETO = "GESTOR DIRETO"
COL_GESTOR_SUPERIOR = "GESTOR SUPERIOR"
# fallback legado
COL_GESTOR = "GESTOR"
GESTOR_DIRETO_ALIASES = ("GESTOR DIRETO", "GESTOR_DIRETO", "GESTOR", "EMAIL GESTOR", "E-MAIL GESTOR")
GESTOR_SUPERIOR_ALIASES = ("GESTOR SUPERIOR", "GESTOR_SUPERIOR")


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


def _col_id(cmap: Dict[str, int], *candidate_names: str) -> int | None:
    """Resolve columnId por título exato ou por título normalizado."""
    if not cmap:
        return None

    for name in candidate_names:
        cid = cmap.get((name or "").strip().upper())
        if cid:
            return cid

    normalized = {norm_title(k): v for k, v in (cmap or {}).items()}
    for name in candidate_names:
        cid = normalized.get(norm_title(name or ""))
        if cid:
            return cid
    return None


def _cell_by_id(row, cid: int | None) -> str:
    if not cid:
        return ""
    for c in row.cells:
        if c.column_id == cid:
            return (c.display_value or c.value or "") if c is not None else ""
    return ""


def _cell_value(sheet, row, cmap: Dict[str, int], *col_names: str) -> str:
    cid = _col_id(cmap, *col_names)
    return _cell_by_id(row, cid)


def listar_colaboradores(access_token: str) -> List[Dict[str, Any]]:
    """Lista colaboradores a partir da planilha de cadastro.

    Obs.: Este serviço é usado principalmente para permissões (USER TYPE) e
    para relação Gestor->Subordinados (GESTOR DIRETO / GESTOR SUPERIOR).
    """
    sheet = _cached_get_cadastro_sheet(access_token)
    cmap = columns_map(sheet)

    out: List[Dict[str, Any]] = []
    for r in sheet.rows:
        email = normalize_email_identity(str(_cell_value(sheet, r, cmap, *EMAIL_ALIASES)))
        if not email:
            continue

        ut = normalizar_user_type(_cell_value(sheet, r, cmap, *USER_TYPE_ALIASES))
        st = (str(_cell_value(sheet, r, cmap, *STATUS_ALIASES)) or "").strip().upper()

        gestor_direto = normalize_email_identity(str(_cell_value(sheet, r, cmap, *GESTOR_DIRETO_ALIASES)))
        gestor_superior = normalize_email_identity(str(_cell_value(sheet, r, cmap, *GESTOR_SUPERIOR_ALIASES)))
        gestor_fallback = normalize_email_identity(str(_cell_value(sheet, r, cmap, COL_GESTOR)))

        out.append(
            {
                "row_id": r.id,
                "email": email,
                "email_local": email_local_part(email),
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
    email = normalize_email_identity(email)
    if not email:
        return None

    wanted_local = email_local_part(email)
    candidates = listar_colaboradores(access_token)

    # 1) exato: aqui precisa ser igualdade real de e-mail normalizado.
    # Não use emails_equivalentes nesta etapa, porque ela também aceita
    # local-part igual. Em bases com mais de uma linha parecida, isso pode
    # fazer um usuário ADMIN ser resolvido como DP apenas pela ordem da planilha.
    for c in candidates:
        if normalize_email_identity(c.get("email", "")) == email:
            return c

    # 2) fallback local-part
    if wanted_local:
        matches = [c for c in candidates if c.get("email_local") == wanted_local]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            # Se houver múltiplos (domínios diferentes), tenta:
            # 1) preferir o mesmo domínio do email informado
            dom = email.split("@", 1)[1] if "@" in email else ""
            for c in matches:
                em = c.get("email", "")
                if dom and safe_lower(em).endswith("@" + dom):
                    return c

            # 2) preferir o de maior privilégio (ADMIN > DP > USER)
            def _score(row: Dict[str, Any]) -> int:
                ut = (row.get("user_type") or "").strip().upper()
                return 3 if ut == "ADMIN" else (2 if ut == "DP" else 1)

            matches.sort(key=_score, reverse=True)
            return matches[0]

    return None


def get_user_row_by_identifiers(access_token: str, *identifiers: str) -> Optional[Dict[str, Any]]:
    """Localiza o usuário aceitando múltiplas identidades vindas do LDAP.

    Ex.: mail, userPrincipalName e sAMAccountName podem divergir entre si.
    A primeira identidade que casar com o cadastro vence.
    """
    seen = set()
    for identifier in identifiers:
        norm = normalize_email_identity(identifier)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        row = get_user_row(access_token, norm)
        if row:
            return row
    return None


def canonical_email_for(access_token: str, *identifiers: str) -> str:
    """Retorna o e-mail canônico do cadastro, se encontrado."""
    row = get_user_row_by_identifiers(access_token, *identifiers)
    if row and row.get("email"):
        return normalize_email_identity(row.get("email"))
    for identifier in identifiers:
        norm = normalize_email_identity(identifier)
        if norm:
            return norm
    return ""


def get_user_type(access_token: str, email: str) -> str:
    """Retorna o USER TYPE (ADMIN | DP | USER) baseado na coluna USER TYPE."""
    row = get_user_row(access_token, email)
    return normalizar_user_type((row.get("user_type") if row else "") or "")


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
    gestor_email = normalize_email_identity(gestor_email)
    if not gestor_email:
        return []

    gestor_row = get_user_row(access_token, gestor_email)
    gestor_canonico = normalize_email_identity((gestor_row or {}).get("email") or gestor_email)
    gestor_identidades = {gestor_email, gestor_canonico, email_local_part(gestor_email), email_local_part(gestor_canonico)}
    gestor_identidades = {g for g in gestor_identidades if g}

    ut = get_user_type(access_token, gestor_canonico or gestor_email)
    is_dp_user = ut == "DP"

    out: List[Dict[str, Any]] = []
    seen = set()

    for c in listar_colaboradores(access_token):
        try:
            colab_email = normalize_email_identity(c.get("email") or "")
            if not colab_email or any(emails_equivalentes(colab_email, g) for g in gestor_identidades):
                continue
            if colab_email in seen:
                continue

            gestor_direto = safe_lower(c.get("gestor_direto") or c.get("gestor") or "")
            gestor_superior = safe_lower(c.get("gestor_superior") or "")

            match = False
            if is_dp_user and gestor_superior == "dp":
                match = True
            elif gestor_superior and any(emails_equivalentes(gestor_superior, g) for g in gestor_identidades):
                match = True
            elif gestor_direto and any(emails_equivalentes(gestor_direto, g) for g in gestor_identidades):
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
