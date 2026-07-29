"""Serviços administrativos para editar cadastro diretamente no PostgreSQL."""
from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, func, or_

from ..logging_config import get_logger
from ..models import Auditoria, Colaborador, ColaboradorComplemento, PermissaoUsuario, HierarquiaGestao, SaldoPeriodoNovo, Solicitacao
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
    "gestor_direto",
    "gestor_superior",
}

INTEGER_FIELDS = {
    "dias_direito",
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


def _normalizar_ref_matricula(value: Any, allow_dp: bool = True, allow_gestor: bool = True) -> str:
    raw = str(value or "").strip()
    if not raw or "@" in raw:
        return ""
    norm = raw.upper().strip()
    if allow_dp and norm in {"DP", "RH", "DEPARTAMENTO PESSOAL"}:
        return "DP"
    if allow_gestor and norm in {"GESTOR", "GESTORES", "GESTOR DIRETO"}:
        return "GESTOR"
    if re.fullmatch(r"MAT\d+", norm):
        return norm
    if re.fullmatch(r"\d+", norm):
        return f"MAT{int(norm):05d}"
    return ""


def _colaborador_ativo_por_matricula(session, matricula: str):
    mat = _normalizar_ref_matricula(matricula, allow_dp=False, allow_gestor=False)
    if not mat:
        return None
    return session.query(Colaborador).filter(
        func.upper(Colaborador.matricula) == mat,
        func.upper(func.coalesce(Colaborador.status, "ATIVO")).in_(["ATIVO", "ACTIVE"])
    ).first()


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

    # Hierarquia por matricula/marcador. E-mail nao cria vinculo operacional.
    gd_ref = _normalizar_ref_matricula(comp.gestor_direto, allow_dp=False, allow_gestor=False)
    gs_ref = _normalizar_ref_matricula(comp.gestor_superior, allow_dp=True, allow_gestor=True)
    gd = _colaborador_ativo_por_matricula(session, gd_ref) if gd_ref else None
    gs = _colaborador_ativo_por_matricula(session, gs_ref) if gs_ref and gs_ref not in {'DP', 'GESTOR'} else None
    h = session.query(HierarquiaGestao).filter(HierarquiaGestao.colaborador_matricula == colab.matricula).first()
    if not h:
        h = HierarquiaGestao(colaborador_id=colab.id, colaborador_matricula=colab.matricula)
        session.add(h)
    h.gestor_direto_id = gd.id if gd else None
    h.gestor_direto_matricula = gd.matricula if gd else None
    h.gestor_direto_email = safe_lower(gd.email) if gd and gd.email else None
    h.gestor_superior_id = gs.id if gs else None
    h.gestor_superior_matricula = gs.matricula if gs else (gs_ref or None)
    h.gestor_superior_email = safe_lower(gs.email) if gs and gs.email else None


def _saldo_periodo_json(session, colab: Colaborador):
    rows = (
        session.query(SaldoPeriodoNovo)
        .filter(SaldoPeriodoNovo.colaborador_matricula == colab.matricula)
        .order_by(SaldoPeriodoNovo.data_inicio.asc(), SaldoPeriodoNovo.periodo_numero.asc(), SaldoPeriodoNovo.tipo_saldo.asc())
        .all()
    )
    out = []
    for s in rows:
        direito = int(round(float(s.saldo_inicial or 0)))
        usados = int(round(float(s.saldo_utilizado or 0)))
        reservados = int(round(float(s.saldo_reservado or 0)))
        disponivel = int(round(float(s.saldo_disponivel or 0)))
        out.append({
            'saldo_id': s.id,
            'periodo_id': s.id,
            'tipo_saldo': s.tipo_saldo,
            'periodo_numero': s.periodo_numero,
            'data_inicio': s.data_inicio.isoformat() if s.data_inicio else None,
            'data_fim': s.data_fim.isoformat() if s.data_fim else None,
            'is_atual': bool(s.is_atual),
            'dias_direito': direito,
            'dias_usados': usados,
            'dias_reservados': reservados,
            'dias_disponiveis': max(0, disponivel),
        })
    return out



def _resumo_saldo_periodo(session, colab: Colaborador) -> Dict[str, Any]:
    rows = (
        session.query(SaldoPeriodoNovo)
        .filter(
            SaldoPeriodoNovo.colaborador_matricula == colab.matricula,
            SaldoPeriodoNovo.is_atual.is_(True),
        )
        .all()
    )

    def sumtipo(tipo: str, attr: str) -> int:
        return int(round(sum(float(getattr(r, attr) or 0) for r in rows if (r.tipo_saldo or "").upper() == tipo)))

    return {
        "regular": {
            "direito": sumtipo("REGULAR", "saldo_inicial"),
            "usado": sumtipo("REGULAR", "saldo_utilizado"),
            "reservado": sumtipo("REGULAR", "saldo_reservado"),
            "disponivel": sumtipo("REGULAR", "saldo_disponivel"),
        },
        "premium": {
            "direito": sumtipo("PREMIUM", "saldo_inicial"),
            "usado": sumtipo("PREMIUM", "saldo_utilizado"),
            "reservado": sumtipo("PREMIUM", "saldo_reservado"),
            "disponivel": sumtipo("PREMIUM", "saldo_disponivel"),
        },
    }

def _ajustes_json(session, colab: Colaborador) -> List[Dict[str, Any]]:
    rows = (
        session.query(Solicitacao)
        .filter(
            Solicitacao.colaborador_matricula == colab.matricula,
            Solicitacao.is_ajuste.is_(True),
        )
        .order_by(Solicitacao.data_inicio.desc().nullslast(), Solicitacao.id.desc())
        .all()
    )
    out: List[Dict[str, Any]] = []
    for row in rows:
        dias = row.dias if row.dias is not None else row.dias_solicitados
        out.append({
            "id": row.id,
            "data_inicio": _serialize_date(row.data_inicio),
            "data_fim": _serialize_date(row.data_fim),
            "solicitacao": row.solicitacao or row.tipo_solicitacao or "AJUSTE",
            "saldo_tipo": (row.saldo_tipo or row.tipo_ferias or "REGULAR").upper(),
            "dias": float(dias or 0),
            "status": row.status or "",
            "observacoes": row.observacoes or "",
            "periodo_aquisitivo_origem": row.periodo_aquisitivo_origem or "",
            "solicitante_matricula": row.solicitante_matricula or "",
            "criado_por": row.criado_por or row.gestor_solicitante_email or "",
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        })
    return out



def _solicitacao_vinculo_expr(colab: Colaborador):
    condicoes = [
        Solicitacao.colaborador_matricula == colab.matricula,
        Solicitacao.colaborador_id == colab.id,
    ]
    email = safe_lower(colab.email or "")
    if email:
        # Compatibilidade exclusivamente para históricos migrados sem matrícula.
        condicoes.append(and_(
            Solicitacao.colaborador_matricula.is_(None),
            Solicitacao.colaborador_id.is_(None),
            func.lower(Solicitacao.colaborador_email) == email,
        ))
    return or_(*condicoes)


def _solicitacoes_json(session, colab: Colaborador) -> List[Dict[str, Any]]:
    rows = (
        session.query(Solicitacao)
        .filter(
            _solicitacao_vinculo_expr(colab),
            or_(Solicitacao.is_ajuste.is_(False), Solicitacao.is_ajuste.is_(None)),
        )
        .order_by(Solicitacao.data_inicio.desc().nullslast(), Solicitacao.id.desc())
        .all()
    )
    return [_solicitacao_dict(row) for row in rows]


def _jsonable_colab(colab: Colaborador) -> Dict[str, Any]:
    session = get_db_session()
    comp = colab.complemento
    saldo_periodo = _saldo_periodo_json(session, colab)
    saldos_resumo = _resumo_saldo_periodo(session, colab)
    ajustes = _ajustes_json(session, colab)
    solicitacoes = _solicitacoes_json(session, colab)
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
        "gestor_direto": (getattr(comp, "gestor_direto", None) if comp else "") or "",
        "gestor_superior": (getattr(comp, "gestor_superior", None) if comp else "") or "",
        "ativo_no_app": bool(comp.ativo_no_app) if comp else True,
        "flags_internas": comp.flags_internas if comp else {},
        "created_at": colab.created_at.isoformat() if colab.created_at else None,
        "updated_at": colab.updated_at.isoformat() if colab.updated_at else None,
        "complemento_updated_at": comp.updated_at.isoformat() if comp and comp.updated_at else None,
        "saldo_periodo": saldo_periodo,
        "saldos_resumo": saldos_resumo,
        "ajustes": ajustes,
        "solicitacoes": solicitacoes,
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
        elif field in {"gestor_direto", "gestor_superior"}:
            setattr(comp, field, str(value or "").strip().upper() or None)
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
        "USER TYPE": comp.user_type,
        "GESTOR DIRETO": getattr(comp, "gestor_direto", None),
        "GESTOR SUPERIOR": getattr(comp, "gestor_superior", None),
        "GESTOR_DIRETO_MATRICULA": getattr(comp, "gestor_direto", None),
        "GESTOR_SUPERIOR_MATRICULA": getattr(comp, "gestor_superior", None),
    })
    colab.raw_payload = raw

    _sincronizar_comp_com_tabelas_novas(session, colab, comp)
    session.flush()

    # V58: a mudança futura para INATIVO preserva o histórico já existente
    # em saldo_periodo. A rotina diária considera somente ATIVOS para criar novos
    # ciclos, portanto o inativo não recebe novos períodos após o desligamento.

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


