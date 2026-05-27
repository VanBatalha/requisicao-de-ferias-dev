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


STATUS_ATIVO_CANONICOS = {"", "ATIVO", "ACTIVE", "1", "SIM", "YES", "TRUE", "OK"}
STATUS_INATIVO_CANONICOS = {
    "INATIVO",
    "INATIVA",
    "INACTIVE",
    "0",
    "NAO",
    "NÃO",
    "NO",
    "FALSE",
    "DESLIGADO",
    "DESLIGADA",
    "DEMITIDO",
    "DEMITIDA",
}


def normalizar_status(value: object) -> str:
    """Normaliza a coluna STATUS para uma forma segura de comparação.

    A regra do cadastro legado considera status vazio como ativo. Porém, quando
    houver mais de uma linha para o mesmo e-mail/usuário, linhas explicitamente
    INATIVAS nunca devem vencer linhas ATIVAS.
    """
    raw = str(value or "").strip()
    if not raw:
        return ""

    n = norm_title(raw).upper()
    if n in {"NAO", "NÃO"}:
        return "NAO"
    return n


def is_status_ativo_value(value: object) -> bool:
    """Retorna se um valor de STATUS deve ser considerado ativo.

    Mantém compatibilidade com a versão estável: quando STATUS está vazio ou
    vem com valor desconhecido, assume ativo. Somente valores explicitamente
    inativos bloqueiam permissões/saldos.
    """
    st = normalizar_status(value)
    if st in STATUS_INATIVO_CANONICOS:
        return False
    if st in STATUS_ATIVO_CANONICOS:
        return True
    return True


def _row_is_active(row: Dict[str, Any] | None) -> bool:
    return bool(row) and is_status_ativo_value((row or {}).get("status") or (row or {}).get("status_raw"))


def _candidate_score(row: Dict[str, Any]) -> int:
    """Pontuação estável para desempate entre linhas equivalentes.

    A linha ativa sempre é filtrada antes desta função. O score só desempata
    duplicidades remanescentes mantendo a compatibilidade da v2: ADMIN > DP > USER.
    """
    ut = (row.get("user_type") or "").strip().upper()
    if ut == "ADMIN":
        return 3
    if ut == "DP":
        return 2
    return 1


def _pick_best_user_row(matches: List[Dict[str, Any]], wanted_email: str) -> Optional[Dict[str, Any]]:
    """Escolhe a melhor linha entre candidatos já filtrados por tipo de match."""
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]

    domain = wanted_email.split("@", 1)[1] if "@" in wanted_email else ""
    if domain:
        same_domain = [m for m in matches if safe_lower(m.get("email") or "").endswith("@" + domain)]
        if same_domain:
            matches = same_domain

    matches = sorted(matches, key=_candidate_score, reverse=True)
    return matches[0]


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
        st_raw = (str(_cell_value(sheet, r, cmap, *STATUS_ALIASES)) or "").strip()
        st = normalizar_status(st_raw)

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
                "status_raw": st_raw,
                "ativo": is_status_ativo_value(st),
                "gestor_direto": gestor_direto,
                "gestor_superior": gestor_superior,
                "gestor": gestor_fallback,
            }
        )
    return out


def get_user_row(access_token: str, email: str) -> Optional[Dict[str, Any]]:
    """Localiza o usuário no cadastro priorizando sempre o registro ATIVO.

    Ordem de resolução:
    1) e-mail exato + STATUS ativo
    2) local-part/usuário equivalente + STATUS ativo
    3) e-mail exato inativo, apenas como referência sem conceder privilégio
    4) local-part inativo, apenas como referência sem conceder privilégio

    Essa ordem evita que uma matrícula/cadastro antigo com STATUS=INATIVO
    continue definindo USER TYPE, principalmente em bases com duplicidade de
    cadastro para o mesmo e-mail/usuário.
    """
    email = normalize_email_identity(email)
    if not email:
        return None

    wanted_local = email_local_part(email)
    candidates = listar_colaboradores(access_token)

    exact_matches = [
        c for c in candidates
        if normalize_email_identity(c.get("email", "")) == email
    ]

    active_exact = [c for c in exact_matches if _row_is_active(c)]
    if active_exact:
        if len(active_exact) < len(exact_matches):
            log.info(
                "Cadastro: ignorando %s registro(s) inativo(s) para email exato %s",
                len(exact_matches) - len(active_exact),
                email,
            )
        return _pick_best_user_row(active_exact, email)

    local_matches: List[Dict[str, Any]] = []
    if wanted_local:
        local_matches = [c for c in candidates if c.get("email_local") == wanted_local]
        active_local = [c for c in local_matches if _row_is_active(c)]
        if active_local:
            if exact_matches:
                log.info(
                    "Cadastro: email %s possui registro exato inativo; usando cadastro ativo equivalente por usuário/local-part.",
                    email,
                )
            return _pick_best_user_row(active_local, email)

    if exact_matches:
        log.warning(
            "Cadastro: email %s possui somente registro(s) inativo(s); nenhum USER TYPE privilegiado será concedido.",
            email,
        )
        return _pick_best_user_row(exact_matches, email)

    if local_matches:
        log.warning(
            "Cadastro: usuário/local-part %s possui somente registro(s) inativo(s); nenhum USER TYPE privilegiado será concedido.",
            wanted_local,
        )
        return _pick_best_user_row(local_matches, email)

    return None

def get_user_row_by_identifiers(access_token: str, *identifiers: str) -> Optional[Dict[str, Any]]:
    """Localiza o usuário aceitando múltiplas identidades vindas do LDAP.

    Ex.: mail, userPrincipalName e sAMAccountName podem divergir entre si.
    Quando algum identificador resolver para cadastro INATIVO, continuamos
    procurando nos demais identificadores para priorizar uma referência ATIVA.
    """
    seen = set()
    inactive_or_fallback: List[Dict[str, Any]] = []

    for identifier in identifiers:
        norm = normalize_email_identity(identifier)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        row = get_user_row(access_token, norm)
        if not row:
            continue
        if _row_is_active(row):
            return row
        inactive_or_fallback.append(row)

    return inactive_or_fallback[0] if inactive_or_fallback else None


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
    """Retorna o USER TYPE (ADMIN | DP | USER) somente de cadastro ativo.

    Se a única linha encontrada estiver INATIVA, retornamos USER para evitar que
    perfis antigos (ex.: DP/ADMIN de matrícula antiga) sejam reutilizados.
    """
    row = get_user_row(access_token, email)
    if not row:
        return "USER"
    if not _row_is_active(row):
        log.warning(
            "Cadastro: USER TYPE ignorado para %s porque o cadastro resolvido está inativo.",
            email,
        )
        return "USER"
    return normalizar_user_type(row.get("user_type") or "")


def is_ativo(access_token: str, email: str) -> bool:
    row = get_user_row(access_token, email)
    if row is None:
        return False
    return _row_is_active(row)


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
