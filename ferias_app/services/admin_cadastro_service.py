"""Serviços administrativos para editar cadastro diretamente no PostgreSQL."""
from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Optional

from sqlalchemy import func, or_

from ..logging_config import get_logger
from ..models import Auditoria, Colaborador, ColaboradorComplemento, PermissaoUsuario, HierarquiaGestao, PeriodoAquisitivo, SaldoPeriodo, AuditoriaSaldos
from ..utils import safe_lower
from .postgres_service import get_db_session

log = get_logger(__name__)


EDITABLE_COLAB_FIELDS = {
    "email",
    "matricula",
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


def _is_active_status_expr():
    return func.upper(func.coalesce(Colaborador.status, "ATIVO")).in_(["ATIVO", "ACTIVE"])


def _active_colaborador_by_email(session, email: str):
    email = safe_lower(email or "")
    if not email:
        return None
    rows = session.query(Colaborador).filter(func.lower(Colaborador.email) == email.lower()).all()
    if rows:
        rows.sort(key=lambda c: (1 if str(c.status or "").strip().upper() in {"ATIVO", "ACTIVE"} else 0, int(c.id or 0)), reverse=True)
        return rows[0]
    return None


def _serialize_date(value: Any) -> Optional[str]:
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    return value if value not in (None, "") else None



def _sincronizar_comp_com_tabelas_novas(session, colab: Colaborador, comp: ColaboradorComplemento):
    # Permissão por matrícula
    ut = str(comp.user_type or 'USER').strip().upper()
    if ut == 'ADMIN':
        ut_role = 'ADMINISTRADOR'
    elif ut in {'DP', 'RH'}:
        ut_role = 'DP'
    else:
        ut_role = 'USER'
    # remove permissões antigas e grava a atual
    session.query(PermissaoUsuario).filter(PermissaoUsuario.colaborador_matricula == colab.matricula).delete(synchronize_session=False)
    session.add(PermissaoUsuario(colaborador_id=colab.id, colaborador_matricula=colab.matricula, role=ut_role))

    # Hierarquia simples por e-mail. Quando encontrar matrícula do gestor, também grava.
    gd_email = safe_lower(comp.gestor_direto_email or '') or None
    gs_email = safe_lower(comp.gestor_superior_email or '') or None
    gd = _active_colaborador_by_email(session, gd_email) if gd_email else None
    gs = _active_colaborador_by_email(session, gs_email) if gs_email and gs_email != 'dp' else None
    h = session.query(HierarquiaGestao).filter(HierarquiaGestao.colaborador_matricula == colab.matricula).first()
    if not h:
        h = HierarquiaGestao(colaborador_id=colab.id, colaborador_matricula=colab.matricula)
        session.add(h)
    h.gestor_direto_id = gd.id if gd else None
    h.gestor_direto_matricula = gd.matricula if gd else None
    h.gestor_direto_email = gd_email
    if gs_email == 'dp':
        h.gestor_superior_tipo = 'DP'
        h.gestor_superior_id = None
        h.gestor_superior_matricula = None
        h.gestor_superior_email_custom = None
    elif gs:
        h.gestor_superior_tipo = 'GESTOR'
        h.gestor_superior_id = gs.id
        h.gestor_superior_matricula = gs.matricula
        h.gestor_superior_email_custom = None
    else:
        h.gestor_superior_tipo = 'EMAIL_CUSTOM' if gs_email else 'GESTOR'
        h.gestor_superior_id = None
        h.gestor_superior_matricula = None
        h.gestor_superior_email_custom = gs_email


def _saldos_periodo_json(session, colab: Colaborador):
    rows = (
        session.query(PeriodoAquisitivo, SaldoPeriodo)
        .join(SaldoPeriodo, SaldoPeriodo.periodo_id == PeriodoAquisitivo.id)
        .filter(PeriodoAquisitivo.colaborador_matricula == colab.matricula)
        .order_by(PeriodoAquisitivo.data_inicio.asc(), PeriodoAquisitivo.periodo_numero.asc(), SaldoPeriodo.tipo_saldo.asc())
        .all()
    )
    out = []
    for p, s in rows:
        direito = int(round(float(s.dias_direito or 0)))
        usados = int(round(float(s.dias_usados or 0)))
        reservados = int(round(float(s.dias_reservados or 0)))
        out.append({
            'saldo_id': s.id,
            'periodo_id': p.id,
            'tipo_saldo': s.tipo_saldo,
            'periodo_numero': p.periodo_numero,
            'data_inicio': p.data_inicio.isoformat() if p.data_inicio else None,
            'data_fim': p.data_fim.isoformat() if p.data_fim else None,
            'is_atual': bool(p.is_atual),
            'dias_direito': direito,
            'dias_usados': usados,
            'dias_reservados': reservados,
            'dias_disponiveis': max(0, direito - usados - reservados),
        })
    return out

def _jsonable_colab(colab: Colaborador) -> Dict[str, Any]:
    comp = colab.complemento
    return {
        "id": colab.id,
        "matricula": colab.matricula or "",
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
        "saldos_periodo": _saldos_periodo_json(get_db_session(), colab),
    }


def buscar_colaboradores_admin(q: str, limit: int = 20) -> List[Dict[str, Any]]:
    session = get_db_session()
    q = (q or "").strip()
    if not q:
        return []
    pattern = f"%{q.lower()}%"
    rows = (
        session.query(Colaborador)
        .outerjoin(ColaboradorComplemento, ColaboradorComplemento.colaborador_id == Colaborador.id)
        .filter(_is_active_status_expr())
        .filter(or_(
            func.lower(Colaborador.email).like(pattern),
            func.lower(Colaborador.nome_completo).like(pattern),
            func.lower(Colaborador.matricula).like(pattern),
        ))
        .order_by(Colaborador.nome_completo.asc().nullslast(), Colaborador.email.asc())
        .limit(max(1, min(int(limit or 20), 50)))
        .all()
    )
    out = []
    for c in rows:
        comp = c.complemento
        out.append({
            "id": c.id,
            "matricula": c.matricula or "",
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
        if field == "matricula":
            matricula = str(value or "").strip().upper() or None
            # Matrícula é o ID externo do cadastro. Não exigimos unicidade rígida
            # no banco para não bloquear cadastros legados, mas avisamos em caso
            # de duplicidade inequívoca.
            if matricula:
                dup = session.query(Colaborador).filter(func.lower(Colaborador.matricula) == matricula.lower(), Colaborador.id != colab.id).first()
                if dup:
                    raise ValueError("Já existe outro colaborador com esta matrícula.")
            setattr(colab, field, matricula)
        elif field == "email":
            value = safe_lower(value or "")
            if not value:
                raise ValueError("E-mail é obrigatório.")
            # impede duplicidade de e-mail em outro cadastro
            dup = session.query(Colaborador).filter(func.lower(Colaborador.email) == value.lower(), Colaborador.id != colab.id, _is_active_status_expr()).first()
            if dup:
                raise ValueError("Já existe outro colaborador ativo com este e-mail.")
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
        "MATRICULA": colab.matricula,
        "MATRÍCULA": colab.matricula,
        "__matricula_escolhida__": colab.matricula,
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

    _sincronizar_comp_com_tabelas_novas(session, colab, comp)
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
    colab = _active_colaborador_by_email(session, email)
    if not colab:
        return None
    return atualizar_colaborador_admin(colab.id, {"user_type": user_type}, actor_email=actor_email)
