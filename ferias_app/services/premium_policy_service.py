from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal
from typing import Any

from sqlalchemy import func

from ..logging_config import get_logger
from ..models import AdminConfig, Auditoria, Colaborador, SaldoPeriodoNovo, Solicitacao

log = get_logger(__name__)

RULE_PREMIUM_ACCUMULATION = "PREMIUM_ACCUMULATION"
TARGET_MATRICULA = "MATRICULA"


def _norm_mat(value: Any) -> str:
    return str(value or "").strip().upper()


def _norm_status(value: Any) -> str:
    raw = str(value or "").strip().upper()
    return (
        raw.replace("Á", "A").replace("Ã", "A").replace("Â", "A")
        .replace("É", "E").replace("Ê", "E").replace("Í", "I")
        .replace("Ó", "O").replace("Ô", "O").replace("Õ", "O")
        .replace("Ú", "U").replace("Ç", "C")
    )


def _is_approved(value: Any) -> bool:
    return "APROV" in _norm_status(value)


def _is_reserved(value: Any) -> bool:
    st = _norm_status(value)
    return any(token in st for token in ("PEND", "ANALISE", "RESERV"))


def _is_adjustment(row: Solicitacao) -> bool:
    txt = f"{row.tipo_solicitacao or ''} {row.solicitacao or ''}".upper()
    return bool(row.is_ajuste) or "AJUSTE" in txt


def premium_accumulation_enabled(matricula: str, *, session=None, at: dt.datetime | None = None) -> bool:
    """Retorna se a matrícula possui exceção ativa de acumulação Premium."""
    mat = _norm_mat(matricula)
    if not mat:
        return False
    own_session = session is None
    if own_session:
        from .postgres_service import get_db_session
        session = get_db_session()
    now = at or dt.datetime.utcnow()
    q = session.query(AdminConfig).filter(
        AdminConfig.rule_type == RULE_PREMIUM_ACCUMULATION,
        AdminConfig.target_type == TARGET_MATRICULA,
        func.upper(func.trim(AdminConfig.target_value)) == mat,
        AdminConfig.enabled.is_(True),
    )
    rows = q.order_by(AdminConfig.id.desc()).all()
    for row in rows:
        if row.valid_from and row.valid_from > now:
            continue
        if row.valid_until and row.valid_until < now:
            continue
        return True
    return False


def _parse_alloc_period(value: Any) -> int | None:
    m = re.search(r"\bP\s*(\d+)\b", str(value or ""), flags=re.IGNORECASE)
    return int(m.group(1)) if m else None


def _as_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except Exception:
        return Decimal("0")