def _as_decimal(value: Any, field_name: str) -> Decimal:
    if isinstance(value, Decimal):
        return value.quantize(Decimal("0.01"))
    text = str(value if value is not None else "0").strip().replace(",", ".")
    try:
        return Decimal(text or "0").quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Valor inválido para {field_name}.") from exc


def _saldo_periodo_dict(row: SaldoPeriodoNovo) -> Dict[str, Any]:
    return {
        "id": row.id,
        "colaborador_matricula": row.colaborador_matricula,
        "periodo_numero": row.periodo_numero,
        "data_inicio": _serialize_date(row.data_inicio),
        "data_fim": _serialize_date(row.data_fim),
        "is_atual": bool(row.is_atual),
        "tipo_saldo": (row.tipo_saldo or "REGULAR").upper(),
        "saldo_inicial": float(row.saldo_inicial or 0),
        "saldo_utilizado": float(row.saldo_utilizado or 0),
        "saldo_reservado": float(row.saldo_reservado or 0),
        "saldo_disponivel": float(row.saldo_disponivel or 0),
    }


def atualizar_saldo_periodo_admin(
    colaborador_id: int,
    saldo_id: int,
    payload: Dict[str, Any],
    actor_email: str = "",
) -> Dict[str, Any]:
    session = get_db_session()
    colab = session.query(Colaborador).filter(Colaborador.id == int(colaborador_id)).first()
    if not colab:
        raise ValueError("Colaborador não encontrado.")

    saldo = session.query(SaldoPeriodoNovo).filter(
        SaldoPeriodoNovo.id == int(saldo_id),
        SaldoPeriodoNovo.colaborador_matricula == colab.matricula,
    ).first()
    if not saldo:
        raise ValueError("Linha de saldo não encontrada para este colaborador.")

    before = _saldo_periodo_dict(saldo)
    tipo = str(payload.get("tipo_saldo", saldo.tipo_saldo or "REGULAR")).strip().upper()
    if tipo in {"CERTARIANA", "LICENCA CERTARIANA", "LICENÇA CERTARIANA"}:
        tipo = "PREMIUM"
    if tipo not in {"REGULAR", "PREMIUM"}:
        raise ValueError("Tipo de saldo inválido.")

    try:
        periodo_numero = int(payload.get("periodo_numero", saldo.periodo_numero))
    except (TypeError, ValueError) as exc:
        raise ValueError("Número do período inválido.") from exc
    if periodo_numero <= 0:
        raise ValueError("O período deve ser maior que zero.")

    data_inicio = _as_date(payload.get("data_inicio", saldo.data_inicio))
    data_fim = _as_date(payload.get("data_fim", saldo.data_fim))
    if not data_inicio or not data_fim:
        raise ValueError("As datas de início e fim são obrigatórias.")
    if data_fim < data_inicio:
        raise ValueError("A data final não pode ser anterior à data inicial.")

    inicial = _as_decimal(payload.get("saldo_inicial", saldo.saldo_inicial), "saldo inicial")
    utilizado = _as_decimal(payload.get("saldo_utilizado", saldo.saldo_utilizado), "saldo utilizado")
    reservado = _as_decimal(payload.get("saldo_reservado", saldo.saldo_reservado), "saldo reservado")
    if inicial < 0 or utilizado < 0 or reservado < 0:
        raise ValueError("Os valores de saldo não podem ser negativos.")
    disponivel = inicial - utilizado - reservado
    if disponivel < 0:
        raise ValueError("Utilizado + reservado não pode ser maior que o saldo inicial.")

    conflito = session.query(SaldoPeriodoNovo.id).filter(
        SaldoPeriodoNovo.colaborador_matricula == colab.matricula,
        SaldoPeriodoNovo.periodo_numero == periodo_numero,
        SaldoPeriodoNovo.tipo_saldo == tipo,
        SaldoPeriodoNovo.id != saldo.id,
    ).first()
    if conflito:
        raise ValueError(f"Já existe uma linha {tipo} para o período P{periodo_numero}.")

    is_atual = _as_bool(payload.get("is_atual", saldo.is_atual))
    if is_atual:
        # A V54 permite saldo somente na linha vigente. Ao trocar o P atual,
        # todo o histórico do mesmo tipo permanece visível, mas zerado.
        session.query(SaldoPeriodoNovo).filter(
            SaldoPeriodoNovo.colaborador_matricula == colab.matricula,
            SaldoPeriodoNovo.tipo_saldo == tipo,
            SaldoPeriodoNovo.id != saldo.id,
        ).update({
            SaldoPeriodoNovo.is_atual: False,
            SaldoPeriodoNovo.saldo_inicial: 0,
            SaldoPeriodoNovo.saldo_utilizado: 0,
            SaldoPeriodoNovo.saldo_reservado: 0,
            SaldoPeriodoNovo.saldo_disponivel: 0,
            SaldoPeriodoNovo.ultima_alteracao: dt.datetime.utcnow(),
            SaldoPeriodoNovo.updated_at: dt.datetime.utcnow(),
        }, synchronize_session=False)
    else:
        inicial = Decimal("0.00")
        utilizado = Decimal("0.00")
        reservado = Decimal("0.00")
        disponivel = Decimal("0.00")

    saldo.periodo_numero = periodo_numero
    saldo.data_inicio = data_inicio
    saldo.data_fim = data_fim
    saldo.is_atual = is_atual
    saldo.tipo_saldo = tipo
    saldo.saldo_inicial = inicial
    saldo.saldo_utilizado = utilizado
    saldo.saldo_reservado = reservado
    saldo.saldo_disponivel = disponivel
    saldo.ultima_alteracao = dt.datetime.utcnow()
    saldo.updated_at = dt.datetime.utcnow()
    session.flush()

    after = _saldo_periodo_dict(saldo)
    session.add(Auditoria(
        actor_email=safe_lower(actor_email or ""),
        action="UPDATE_SALDO_PERIODO_ADMIN",
        entity_type="saldo_periodo",
        entity_id=saldo.id,
        before_data=before,
        after_data=after,
        context={"origem": "painel_admin", "colaborador_id": colab.id, "matricula": colab.matricula},
    ))
    session.commit()
    return obter_colaborador_admin(colab.id)


