"""Serviços administrativos para editar cadastro diretamente no PostgreSQL."""
from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Optional

from sqlalchemy import func, or_

from ..logging_config import get_logger
from ..models import Auditoria, Colaborador, ColaboradorComplemento
from ..utils import safe_lower
from .postgres_service import get_db_session

log = get_logger(__name__)


EDITABLE_COLAB_FIELDS = {
    "email",
    "nome_completo",
    "status",
    "data_admissao",
    "setor",
    "cargo",
    "regime",
    "dias_direito",
}

EDITABLE_COMP_FIELDS = {
    "user_type",
    "gestor_direto_email",
    "gestor_superior_email",
    "ativo_no_app",
    "saldo_regular_direito",
    "saldo_regular_usado",
    "saldo_regular_reservado",
    "saldo_regular_disponivel",
    "saldo_premium_direito",
    "saldo_premium_usado",
    "saldo_premium_reservado",
    "saldo_premium_disponivel",
    "total_solicitacoes",
}

INTEGER_FIELDS = {
    "dias_direito",
    "saldo_regular_direito",
    "saldo_regular_usado",
    "saldo_regular_reservado",
    "saldo_regular_disponivel",
    "saldo_premium_direito",
    "saldo_premium_usado",
    "saldo_premium_reservado",
    "saldo_premium_disponivel",
    "total_solicitacoes",
}


def _as_date(value: Any) -> Optional[dt.date]:
    if value in (None, ""):
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(text[:19], fmt).date()
        except ValueError:
            pass
    return None


def _as_int(value: Any) -> int:
    if value in (None, ""):
        return 0
    try:
        text = str(value).strip().replace(",", ".")
        return int(round(float(text)))
    except Exception:
        return 0


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"1", "true", "sim", "s", "yes", "y", "on", "ativo"}


def _serialize_date(value: Any) -> Optional[str]:
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    return value if value not in (None, "") else None


def _jsonable_colab(colab: Colaborador) -> Dict[str, Any]:
    comp = colab.complemento
    return {
        "id": colab.id,
        "email": colab.email or "",
        "nome_completo": colab.nome_completo or "",
        "status": colab.status or "",
        "data_admissao": _serialize_date(colab.data_admissao),
        "setor": colab.setor or "",
        "cargo": colab.cargo or "",
        "regime": colab.regime or "",
        "dias_direito": int(colab.dias_direito or 0),
        "origem_sheet_id": colab.origem_sheet_id,
        "origem_row_id": colab.origem_row_id,
        "raw_payload": colab.raw_payload or {},
        "user_type": (comp.user_type if comp else "USER") or "USER",
        "gestor_direto_email": (comp.gestor_direto_email if comp else "") or "",
        "gestor_superior_email": (comp.gestor_superior_email if comp else "") or "",
        "ativo_no_app": bool(comp.ativo_no_app) if comp else True,
        "flags_internas": comp.flags_internas if comp else {},
        "saldo_regular_direito": int((comp.saldo_regular_direito if comp else 0) or 0),
        "saldo_regular_usado": int((comp.saldo_regular_usado if comp else 0) or 0),
        "saldo_regular_reservado": int((comp.saldo_regular_reservado if comp else 0) or 0),
        "saldo_regular_disponivel": int((comp.saldo_regular_disponivel if comp else 0) or 0),
        "saldo_premium_direito": int((comp.saldo_premium_direito if comp else 0) or 0),
        "saldo_premium_usado": int((comp.saldo_premium_usado if comp else 0) or 0),
        "saldo_premium_reservado": int((comp.saldo_premium_reservado if comp else 0) or 0),
        "saldo_premium_disponivel": int((comp.saldo_premium_disponivel if comp else 0) or 0),
        "total_solicitacoes": int((comp.total_solicitacoes if comp else 0) or 0),
        "periodo_aquisitivo_atual": comp.periodo_aquisitivo_atual if comp else {},
        "created_at": colab.created_at.isoformat() if colab.created_at else None,
        "updated_at": colab.updated_at.isoformat() if colab.updated_at else None,
        "complemento_updated_at": comp.updated_at.isoformat() if comp and comp.updated_at else None,
    }


def buscar_colaboradores_admin(q: str, limit: int = 20) -> List[Dict[str, Any]]:
    session = get_db_session()
    q = (q or "").strip()
    if not q:
        return []
    pattern = f"%{q.lower()}%"
    rows = (
        session.query(Colaborador)
        .outerjoin(ColaboradorComplemento)
        .filter(or_(func.lower(Colaborador.email).like(pattern), func.lower(Colaborador.nome_completo).like(pattern)))
        .order_by(Colaborador.nome_completo.asc().nullslast(), Colaborador.email.asc())
        .limit(max(1, min(int(limit or 20), 50)))
        .all()
    )
    out = []
    for c in rows:
        comp = c.complemento
        out.append({
            "id": c.id,
            "email": c.email or "",
            "nome_completo": c.nome_completo or "",
            "status": c.status or "",
            "setor": c.setor or "",
            "cargo": c.cargo or "",
            "user_type": (comp.user_type if comp else "USER") or "USER",
            "ativo_no_app": bool(comp.ativo_no_app) if comp else True,
        })
    return out