def initialize_premium_accumulation(session, colab: Colaborador, *, reference_date: dt.date | None = None) -> dict[str, Any]:
    """Reconstrói uma vez os ciclos Premium acumuláveis da matrícula.

    A reconstrução parte dos créditos legais já adquiridos (P1=30, P2+=15),
    aplica ajustes aprovados no período de origem quando identificável e distribui
    solicitações aprovadas/reservadas em FIFO entre os créditos disponíveis.

    Essa rotina é chamada ao habilitar a exceção. A rotina diária subsequente não
    recalcula os ciclos já existentes; apenas deixa de expirar os históricos e cria
    novos créditos quando vencerem.
    """
    from .period_accrual_service import business_today, premium_cycles

    reference_date = reference_date or business_today()
    mat = _norm_mat(colab.matricula)
    cycles = premium_cycles(colab.data_admissao, reference_date)
    if not cycles:
        return {"cycles": 0, "available": 0, "used": 0, "reserved": 0}

    rows = session.query(SaldoPeriodoNovo).filter(
        func.upper(func.trim(SaldoPeriodoNovo.colaborador_matricula)) == mat,
        func.upper(func.trim(SaldoPeriodoNovo.tipo_saldo)) == "PREMIUM",
    ).all()
    by_number = {int(r.periodo_numero or 0): r for r in rows}
    valid_numbers = {c.numero for c in cycles}
    for row in rows:
        if int(row.periodo_numero or 0) not in valid_numbers:
            session.delete(row)

    now = dt.datetime.utcnow()
    state: dict[int, dict[str, Decimal]] = {}
    for cycle in cycles:
        state[cycle.numero] = {
            "initial": Decimal(str(cycle.base)),
            "used": Decimal("0"),
            "reserved": Decimal("0"),
        }

    # Ajustes aprovados alteram o direito, não "dias usados".
    adjustments = session.query(Solicitacao).filter(
        func.upper(func.trim(Solicitacao.colaborador_matricula)) == mat,
        func.upper(func.trim(func.coalesce(Solicitacao.saldo_tipo, Solicitacao.tipo_ferias, "REGULAR"))) == "PREMIUM",
        Solicitacao.is_ajuste.is_(True),
    ).order_by(Solicitacao.id.asc()).all()
    for adj in adjustments:
        if not _is_approved(adj.status):
            continue
        metadata = adj.metadata_json if isinstance(adj.metadata_json, dict) else {}
        if metadata.get("v54_premium_adjustment_ignored"):
            continue
        dias = _as_decimal(adj.dias if adj.dias is not None else adj.dias_solicitados)
        numero = _parse_alloc_period(adj.periodo_aquisitivo_origem)
        if numero not in state:
            numero = cycles[-1].numero
        state[numero]["initial"] = max(Decimal("0"), state[numero]["initial"] + dias)

    # Solicitações Premium usam o pool acumulado em FIFO. A data da solicitação
    # não muda o fato de que o saldo foi reservado/aprovado no app atual.
    requests = session.query(Solicitacao).filter(
        func.upper(func.trim(Solicitacao.colaborador_matricula)) == mat,
        func.upper(func.trim(func.coalesce(Solicitacao.saldo_tipo, Solicitacao.tipo_ferias, "REGULAR"))) == "PREMIUM",
        func.coalesce(Solicitacao.is_ajuste, False).is_(False),
    ).order_by(Solicitacao.id.asc()).all()

    def consume(amount: Decimal, bucket: str) -> Decimal:
        remaining = max(Decimal("0"), amount)
        for cycle in cycles:
            st = state[cycle.numero]
            available = max(Decimal("0"), st["initial"] - st["used"] - st["reserved"])
            if available <= 0:
                continue
            take = min(available, remaining)
            st[bucket] += take
            remaining -= take
            if remaining <= 0:
                break
        return remaining

    overflow = Decimal("0")
    for req in requests:
        dias = abs(_as_decimal(req.dias if req.dias is not None else req.dias_solicitados))
        if dias <= 0:
            continue
        if _is_approved(req.status):
            overflow += consume(dias, "used")
        elif _is_reserved(req.status):
            overflow += consume(dias, "reserved")

    # Se o histórico registrado supera os créditos legais/ajustes conhecidos,
    # preserva o histórico sem saldo negativo adicionando o déficit ao ciclo atual.
    if overflow > 0:
        state[cycles[-1].numero]["initial"] += overflow
        # reaplica o excedente como utilizado por segurança (aprovados predominam
        # nos dados legados; o objetivo é não criar saldo disponível artificial).
        state[cycles[-1].numero]["used"] += overflow
        log.warning(
            "Premium acumulável %s: histórico excedeu créditos conhecidos em %s dia(s); "
            "direito do ciclo atual foi ajustado apenas para preservar consistência.",
            mat, overflow,
        )

    for cycle in cycles:
        row = by_number.get(cycle.numero)
        if row is None:
            row = SaldoPeriodoNovo(
                colaborador_id=colab.id,
                colaborador_matricula=mat,
                periodo_numero=cycle.numero,
                data_inicio=cycle.data_inicio,
                data_fim=cycle.data_fim,
                is_atual=False,
                tipo_saldo="PREMIUM",
                saldo_inicial=0,
                saldo_utilizado=0,
                saldo_reservado=0,
                saldo_disponivel=0,
                ultima_alteracao=now,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            by_number[cycle.numero] = row
        st = state[cycle.numero]
        row.colaborador_id = colab.id
        row.colaborador_matricula = mat
        row.periodo_numero = cycle.numero
        row.data_inicio = cycle.data_inicio
        row.data_fim = cycle.data_fim
        row.tipo_saldo = "PREMIUM"
        row.is_atual = cycle.numero == cycles[-1].numero
        row.saldo_inicial = st["initial"]
        row.saldo_utilizado = st["used"]
        row.saldo_reservado = st["reserved"]
        row.saldo_disponivel = max(Decimal("0"), st["initial"] - st["used"] - st["reserved"])
        row.ultima_alteracao = now
        row.updated_at = now

    return {
        "cycles": len(cycles),
        "available": float(sum((max(Decimal("0"), st["initial"] - st["used"] - st["reserved"]) for st in state.values()), Decimal("0"))),
        "used": float(sum((st["used"] for st in state.values()), Decimal("0"))),
        "reserved": float(sum((st["reserved"] for st in state.values()), Decimal("0"))),
    }


def set_premium_accumulation(
    session,
    colab: Colaborador,
    enabled: bool,
    *,
    actor_email: str = "",
    reason: str = "",
    initialize: bool = True,
) -> dict[str, Any]:
    """Habilita/desabilita a exceção por matrícula e registra auditoria."""
    mat = _norm_mat(colab.matricula)
    if not mat:
        raise ValueError("Colaborador sem matrícula.")

    existing = session.query(AdminConfig).filter(
        AdminConfig.rule_type == RULE_PREMIUM_ACCUMULATION,
        AdminConfig.target_type == TARGET_MATRICULA,
        func.upper(func.trim(AdminConfig.target_value)) == mat,
        AdminConfig.enabled.is_(True),
    ).order_by(AdminConfig.id.desc()).all()

    before = bool(existing)
    now = dt.datetime.utcnow()
    init_result = None

    if enabled:
        if existing:
            cfg = existing[0]
            cfg.updated_at = now
            cfg.reason = reason or cfg.reason
        else:
            cfg = AdminConfig(
                rule_type=RULE_PREMIUM_ACCUMULATION,
                target_type=TARGET_MATRICULA,
                target_value=mat,
                enabled=True,
                reason=reason or "Exceção de acumulação da Licença Certariana",
                config_data={"policy": "accumulate_premium_cycles"},
                created_by=str(actor_email or "admin"),
                created_at=now,
                updated_at=now,
            )
            session.add(cfg)
            session.flush()
        if initialize and not before:
            init_result = initialize_premium_accumulation(session, colab)
    else:
        for cfg in existing:
            cfg.enabled = False
            cfg.revoked_by = str(actor_email or "admin")
            cfg.revoked_at = now
            cfg.updated_at = now
        # Ao remover a exceção, volta imediatamente à regra padrão: somente o
        # ciclo Premium vigente mantém saldo; históricos permanecem como linhas,
        # mas zerados. A reativação futura pode reconstruí-los a partir do histórico.
        rows = session.query(SaldoPeriodoNovo).filter(
            func.upper(func.trim(SaldoPeriodoNovo.colaborador_matricula)) == mat,
            func.upper(func.trim(SaldoPeriodoNovo.tipo_saldo)) == "PREMIUM",
        ).all()
        for row in rows:
            if not bool(row.is_atual):
                row.saldo_inicial = 0
                row.saldo_utilizado = 0
                row.saldo_reservado = 0
                row.saldo_disponivel = 0
                row.ultima_alteracao = now
                row.updated_at = now

    session.add(Auditoria(
        actor_email=str(actor_email or "admin"),
        action="SET_PREMIUM_ACCUMULATION",
        entity_type="colaborador",
        entity_id=colab.id,
        before_data={"premium_acumula": before},
        after_data={"premium_acumula": bool(enabled)},
        context={"matricula": mat, "init_result": init_result, "reason": reason or ""},
    ))
    return {"enabled": bool(enabled), "initialized": init_result}