def excluir_saldo_periodo_admin(
    colaborador_id: int,
    saldo_id: int,
    actor_email: str = "",
) -> Dict[str, Any]:
    session = get_db_session()
    colab = session.query(Colaborador).filter(Colaborador.id == int(colaborador_id)).first()
    if not colab:
        raise ValueError("Colaborador não encontrado.")
    if str(colab.status or "").strip().upper() not in {"ATIVO", "ACTIVE"} or colab.data_admissao is None:
        raise ValueError("Colaborador inativo ou sem data de admissão não pode possuir saldo.")
    saldo = session.query(SaldoPeriodoNovo).filter(
        SaldoPeriodoNovo.id == int(saldo_id),
        SaldoPeriodoNovo.colaborador_matricula == colab.matricula,
    ).first()
    if not saldo:
        raise ValueError("Linha de saldo não encontrada para este colaborador.")

    before = _saldo_periodo_dict(saldo)
    session.add(Auditoria(
        actor_email=safe_lower(actor_email or ""),
        action="DELETE_SALDO_PERIODO_ADMIN",
        entity_type="saldo_periodo",
        entity_id=saldo.id,
        before_data=before,
        after_data=None,
        context={"origem": "painel_admin", "colaborador_id": colab.id, "matricula": colab.matricula},
    ))
    session.delete(saldo)
    session.commit()
    return obter_colaborador_admin(colab.id)


def _ajuste_v54_ignorado(row: Solicitacao) -> bool:
    metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
    return bool(metadata.get("v54_premium_adjustment_ignored"))


def _ajuste_dict(row: Solicitacao) -> Dict[str, Any]:
    dias = row.dias if row.dias is not None else row.dias_solicitados
    metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
    return {
        "id": row.id,
        "colaborador_matricula": row.colaborador_matricula,
        "saldo_tipo": (row.saldo_tipo or row.tipo_ferias or "REGULAR").upper(),
        "dias": float(dias or 0),
        "data_inicio": _serialize_date(row.data_inicio),
        "data_fim": _serialize_date(row.data_fim),
        "status": row.status or "",
        "solicitacao": row.solicitacao or row.tipo_solicitacao or "AJUSTE",
        "observacoes": row.observacoes or "",
        "periodo_aquisitivo_origem": row.periodo_aquisitivo_origem or "",
        "v54_ignorado_no_saldo": bool(metadata.get("v54_premium_adjustment_ignored")),
        "v54_motivo_ignorado": str(metadata.get("v54_reason") or ""),
    }


def _parse_alloc(value: Any) -> List[Dict[str, Any]]:
    text = str(value or "")
    out: List[Dict[str, Any]] = []
    for numero, dias in re.findall(r"P\s*(\d+)\s*[:=\-]\s*(\d+(?:[\.,]\d+)?)", text, flags=re.IGNORECASE):
        try:
            out.append({"periodo_numero": int(numero), "dias": Decimal(str(dias).replace(",", "."))})
        except Exception:
            continue
    return out