def obter_colaborador_admin(colaborador_id: int) -> Optional[Dict[str, Any]]:
    session = get_db_session()
    colab = session.query(Colaborador).filter(Colaborador.id == int(colaborador_id)).first()
    if not colab:
        return None
    return _jsonable_colab(colab)


def atualizar_colaborador_admin(colaborador_id: int, payload: Dict[str, Any], actor_email: str = "") -> Dict[str, Any]:
    session = get_db_session()
    colab = session.query(Colaborador).filter(Colaborador.id == int(colaborador_id)).first()
    if not colab:
        raise ValueError("Colaborador não encontrado.")

    before = _jsonable_colab(colab)

    comp = colab.complemento
    if not comp:
        comp = ColaboradorComplemento(colaborador_id=colab.id, user_type="USER", ativo_no_app=True)
        session.add(comp)
        session.flush()

    # Colaborador base
    for field in EDITABLE_COLAB_FIELDS:
        if field not in payload:
            continue
        value = payload.get(field)
        if field == "email":
            value = safe_lower(value or "")
            if not value:
                raise ValueError("E-mail é obrigatório.")
            # impede duplicidade de e-mail em outro cadastro
            dup = session.query(Colaborador).filter(func.lower(Colaborador.email) == value.lower(), Colaborador.id != colab.id).first()
            if dup:
                raise ValueError("Já existe outro colaborador com este e-mail.")
            setattr(colab, field, value)
        elif field == "data_admissao":
            setattr(colab, field, _as_date(value))
        elif field == "dias_direito":
            setattr(colab, field, _as_int(value))
        else:
            setattr(colab, field, str(value or "").strip() or None)

    # Complemento
    for field in EDITABLE_COMP_FIELDS:
        if field not in payload:
            continue
        value = payload.get(field)
        if field == "user_type":
            ut = str(value or "USER").strip().upper()
            if ut in {"ADMINISTRADOR"}:
                ut = "ADMIN"
            if ut in {"RH"}:
                ut = "DP"
            if ut not in {"USER", "DP", "ADMIN"}:
                ut = "USER"
            setattr(comp, field, ut)
        elif field in {"gestor_direto_email", "gestor_superior_email"}:
            setattr(comp, field, safe_lower(value or "") or None)
        elif field == "ativo_no_app":
            setattr(comp, field, _as_bool(value))
        elif field in INTEGER_FIELDS:
            setattr(comp, field, _as_int(value))
        else:
            setattr(comp, field, value)

    now = dt.datetime.utcnow()
    colab.updated_at = now
    comp.updated_at = now

    # Mantém algumas chaves do raw_payload coerentes para a camada legada.
    raw = dict(colab.raw_payload or {}) if isinstance(colab.raw_payload, dict) else {}
    raw.update({
        "EMAIL DA EMPRESA": colab.email,
        "NOME COMPLETO": colab.nome_completo,
        "STATUS": colab.status,
        "DATA DE ADMISSAO": colab.data_admissao.isoformat() if colab.data_admissao else None,
        "DATA DE ADMISSÃO": colab.data_admissao.isoformat() if colab.data_admissao else None,
        "SETOR": colab.setor,
        "CARGO": colab.cargo,
        "REGIME": colab.regime,
        "DIAS DE DIREITO": colab.dias_direito,
        "USER TYPE": comp.user_type,
        "GESTOR DIRETO": comp.gestor_direto_email,
        "GESTOR SUPERIOR": comp.gestor_superior_email,
    })
    colab.raw_payload = raw

    session.flush()
    after = _jsonable_colab(colab)

    try:
        session.add(Auditoria(
            actor_email=safe_lower(actor_email or ""),
            action="UPDATE_COLABORADOR_ADMIN",
            entity_type="colaborador",
            entity_id=colab.id,
            before_data=before,
            after_data=after,
            context={"origem": "painel_admin"},
        ))
    except Exception as exc:
        log.warning("Falha ao registrar auditoria de colaborador %s: %s", colab.id, exc)

    session.commit()
    return after


def atualizar_user_type_por_email(email: str, user_type: str, actor_email: str = "") -> Optional[Dict[str, Any]]:
    session = get_db_session()
    email = safe_lower(email or "")
    if not email:
        raise ValueError("E-mail é obrigatório.")
    colab = session.query(Colaborador).filter(func.lower(Colaborador.email) == email.lower()).first()
    if not colab:
        return None
    return atualizar_colaborador_admin(colab.id, {"user_type": user_type}, actor_email=actor_email)
