from __future__ import annotations

import re
import unicodedata

from ..utils import safe_lower

STATUS_CANON = {
    "pendente": "PENDENTE",
    "em analise": "EM ANÁLISE",
    "em análise": "EM ANÁLISE",
    "aprovada": "APROVADA",
    "aprovado": "APROVADA",
    "cancelada": "CANCELADA",
    "cancelado": "CANCELADA",
    "reprovado": "REPROVADO",
    "reprovada": "REPROVADO",
    "rejeitada": "REPROVADO",
    "rejeitado": "REPROVADO",
}

STATUS_APROVADA = {"aprovada", "aprovado"}
STATUS_RESERVA = {"pendente", "em analise", "em análise"}


def norm_title(value: str | None) -> str:
    text = "" if value is None else str(value)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[_\-]+", " ", text)
    text = re.sub(r"[^0-9a-zA-Z\s]+", " ", text)
    return " ".join(text.strip().lower().split())


def cols_norm_map(cols: dict | None) -> dict:
    out = {}
    for key, value in (cols or {}).items():
        try:
            out[norm_title(key)] = value
        except Exception:
            continue
    return out


def col_id(cols_norm: dict, *candidates: str):
    for name in candidates:
        cid = cols_norm.get(norm_title(name))
        if cid:
            return cid
    return None


def norm(value: str | None) -> str:
    return norm_title((value or "").strip())


def norm_solicitacao(value: str) -> str:
    return norm_title(value)


def is_ajuste(solicitacao: str) -> bool:
    return "ajuste" in norm_solicitacao(solicitacao)


def infer_saldo_tipo(observacoes: str, explicit: str = "") -> str:
    explicit_norm = norm_title(explicit) if explicit else ""
    if explicit_norm in {
        "premium",
        "licenca premium",
        "licenca certariana",
        "licença certariana",
        "certariana",
        "licenca certareana",
        "licença certareana",
    }:
        return "PREMIUM"
    if explicit_norm in {"regular", "ferias", "férias", "ferias regulares", "férias regulares"}:
        return "REGULAR"

    obs_norm = norm_title(observacoes or "")
    premium_markers = (
        "saldo: premium",
        "saldo premium",
        "[premium]",
        "premium",
        "saldo: certariana",
        "saldo certariana",
        "[certariana]",
        "certariana",
    )
    if any(marker in obs_norm for marker in premium_markers):
        return "PREMIUM"
    return "REGULAR"


def norm_status(value: str) -> str:
    if not value:
        return ""
    text = unicodedata.normalize("NFD", str(value))
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    text = re.sub(r"\s+", " ", text.strip().lower())
    return text


def canonical_status(value: str) -> str:
    normalized = norm_status(value)
    return STATUS_CANON.get(normalized, (value or "").strip().upper())


def norm_email(email: str) -> str:
    return safe_lower(email)