def _format_alloc(items: List[Dict[str, Any]]) -> str:
    partes: List[str] = []
    for item in items:
        numero = int(item.get("periodo_numero") or 0)
        dias = Decimal(str(item.get("dias") or 0)).quantize(Decimal("0.01"))
        if numero <= 0 or dias <= 0:
            continue
        texto = format(dias.normalize(), "f")
        partes.append(f"P{numero}:{texto}")
    return " | ".join(partes)


def _saldo_por_periodo(session, colab: Colaborador, tipo: str, numero: int) -> Optional[SaldoPeriodoNovo]:
    """Retorna exclusivamente a linha vigente.

    ``numero`` e mantido na assinatura porque os registros historicos guardam o
    P de origem, mas nenhuma edicao pode reativar saldo em uma linha antiga.
    """
    return session.query(SaldoPeriodoNovo).filter(
        SaldoPeriodoNovo.colaborador_matricula == colab.matricula,
        SaldoPeriodoNovo.tipo_saldo == tipo,
        SaldoPeriodoNovo.is_atual.is_(True),
    ).order_by(SaldoPeriodoNovo.periodo_numero.desc()).first()


def _premium_event_affects_current(colab: Colaborador, event_date: Any) -> bool:
    try:
        from .period_accrual_service import premium_event_in_current_cycle
        return premium_event_in_current_cycle(colab.data_admissao, _as_date(event_date))
    except Exception:
        return False


def _status_ajuste_aprovado(status: Any) -> bool:
    return "APROV" in str(status or "").strip().upper()


def _ajuste_aprovado(row: Solicitacao) -> bool:
    return _status_ajuste_aprovado(row.status)


def _reverter_efeito_ajuste(session, colab: Colaborador, ajuste: Solicitacao) -> None:
    # Ajustes Premium legados da estrutura anual foram preservados apenas como
    # histórico na correção V54. Como não compuseram o saldo atual, não podem
    # ser estornados ao editar/excluir enquanto mantiverem esta marcação.
    if _ajuste_v54_ignorado(ajuste):
        return
    if not _ajuste_aprovado(ajuste):
        return
    dias = _as_decimal(ajuste.dias if ajuste.dias is not None else ajuste.dias_solicitados, "dias")
    if dias == 0:
        return
    tipo = str(ajuste.saldo_tipo or ajuste.tipo_ferias or "REGULAR").strip().upper()
    if tipo == "PREMIUM" and not _premium_event_affects_current(colab, ajuste.data_inicio):
        return
    alloc = _parse_alloc(ajuste.periodo_aquisitivo_origem)
    if not alloc:
        raise ValueError(
            "Este ajuste não possui o período de origem registrado. Edite manualmente o saldo antes de excluir ou alterar o ajuste."
        )

    for item in alloc:
        saldo = _saldo_por_periodo(session, colab, tipo, int(item["periodo_numero"]))
        if not saldo:
            raise ValueError(f"Não foi encontrada a linha {tipo} do período P{item['periodo_numero']} para estornar o ajuste.")
        qtd = Decimal(str(item["dias"]))
        atual_inicial = Decimal(str(saldo.saldo_inicial or 0))
        atual_usado = Decimal(str(saldo.saldo_utilizado or 0))
        atual_disp = Decimal(str(saldo.saldo_disponivel or 0))
        if dias > 0:
            if atual_inicial < qtd or atual_disp < qtd:
                raise ValueError(
                    f"O crédito do ajuste já foi consumido ou reservado em P{item['periodo_numero']}. "
                    "Corrija primeiro a linha de saldo antes de alterar/excluir este ajuste."
                )
            saldo.saldo_inicial = atual_inicial - qtd
            saldo.saldo_disponivel = atual_disp - qtd
        else:
            if atual_usado < qtd:
                raise ValueError(f"O saldo utilizado de P{item['periodo_numero']} é insuficiente para estornar este ajuste.")
            saldo.saldo_utilizado = atual_usado - qtd
            saldo.saldo_disponivel = atual_disp + qtd
        saldo.ultima_alteracao = dt.datetime.utcnow()
        saldo.updated_at = dt.datetime.utcnow()


def _aplicar_efeito_ajuste(
    session,
    colab: Colaborador,
    tipo: str,
    dias: Decimal,
    periodo_numero: Optional[int] = None,
    data_inicio: Any = None,
) -> List[Dict[str, Any]]:
    if dias == 0:
        raise ValueError("O ajuste deve ser diferente de zero.")
    if tipo == "PREMIUM" and not _premium_event_affects_current(colab, data_inicio):
        return []

    query = session.query(SaldoPeriodoNovo).filter(
        SaldoPeriodoNovo.colaborador_matricula == colab.matricula,
        SaldoPeriodoNovo.tipo_saldo == tipo,
        SaldoPeriodoNovo.is_atual.is_(True),
    )
    if periodo_numero:
        query = query.filter(SaldoPeriodoNovo.periodo_numero == int(periodo_numero))
    saldos = query.order_by(
        SaldoPeriodoNovo.is_atual.desc(),
        SaldoPeriodoNovo.data_inicio.desc(),
        SaldoPeriodoNovo.periodo_numero.desc(),
    ).all()
    if not saldos:
        raise ValueError(f"Não existe linha de saldo {tipo} para aplicar o ajuste.")

    movimentos: List[Dict[str, Any]] = []
    if dias > 0:
        saldo = saldos[0]
        saldo.saldo_inicial = Decimal(str(saldo.saldo_inicial or 0)) + dias
        saldo.saldo_disponivel = Decimal(str(saldo.saldo_disponivel or 0)) + dias
        saldo.ultima_alteracao = dt.datetime.utcnow()
        saldo.updated_at = dt.datetime.utcnow()
        movimentos.append({"periodo_numero": saldo.periodo_numero, "dias": dias})
        return movimentos

    restante = abs(dias)
    # Para débito sem período explícito, consome primeiro os períodos mais antigos.
    if not periodo_numero:
        saldos = list(reversed(saldos))
    for saldo in saldos:
        disponivel = Decimal(str(saldo.saldo_disponivel or 0))
        if disponivel <= 0:
            continue
        retirar = min(disponivel, restante)
        saldo.saldo_utilizado = Decimal(str(saldo.saldo_utilizado or 0)) + retirar
        saldo.saldo_disponivel = disponivel - retirar
        saldo.ultima_alteracao = dt.datetime.utcnow()
        saldo.updated_at = dt.datetime.utcnow()
        movimentos.append({"periodo_numero": saldo.periodo_numero, "dias": retirar})
        restante -= retirar
        if restante <= 0:
            break
    if restante > 0:
        raise ValueError(f"Ajuste negativo maior que o saldo disponível. Faltam {restante} dia(s).")
    return movimentos


def atualizar_ajuste_admin(
    colaborador_id: int,
    ajuste_id: int,
    payload: Dict[str, Any],
    actor_email: str = "",
) -> Dict[str, Any]:
    session = get_db_session()
    colab = session.query(Colaborador).filter(Colaborador.id == int(colaborador_id)).first()
    if not colab:
        raise ValueError("Colaborador não encontrado.")
    ajuste = session.query(Solicitacao).filter(
        Solicitacao.id == int(ajuste_id),
        Solicitacao.colaborador_matricula == colab.matricula,
        Solicitacao.is_ajuste.is_(True),
    ).first()
    if not ajuste:
        raise ValueError("Ajuste não encontrado para este colaborador.")

    before = _ajuste_dict(ajuste)
    legado_v54_ignorado = _ajuste_v54_ignorado(ajuste)
    tipo_antigo = str(ajuste.saldo_tipo or ajuste.tipo_ferias or "REGULAR").strip().upper()
    if tipo_antigo in {"CERTARIANA", "LICENCA CERTARIANA", "LICENÇA CERTARIANA"}:
        tipo_antigo = "PREMIUM"
    dias_antigos = _as_decimal(
        ajuste.dias if ajuste.dias is not None else ajuste.dias_solicitados,
        "dias",
    )
    alloc_antiga = _parse_alloc(ajuste.periodo_aquisitivo_origem)
    aprovado_antes = _ajuste_aprovado(ajuste)

    tipo = str(payload.get("saldo_tipo", tipo_antigo)).strip().upper()
    if tipo in {"CERTARIANA", "LICENCA CERTARIANA", "LICENÇA CERTARIANA"}:
        tipo = "PREMIUM"
    if tipo not in {"REGULAR", "PREMIUM"}:
        raise ValueError("Tipo de ajuste inválido.")

    dias = _as_decimal(
        payload.get("dias", ajuste.dias if ajuste.dias is not None else ajuste.dias_solicitados),
        "dias",
    )
    if dias == 0:
        raise ValueError("O ajuste deve ser diferente de zero.")
    if dias != dias.to_integral_value():
        raise ValueError("O ajuste deve ser informado em dias inteiros.")

    status = str(payload.get("status", ajuste.status or "APROVADA")).strip().upper()
    aliases_status = {
        "APROVADO": "APROVADA",
        "EM ANALISE": "EM ANÁLISE",
        "CANCELADO": "CANCELADA",
        "REPROVADA": "REPROVADO",
    }
    status = aliases_status.get(status, status)
    status_validos = {"APROVADA", "PENDENTE", "EM ANÁLISE", "CANCELADA", "REPROVADO"}
    if status not in status_validos:
        raise ValueError("Status do ajuste inválido.")
    aprovado_depois = _status_ajuste_aprovado(status)

    data_inicio = _as_date(payload.get("data_inicio", ajuste.data_inicio))
    if not data_inicio:
        raise ValueError("Data do ajuste inválida.")

    periodo_raw = payload.get("periodo_numero")
    periodo_numero = None
    if str(periodo_raw or "").strip():
        try:
            periodo_numero = int(periodo_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("Período do ajuste inválido.") from exc
        if periodo_numero <= 0:
            raise ValueError("O período do ajuste deve ser maior que zero.")

    periodo_mudou = False
    if periodo_numero is not None:
        periodo_mudou = not (
            len(alloc_antiga) == 1
            and int(alloc_antiga[0].get("periodo_numero") or 0) == periodo_numero
        )
    impacto_mudou = (
        tipo != tipo_antigo
        or dias != dias_antigos
        or periodo_mudou
        or data_inicio != ajuste.data_inicio
    )

    movimentos = alloc_antiga
    saldo_recalculado = False
    # Saiu de aprovado ou mudou o impacto de um ajuste aprovado: estorna primeiro.
    if aprovado_antes and (impacto_mudou or not aprovado_depois):
        _reverter_efeito_ajuste(session, colab, ajuste)
        saldo_recalculado = True

    # Entrou em aprovado ou mudou o impacto mantendo-se aprovado: aplica o novo efeito.
    if aprovado_depois and (impacto_mudou or not aprovado_antes):
        movimentos = _aplicar_efeito_ajuste(
            session, colab, tipo, dias, periodo_numero, data_inicio=data_inicio
        )
        saldo_recalculado = True
    elif impacto_mudou and not aprovado_depois:
        movimentos = ([{"periodo_numero": periodo_numero, "dias": abs(dias)}] if periodo_numero else alloc_antiga)

    solicitacao_label = "AJUSTE CERTARIANA" if tipo == "PREMIUM" else "AJUSTE FÉRIAS"
    ajuste.saldo_tipo = tipo
    ajuste.tipo_ferias = tipo
    ajuste.solicitacao = solicitacao_label
    ajuste.tipo_solicitacao = solicitacao_label
    ajuste.dias = int(dias)
    ajuste.dias_solicitados = dias
    ajuste.status = status
    ajuste.data_inicio = data_inicio
    ajuste.data_fim = _as_date(payload.get("data_fim")) or data_inicio
    ajuste.observacoes = str(payload.get("observacoes", ajuste.observacoes or "")).strip()
    if impacto_mudou or aprovado_antes != aprovado_depois:
        ajuste.periodo_aquisitivo_origem = _format_alloc(movimentos)

    # Ao editar materialmente um ajuste legado e aplicá-lo pela regra nova,
    # remove a marca de "somente histórico". Excluir sem editar continua sem
    # movimentar saldo, pois o lançamento nunca integrou o saldo V54.
    if legado_v54_ignorado and aprovado_depois and (impacto_mudou or not aprovado_antes) and movimentos:
        metadata = dict(ajuste.metadata_json or {})
        metadata.pop("v54_premium_adjustment_ignored", None)
        metadata.pop("v54_reason", None)
        metadata["v54_reapplied_at"] = dt.datetime.utcnow().isoformat()
        ajuste.metadata_json = metadata

    ajuste.updated_at = dt.datetime.utcnow()
    session.flush()

    after = _ajuste_dict(ajuste)
    session.add(Auditoria(
        actor_email=safe_lower(actor_email or ""),
        action="UPDATE_AJUSTE_ADMIN",
        entity_type="solicitacao_ajuste",
        entity_id=ajuste.id,
        before_data=before,
        after_data=after,
        context={
            "origem": "painel_admin",
            "colaborador_id": colab.id,
            "matricula": colab.matricula,
            "saldo_recalculado": saldo_recalculado,
        },
    ))
    session.commit()
    return obter_colaborador_admin(colab.id)

def excluir_ajuste_admin(
    colaborador_id: int,
    ajuste_id: int,
    actor_email: str = "",
) -> Dict[str, Any]:
    session = get_db_session()
    colab = session.query(Colaborador).filter(Colaborador.id == int(colaborador_id)).first()
    if not colab:
        raise ValueError("Colaborador não encontrado.")
    ajuste = session.query(Solicitacao).filter(
        Solicitacao.id == int(ajuste_id),
        Solicitacao.colaborador_matricula == colab.matricula,
        Solicitacao.is_ajuste.is_(True),
    ).first()
    if not ajuste:
        raise ValueError("Ajuste não encontrado para este colaborador.")

    before = _ajuste_dict(ajuste)
    _reverter_efeito_ajuste(session, colab, ajuste)
    session.add(Auditoria(
        actor_email=safe_lower(actor_email or ""),
        action="DELETE_AJUSTE_ADMIN",
        entity_type="solicitacao_ajuste",
        entity_id=ajuste.id,
        before_data=before,
        after_data=None,
        context={"origem": "painel_admin", "colaborador_id": colab.id, "matricula": colab.matricula},
    ))
    session.delete(ajuste)
    session.commit()
    return obter_colaborador_admin(colab.id)



def _solicitacao_dict(row: Solicitacao) -> Dict[str, Any]:
    dias = row.dias if row.dias is not None else row.dias_solicitados
    return {
        "id": row.id,
        "colaborador_matricula": row.colaborador_matricula or "",
        "solicitante_matricula": row.solicitante_matricula or "",
        "solicitante": row.criado_por or row.gestor_solicitante_email or "",
        "tipo_solicitacao": row.tipo_solicitacao or row.solicitacao or "",
        "solicitacao": row.solicitacao or row.tipo_solicitacao or "",
        "saldo_tipo": (row.saldo_tipo or row.tipo_ferias or "REGULAR").upper(),
        "dias": float(dias or 0),
        "data_inicio": _serialize_date(row.data_inicio),
        "data_fim": _serialize_date(row.data_fim),
        "status": row.status or "",
        "observacoes": row.observacoes or "",
        "periodo_aquisitivo_origem": row.periodo_aquisitivo_origem or "",
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _normalizar_tipo_saldo(value: Any) -> str:
    tipo = str(value or "REGULAR").strip().upper()
    if tipo in {"CERTARIANA", "LICENCA CERTARIANA", "LICENÇA CERTARIANA"}:
        tipo = "PREMIUM"
    if tipo not in {"REGULAR", "PREMIUM"}:
        raise ValueError("Tipo de saldo inválido.")
    return tipo


def _normalizar_status_solicitacao(value: Any) -> str:
    raw = str(value or "PENDENTE").strip().upper()
    norm = (
        raw.replace("Á", "A").replace("Ã", "A").replace("Â", "A")
        .replace("É", "E").replace("Ê", "E").replace("Í", "I")
        .replace("Ó", "O").replace("Ô", "O").replace("Õ", "O")
        .replace("Ú", "U").replace("Ç", "C")
    )
    if "APROV" in norm:
        return "APROVADA"
    if "ANALISE" in norm:
        return "EM ANÁLISE"
    if "RESERV" in norm:
        return "RESERVADA"
    if "PEND" in norm:
        return "PENDENTE"
    if "CANCEL" in norm:
        return "CANCELADA"
    if any(token in norm for token in ("REPROV", "REJEIT", "NEGAD")):
        return "REPROVADO"
    raise ValueError("Status da solicitação inválido.")


def _impacto_status_solicitacao(status: Any) -> Optional[str]:
    try:
        status = _normalizar_status_solicitacao(status)
    except ValueError:
        return None
    if status == "APROVADA":
        return "utilizado"
    if status in {"PENDENTE", "EM ANÁLISE", "RESERVADA"}:
        return "reservado"
    return None


def _is_afastamento_solicitacao(tipo_solicitacao: Any) -> bool:
    texto = str(tipo_solicitacao or "").strip().upper()
    texto = texto.replace("Á", "A").replace("Ã", "A").replace("É", "E").replace("Í", "I").replace("Ç", "C")
    return "LICENCA MATERNIDADE" in texto or "LICENCA PATERNIDADE" in texto


def _reverter_efeito_solicitacao(session, colab: Colaborador, solicitacao: Solicitacao) -> List[Dict[str, Any]]:
    impacto = _impacto_status_solicitacao(solicitacao.status)
    dias = _as_decimal(
        solicitacao.dias if solicitacao.dias is not None else solicitacao.dias_solicitados,
        "dias",
    )
    if not impacto or dias <= 0:
        return []

    alloc = _parse_alloc(solicitacao.periodo_aquisitivo_origem)
    tipo = _normalizar_tipo_saldo(solicitacao.saldo_tipo or solicitacao.tipo_ferias)
    if tipo == "PREMIUM" and not _premium_event_affects_current(colab, solicitacao.data_inicio):
        return []
    campo = "saldo_utilizado" if impacto == "utilizado" else "saldo_reservado"
    if not alloc:
        if _is_afastamento_solicitacao(solicitacao.tipo_solicitacao or solicitacao.solicitacao):
            return []
        # Alguns registros históricos migrados não possuem P de origem. Nesse
        # caso, o estorno é inferido a partir do saldo utilizado/reservado atual,
        # preservando o total e registrando a distribuição encontrada na auditoria.
        restante = dias
        movimentos_inferidos: List[Dict[str, Any]] = []
        saldos = (
            session.query(SaldoPeriodoNovo)
            .filter(
                SaldoPeriodoNovo.colaborador_matricula == colab.matricula,
                SaldoPeriodoNovo.tipo_saldo == tipo,
                SaldoPeriodoNovo.is_atual.is_(True),
            )
            .order_by(SaldoPeriodoNovo.periodo_numero.desc())
            .all()
        )
        for saldo in saldos:
            atual = Decimal(str(getattr(saldo, campo) or 0))
            if atual <= 0:
                continue
            qtd = min(atual, restante)
            setattr(saldo, campo, atual - qtd)
            saldo.saldo_disponivel = Decimal(str(saldo.saldo_disponivel or 0)) + qtd
            saldo.ultima_alteracao = dt.datetime.utcnow()
            saldo.updated_at = dt.datetime.utcnow()
            movimentos_inferidos.append({"periodo_numero": saldo.periodo_numero, "dias": qtd})
            restante -= qtd
            if restante <= 0:
                break
        if restante > 0:
            nome_campo = "utilizado" if campo == "saldo_utilizado" else "reservado"
            raise ValueError(
                f"Não foi possível inferir os períodos do histórico: faltam {restante} dia(s) no saldo {nome_campo}."
            )
        return movimentos_inferidos

    movimentos: List[Dict[str, Any]] = []
    for item in alloc:
        numero = int(item.get("periodo_numero") or 0)
        qtd = Decimal(str(item.get("dias") or 0))
        saldo = _saldo_por_periodo(session, colab, tipo, numero)
        if not saldo:
            raise ValueError(f"Não foi encontrada a linha {tipo} do período P{numero} para estornar a solicitação.")
        atual = Decimal(str(getattr(saldo, campo) or 0))
        if atual < qtd:
            nome_campo = "utilizado" if campo == "saldo_utilizado" else "reservado"
            raise ValueError(
                f"O saldo {nome_campo} de P{numero} é menor que o valor desta solicitação. "
                "Revise a linha de saldo antes de alterar ou excluir."
            )
        setattr(saldo, campo, atual - qtd)
        saldo.saldo_disponivel = Decimal(str(saldo.saldo_disponivel or 0)) + qtd
        saldo.ultima_alteracao = dt.datetime.utcnow()
        saldo.updated_at = dt.datetime.utcnow()
        movimentos.append({"periodo_numero": numero, "dias": qtd})
    return movimentos


def _aplicar_efeito_solicitacao(
    session,
    colab: Colaborador,
    tipo: str,
    dias: Decimal,
    status: str,
    tipo_solicitacao: str,
    preferred_alloc: Optional[List[Dict[str, Any]]] = None,
    data_inicio: Any = None,
) -> List[Dict[str, Any]]:
    impacto = _impacto_status_solicitacao(status)
    if not impacto or dias <= 0 or _is_afastamento_solicitacao(tipo_solicitacao):
        return []

    tipo = _normalizar_tipo_saldo(tipo)
    if tipo == "PREMIUM" and not _premium_event_affects_current(colab, data_inicio):
        return []
    campo = "saldo_utilizado" if impacto == "utilizado" else "saldo_reservado"
    movimentos: List[Dict[str, Any]] = []

    preferred = preferred_alloc or []
    soma_preferred = sum((Decimal(str(item.get("dias") or 0)) for item in preferred), Decimal("0"))
    if preferred and soma_preferred == dias:
        saldos_preferred: List[tuple[SaldoPeriodoNovo, Decimal]] = []
        pode_usar = True
        for item in preferred:
            numero = int(item.get("periodo_numero") or 0)
            qtd = Decimal(str(item.get("dias") or 0))
            saldo = _saldo_por_periodo(session, colab, tipo, numero)
            if not saldo or Decimal(str(saldo.saldo_disponivel or 0)) < qtd:
                pode_usar = False
                break
            saldos_preferred.append((saldo, qtd))
        if pode_usar:
            for saldo, qtd in saldos_preferred:
                setattr(saldo, campo, Decimal(str(getattr(saldo, campo) or 0)) + qtd)
                saldo.saldo_disponivel = Decimal(str(saldo.saldo_disponivel or 0)) - qtd
                saldo.ultima_alteracao = dt.datetime.utcnow()
                saldo.updated_at = dt.datetime.utcnow()
                movimentos.append({"periodo_numero": saldo.periodo_numero, "dias": qtd})
            return movimentos

    saldos = (
        session.query(SaldoPeriodoNovo)
        .filter(
            SaldoPeriodoNovo.colaborador_matricula == colab.matricula,
            SaldoPeriodoNovo.tipo_saldo == tipo,
            SaldoPeriodoNovo.is_atual.is_(True),
        )
        .order_by(SaldoPeriodoNovo.periodo_numero.desc())
        .all()
    )
    restante = dias
    for saldo in saldos:
        disponivel = Decimal(str(saldo.saldo_disponivel or 0))
        if disponivel <= 0:
            continue
        consumir = min(disponivel, restante)
        setattr(saldo, campo, Decimal(str(getattr(saldo, campo) or 0)) + consumir)
        saldo.saldo_disponivel = disponivel - consumir
        saldo.ultima_alteracao = dt.datetime.utcnow()
        saldo.updated_at = dt.datetime.utcnow()
        movimentos.append({"periodo_numero": saldo.periodo_numero, "dias": consumir})
        restante -= consumir
        if restante <= 0:
            break
    if restante > 0:
        raise ValueError(f"Saldo insuficiente para atualizar a solicitação. Faltam {restante} dia(s).")
    return movimentos


def atualizar_solicitacao_admin(
    colaborador_id: int,
    solicitacao_id: int,
    payload: Dict[str, Any],
    actor_email: str = "",
) -> Dict[str, Any]:
    session = get_db_session()
    colab = session.query(Colaborador).filter(Colaborador.id == int(colaborador_id)).first()
    if not colab:
        raise ValueError("Colaborador não encontrado.")

    solicitacao = session.query(Solicitacao).filter(
        Solicitacao.id == int(solicitacao_id),
        _solicitacao_vinculo_expr(colab),
        or_(Solicitacao.is_ajuste.is_(False), Solicitacao.is_ajuste.is_(None)),
    ).first()
    if not solicitacao:
        raise ValueError("Solicitação não encontrada para este colaborador.")

    before = _solicitacao_dict(solicitacao)
    tipo_antigo = _normalizar_tipo_saldo(solicitacao.saldo_tipo or solicitacao.tipo_ferias)
    dias_antigos = _as_decimal(
        solicitacao.dias if solicitacao.dias is not None else solicitacao.dias_solicitados,
        "dias",
    )
    status_antigo = _normalizar_status_solicitacao(solicitacao.status)
    tipo_solic_antigo = str(solicitacao.tipo_solicitacao or solicitacao.solicitacao or "GOZO").strip().upper()
    alloc_antiga = _parse_alloc(solicitacao.periodo_aquisitivo_origem)

    tipo = _normalizar_tipo_saldo(payload.get("saldo_tipo", tipo_antigo))
    dias = _as_decimal(payload.get("dias", dias_antigos), "dias")
    if dias <= 0:
        raise ValueError("A quantidade de dias deve ser maior que zero.")
    if dias != dias.to_integral_value():
        raise ValueError("A quantidade deve ser informada em dias inteiros.")

    status = _normalizar_status_solicitacao(payload.get("status", status_antigo))
    tipo_solic = str(payload.get("tipo_solicitacao", tipo_solic_antigo) or "GOZO").strip().upper()
    if not tipo_solic:
        raise ValueError("Informe o tipo da solicitação.")

    data_inicio = _as_date(payload.get("data_inicio", solicitacao.data_inicio))
    data_fim = _as_date(payload.get("data_fim", solicitacao.data_fim))
    if not data_inicio or not data_fim:
        raise ValueError("Informe as datas inicial e final.")
    if data_fim < data_inicio:
        raise ValueError("A data final não pode ser anterior à data inicial.")

    impacto_antigo = _impacto_status_solicitacao(status_antigo)
    impacto_novo = _impacto_status_solicitacao(status)
    afast_antigo = _is_afastamento_solicitacao(tipo_solic_antigo)
    afast_novo = _is_afastamento_solicitacao(tipo_solic)
    impacto_mudou = (
        tipo != tipo_antigo
        or dias != dias_antigos
        or impacto_novo != impacto_antigo
        or afast_novo != afast_antigo
        or data_inicio != solicitacao.data_inicio
    )

    movimentos = alloc_antiga
    saldo_recalculado = False
    if impacto_mudou:
        _reverter_efeito_solicitacao(session, colab, solicitacao)
        preferred = alloc_antiga if tipo == tipo_antigo and dias == dias_antigos else None
        movimentos = _aplicar_efeito_solicitacao(
            session,
            colab,
            tipo,
            dias,
            status,
            tipo_solic,
            preferred_alloc=preferred,
            data_inicio=data_inicio,
        )
        saldo_recalculado = bool(impacto_antigo or impacto_novo)

    solicitacao.colaborador_id = colab.id
    solicitacao.colaborador_matricula = colab.matricula
    solicitacao.colaborador_email = safe_lower(colab.email or solicitacao.colaborador_email or "") or None
    solicitacao.tipo_solicitacao = tipo_solic
    solicitacao.solicitacao = tipo_solic
    solicitacao.tipo_ferias = tipo
    solicitacao.saldo_tipo = tipo
    solicitacao.dias_solicitados = dias
    solicitacao.dias = int(dias)
    solicitacao.status = status
    solicitacao.data_inicio = data_inicio
    solicitacao.data_fim = data_fim
    solicitacao.observacoes = str(payload.get("observacoes", solicitacao.observacoes or "")).strip()
    if impacto_mudou:
        solicitacao.periodo_aquisitivo_origem = _format_alloc(movimentos) if movimentos else None
    solicitacao.updated_at = dt.datetime.utcnow()
    session.flush()

    after = _solicitacao_dict(solicitacao)
    session.add(Auditoria(
        actor_email=safe_lower(actor_email or ""),
        action="UPDATE_SOLICITACAO_ADMIN",
        entity_type="solicitacao_ferias",
        entity_id=solicitacao.id,
        before_data=before,
        after_data=after,
        context={
            "origem": "painel_admin",
            "colaborador_id": colab.id,
            "matricula": colab.matricula,
            "saldo_recalculado": saldo_recalculado,
        },
    ))
    session.commit()
    return obter_colaborador_admin(colab.id)


def excluir_solicitacao_admin(
    colaborador_id: int,
    solicitacao_id: int,
    actor_email: str = "",
) -> Dict[str, Any]:
    session = get_db_session()
    colab = session.query(Colaborador).filter(Colaborador.id == int(colaborador_id)).first()
    if not colab:
        raise ValueError("Colaborador não encontrado.")

    solicitacao = session.query(Solicitacao).filter(
        Solicitacao.id == int(solicitacao_id),
        _solicitacao_vinculo_expr(colab),
        or_(Solicitacao.is_ajuste.is_(False), Solicitacao.is_ajuste.is_(None)),
    ).first()
    if not solicitacao:
        raise ValueError("Solicitação não encontrada para este colaborador.")

    before = _solicitacao_dict(solicitacao)
    _reverter_efeito_solicitacao(session, colab, solicitacao)
    session.add(Auditoria(
        actor_email=safe_lower(actor_email or ""),
        action="DELETE_SOLICITACAO_ADMIN",
        entity_type="solicitacao_ferias",
        entity_id=solicitacao.id,
        before_data=before,
        after_data=None,
        context={"origem": "painel_admin", "colaborador_id": colab.id, "matricula": colab.matricula},
    ))
    session.delete(solicitacao)
    session.commit()
    return obter_colaborador_admin(colab.id)
