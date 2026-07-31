from __future__ import annotations

import calendar
import datetime as dt
import threading
import time
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, or_, text

from ..config import get_settings
from ..logging_config import get_logger
from ..models import Auditoria, Colaborador, SaldoPeriodoNovo, Solicitacao, SyncState
from .postgres_service import get_session

log = get_logger(__name__)

_SYNC_NAME = "ciclos_saldos_v59"
_ADVISORY_LOCK_KEY = 5700729
_LOCAL_CHECK_INTERVAL_SECONDS = 1800
_local_lock = threading.Lock()
_last_local_check = 0.0
_thread_running = False
_ACTIVE_STATUSES = {"ATIVO", "ACTIVE"}


@dataclass(frozen=True)
class Cycle:
    tipo: str
    numero: int
    data_inicio: dt.date
    data_fim: dt.date
    credito_em: dt.date
    proximo_credito_em: dt.date
    base: float


def business_today() -> dt.date:
    """Data corrente no fuso de negócio."""
    timezone_name = str(get_settings().app_timezone or "America/Fortaleza").strip()
    try:
        timezone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("APP_TIMEZONE inválido (%s); usando America/Fortaleza", timezone_name)
        timezone = ZoneInfo("America/Fortaleza")
    return dt.datetime.now(timezone).date()


def _business_date_from_utc(value: dt.datetime | None) -> dt.date | None:
    if not value:
        return None
    timezone_name = str(get_settings().app_timezone or "America/Fortaleza").strip()
    try:
        timezone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        timezone = ZoneInfo("America/Fortaleza")
    stamp = value
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    return stamp.astimezone(timezone).date()


def add_months(value: dt.date, months: int) -> dt.date:
    year = value.year + (value.month - 1 + months) // 12
    month = (value.month - 1 + months) % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return dt.date(year, month, day)


def regular_cycles(admissao: dt.date | None, reference_date: dt.date) -> list[Cycle]:
    """Retorna somente ciclos anuais já concluídos.

    O ciclo em formação nunca é gravado. Para admissão em 11/02/2019, P7
    corresponde a 11/02/2025–10/02/2026 e passa a existir em 11/02/2026.
    """
    if not admissao or reference_date < admissao:
        return []
    out: list[Cycle] = []
    numero = 1
    while True:
        credito = add_months(admissao, numero * 12)
        if credito > reference_date:
            break
        out.append(Cycle(
            tipo="REGULAR",
            numero=numero,
            data_inicio=add_months(admissao, (numero - 1) * 12),
            data_fim=credito - dt.timedelta(days=1),
            credito_em=credito,
            proximo_credito_em=add_months(admissao, (numero + 1) * 12),
            base=30.0,
        ))
        numero += 1
    return out


def premium_cycles(admissao: dt.date | None, reference_date: dt.date) -> list[Cycle]:
    """Retorna os ciclos Premium já adquiridos.

    P1: 30 dias, no dia seguinte ao fechamento de cinco anos.
    P2+: 15 dias a cada 30 meses. O ciclo anterior expira quando nasce o novo.
    """
    if not admissao:
        return []
    primeiro_credito = add_months(admissao, 60) + dt.timedelta(days=1)
    if primeiro_credito > reference_date:
        return []

    out: list[Cycle] = []
    numero = 1
    while True:
        credito = add_months(primeiro_credito, (numero - 1) * 30)
        if credito > reference_date:
            break
        proximo = add_months(primeiro_credito, numero * 30)
        inicio_aquisicao = admissao if numero == 1 else add_months(primeiro_credito, (numero - 2) * 30)
        out.append(Cycle(
            tipo="PREMIUM",
            numero=numero,
            data_inicio=inicio_aquisicao,
            data_fim=credito - dt.timedelta(days=1),
            credito_em=credito,
            proximo_credito_em=proximo,
            base=30.0 if numero == 1 else 15.0,
        ))
        numero += 1
    return out


def premium_event_in_current_cycle(
    admissao: dt.date | None,
    event_date: dt.date | dt.datetime | None,
    reference_date: dt.date | None = None,
) -> bool:
    if isinstance(event_date, dt.datetime):
        event_date = event_date.date()
    reference_date = reference_date or business_today()
    cycles = premium_cycles(admissao, reference_date)
    if not cycles or not event_date:
        return False
    current = cycles[-1]
    return current.credito_em <= event_date < current.proximo_credito_em


def _normalize(value: object) -> str:
    raw = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(ch for ch in raw if not unicodedata.combining(ch)).strip().upper()


def _is_active(colab: Colaborador) -> bool:
    return _normalize(colab.status or "ATIVO") in _ACTIVE_STATUSES


def _as_float(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _create_balance_row(
    session,
    colab: Colaborador,
    cycle: Cycle,
    *,
    initial: float,
    used: float,
    reserved: float,
    is_current: bool,
) -> SaldoPeriodoNovo:
    now = dt.datetime.utcnow()
    available = max(0.0, initial - used - reserved)
    row = SaldoPeriodoNovo(
        colaborador_id=colab.id,
        colaborador_matricula=str(colab.matricula).strip().upper(),
        periodo_numero=cycle.numero,
        data_inicio=cycle.data_inicio,
        data_fim=cycle.data_fim,
        is_atual=is_current,
        tipo_saldo=cycle.tipo,
        saldo_inicial=initial,
        saldo_utilizado=used,
        saldo_reservado=reserved,
        saldo_disponivel=available,
        ultima_alteracao=now,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    return row


def _zero_historical(row: SaldoPeriodoNovo) -> bool:
    changed = bool(row.is_atual) or any(abs(_as_float(value)) > 0.0001 for value in (
        row.saldo_inicial,
        row.saldo_utilizado,
        row.saldo_reservado,
        row.saldo_disponivel,
    ))
    row.is_atual = False
    row.saldo_inicial = 0
    row.saldo_utilizado = 0
    row.saldo_reservado = 0
    row.saldo_disponivel = 0
    if changed:
        now = dt.datetime.utcnow()
        row.ultima_alteracao = now
        row.updated_at = now
    return changed


def _request_state_for_premium(session, matricula: str, cycle: Cycle) -> tuple[float, float, float]:
    """Reconstrói o ciclo Premium vigente a partir do histórico do próprio ciclo."""
    rows = session.query(Solicitacao).filter(
        func.upper(func.trim(Solicitacao.colaborador_matricula)) == matricula,
        func.upper(func.trim(func.coalesce(Solicitacao.saldo_tipo, Solicitacao.tipo_ferias, "REGULAR"))) == "PREMIUM",
        Solicitacao.data_inicio >= cycle.credito_em,
        Solicitacao.data_inicio < cycle.proximo_credito_em,
    ).all()

    used = 0.0
    reserved = 0.0
    adjustments = 0.0
    for row in rows:
        days = _as_float(row.dias if row.dias is not None else row.dias_solicitados)
        status = _normalize(row.status)
        request_type = _normalize(row.tipo_solicitacao)
        request_name = _normalize(row.solicitacao)
        is_adjustment = bool(row.is_ajuste) or request_type == "AJUSTE" or "AJUSTE" in request_name
        metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
        if is_adjustment:
            if metadata.get("v54_premium_adjustment_ignored"):
                continue
            if status in {"APROVADO", "APROVADA"}:
                adjustments += days
            continue
        days = abs(days)
        if status in {"APROVADO", "APROVADA"}:
            used += days
        elif status in {"PENDENTE", "EM ANALISE", "ANALISE", "RESERVA", "RESERVADO", "RESERVADA"}:
            reserved += days
    return used, reserved, adjustments


def _clean_invalid_rows(
    session,
    colab: Colaborador,
    tipo: str,
    valid_numbers: set[int],
) -> tuple[list[SaldoPeriodoNovo], int]:
    mat = str(colab.matricula).strip().upper()
    rows = session.query(SaldoPeriodoNovo).filter(
        func.upper(func.trim(SaldoPeriodoNovo.colaborador_matricula)) == mat,
        func.upper(func.trim(SaldoPeriodoNovo.tipo_saldo)) == tipo,
    ).order_by(SaldoPeriodoNovo.periodo_numero.asc(), SaldoPeriodoNovo.id.asc()).all()
    valid: list[SaldoPeriodoNovo] = []
    deleted = 0
    for row in rows:
        if int(row.periodo_numero or 0) not in valid_numbers:
            session.delete(row)
            deleted += 1
        else:
            valid.append(row)
    return valid, deleted


def _ensure_regular_cycles(session, colab: Colaborador, cycles: list[Cycle]) -> dict[str, int]:
    valid_numbers = {cycle.numero for cycle in cycles}
    rows, deleted = _clean_invalid_rows(session, colab, "REGULAR", valid_numbers)
    if not cycles:
        return {"created": 0, "deleted": deleted, "zeroed": 0, "rolled": 0}

    by_number = {int(row.periodo_numero): row for row in rows}
    current_cycle = cycles[-1]
    current_row = by_number.get(current_cycle.numero)
    previous_current = next((row for row in sorted(rows, key=lambda item: (bool(item.is_atual), int(item.periodo_numero or 0), int(item.id or 0)), reverse=True) if row.is_atual), None)
    previous_state = None
    if previous_current is not None:
        previous_state = (
            max(0.0, _as_float(previous_current.saldo_disponivel)),
            max(0.0, _as_float(previous_current.saldo_reservado)),
        )
    created = zeroed = rolled = 0

    # Primeiro desmarca/zera o histórico para não existir mais de uma linha vigente.
    for row in rows:
        if row is not current_row and _zero_historical(row):
            zeroed += 1

    if current_row is None:
        if previous_state is not None:
            previous_available, previous_reserved = previous_state
            initial = previous_available + previous_reserved + current_cycle.base
            used = 0.0
            reserved = previous_reserved
            rolled += 1
        else:
            initial = current_cycle.base
            used = 0.0
            reserved = 0.0
        current_row = _create_balance_row(
            session,
            colab,
            current_cycle,
            initial=initial,
            used=used,
            reserved=reserved,
            is_current=True,
        )
        by_number[current_cycle.numero] = current_row
        created += 1
    else:
        # Uma linha vigente completamente zerada é vestígio da estrutura antiga.
        if not any(abs(_as_float(value)) > 0.0001 for value in (
            current_row.saldo_inicial,
            current_row.saldo_utilizado,
            current_row.saldo_reservado,
            current_row.saldo_disponivel,
        )):
            current_row.saldo_inicial = current_cycle.base
            current_row.saldo_utilizado = 0
            current_row.saldo_reservado = 0
            current_row.saldo_disponivel = current_cycle.base
        current_row.is_atual = True

    now = dt.datetime.utcnow()
    for cycle in cycles:
        row = by_number.get(cycle.numero)
        if row is None:
            row = _create_balance_row(
                session,
                colab,
                cycle,
                initial=0,
                used=0,
                reserved=0,
                is_current=False,
            )
            by_number[cycle.numero] = row
            created += 1
        row.colaborador_id = colab.id
        row.colaborador_matricula = str(colab.matricula).strip().upper()
        row.tipo_saldo = "REGULAR"
        row.data_inicio = cycle.data_inicio
        row.data_fim = cycle.data_fim
        if cycle.numero == current_cycle.numero:
            row.is_atual = True
            row.saldo_disponivel = max(0.0, _as_float(row.saldo_inicial) - _as_float(row.saldo_utilizado) - _as_float(row.saldo_reservado))
        else:
            _zero_historical(row)
        row.ultima_alteracao = row.ultima_alteracao or now
        row.updated_at = now

    return {"created": created, "deleted": deleted, "zeroed": zeroed, "rolled": rolled}


def _ensure_premium_cycles(session, colab: Colaborador, cycles: list[Cycle]) -> dict[str, int]:
    valid_numbers = {cycle.numero for cycle in cycles}
    rows, deleted = _clean_invalid_rows(session, colab, "PREMIUM", valid_numbers)
    if not cycles:
        return {"created": 0, "deleted": deleted, "zeroed": 0, "recalculated": 0}

    by_number = {int(row.periodo_numero): row for row in rows}
    current_cycle = cycles[-1]
    current_row = by_number.get(current_cycle.numero)
    created = zeroed = recalculated = 0

    for row in rows:
        if row is not current_row and _zero_historical(row):
            zeroed += 1

    if current_row is None:
        used, reserved, adjustments = _request_state_for_premium(
            session,
            str(colab.matricula).strip().upper(),
            current_cycle,
        )
        initial = max(0.0, current_cycle.base + adjustments)
        if initial < used + reserved:
            log.warning(
                "Saldo Premium inconsistente para %s P%s: direito %.2f, utilizado %.2f, reservado %.2f. Ajustando direito para cobrir movimentos.",
                colab.matricula,
                current_cycle.numero,
                initial,
                used,
                reserved,
            )
            initial = used + reserved
        current_row = _create_balance_row(
            session,
            colab,
            current_cycle,
            initial=initial,
            used=used,
            reserved=reserved,
            is_current=True,
        )
        by_number[current_cycle.numero] = current_row
        created += 1
        recalculated += 1
    else:
        if not any(abs(_as_float(value)) > 0.0001 for value in (
            current_row.saldo_inicial,
            current_row.saldo_utilizado,
            current_row.saldo_reservado,
            current_row.saldo_disponivel,
        )):
            used, reserved, adjustments = _request_state_for_premium(
                session,
                str(colab.matricula).strip().upper(),
                current_cycle,
            )
            initial = max(current_cycle.base + adjustments, used + reserved, 0.0)
            current_row.saldo_inicial = initial
            current_row.saldo_utilizado = used
            current_row.saldo_reservado = reserved
            current_row.saldo_disponivel = max(0.0, initial - used - reserved)
            recalculated += 1
        current_row.is_atual = True

    now = dt.datetime.utcnow()
    for cycle in cycles:
        row = by_number.get(cycle.numero)
        if row is None:
            row = _create_balance_row(
                session,
                colab,
                cycle,
                initial=0,
                used=0,
                reserved=0,
                is_current=False,
            )
            by_number[cycle.numero] = row
            created += 1
        row.colaborador_id = colab.id
        row.colaborador_matricula = str(colab.matricula).strip().upper()
        row.tipo_saldo = "PREMIUM"
        row.data_inicio = cycle.data_inicio
        row.data_fim = cycle.data_fim
        if cycle.numero == current_cycle.numero:
            row.is_atual = True
            row.saldo_disponivel = max(0.0, _as_float(row.saldo_inicial) - _as_float(row.saldo_utilizado) - _as_float(row.saldo_reservado))
        else:
            _zero_historical(row)
        row.ultima_alteracao = row.ultima_alteracao or now
        row.updated_at = now

    return {"created": created, "deleted": deleted, "zeroed": zeroed, "recalculated": recalculated}


def ensure_due_periods(
    reference_date: dt.date | None = None,
    actor_email: str = "daily-period-service",
    *,
    force: bool = False,
    wait_for_lock: bool = True,
) -> dict:
    """Cria/normaliza ciclos vencidos somente para colaboradores ativos.

    Colaboradores que se tornarem inativos não recebem novos ciclos, mas o
    histórico já existente em ``saldo_periodo`` é preservado. Não consulta nem
    grava ``periodos_aquisitivos`` ou ``saldos_periodo``.
    """
    reference_date = reference_date or business_today()
    with get_session() as session:
        if wait_for_lock:
            session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _ADVISORY_LOCK_KEY})
        else:
            acquired = bool(session.execute(
                text("SELECT pg_try_advisory_xact_lock(:key)"),
                {"key": _ADVISORY_LOCK_KEY},
            ).scalar())
            if not acquired:
                return {
                    "ok": True,
                    "skipped": True,
                    "reason": "another_worker_processing",
                    "reference_date": reference_date.isoformat(),
                }

        state = session.query(SyncState).filter(SyncState.sync_name == _SYNC_NAME).first()
        last_business_date = _business_date_from_utc(state.last_success_at) if state else None
        if not force and last_business_date and last_business_date >= reference_date:
            return {
                "ok": True,
                "skipped": True,
                "reason": "already_processed",
                "reference_date": reference_date.isoformat(),
            }

        counters = defaultdict(int)
        collaborators = session.query(Colaborador).filter(
            func.upper(func.trim(func.coalesce(Colaborador.status, ""))).in_(("ATIVO", "ACTIVE")),
            Colaborador.data_admissao.isnot(None),
            Colaborador.matricula.isnot(None),
        ).order_by(Colaborador.id.asc()).all()

        for colab in collaborators:
            regular_result = _ensure_regular_cycles(
                session,
                colab,
                regular_cycles(colab.data_admissao, reference_date),
            )
            premium_result = _ensure_premium_cycles(
                session,
                colab,
                premium_cycles(colab.data_admissao, reference_date),
            )
            for key, value in regular_result.items():
                counters[f"regular_{key}"] += value
            for key, value in premium_result.items():
                counters[f"premium_{key}"] += value

        now = dt.datetime.utcnow()
        extra = {
            "reference_date": reference_date.isoformat(),
            "timezone": str(get_settings().app_timezone or "America/Fortaleza"),
            "active_collaborators_with_admission": len(collaborators),
            **dict(counters),
            "regular_created": int(counters["regular_created"]),
            "premium_created": int(counters["premium_created"]),
            "future_rows_removed": int(counters["regular_deleted"] + counters["premium_deleted"]),
            "rule": "saldo_periodo_only; create_for_active_only; preserve_history_after_inactivation; regular_12m_completed; premium_5y_plus_1d_then_30m; no_future_cycles",
            "version": "v61",
        }
        if not state:
            state = SyncState(sync_name=_SYNC_NAME)
            session.add(state)
        state.last_started_at = now
        state.last_finished_at = now
        state.last_success_at = now
        state.last_status = "success"
        state.last_error = None
        state.extra = extra
        state.updated_at = now
        session.add(Auditoria(
            actor_email=actor_email,
            action="DAILY_PERIOD_ACCRUAL_V59",
            entity_type="saldo_periodo",
            entity_id=0,
            before_data=None,
            after_data=extra,
            context={"reference_date": reference_date.isoformat()},
        ))
        return {"ok": True, **extra}


def ensure_daily_periods_current(actor_email: str = "request-period-check") -> dict:
    today = business_today()
    with get_session() as session:
        state = session.query(SyncState).filter(SyncState.sync_name == _SYNC_NAME).first()
        last_business_date = _business_date_from_utc(state.last_success_at) if state else None
        if last_business_date and last_business_date >= today:
            return {"ok": True, "skipped": True, "reason": "already_processed", "reference_date": today.isoformat()}
    return ensure_due_periods(today, actor_email=actor_email, force=False, wait_for_lock=True)


def _daily_worker() -> None:
    global _thread_running
    try:
        today = business_today()
        with get_session() as session:
            state = session.query(SyncState).filter(SyncState.sync_name == _SYNC_NAME).first()
            last_business_date = _business_date_from_utc(state.last_success_at) if state else None
            if last_business_date and last_business_date >= today:
                return
        result = ensure_due_periods(today, force=False, wait_for_lock=False)
        log.info("Verificação diária de períodos V59: %s", result)
    except Exception:
        log.exception("Falha na verificação diária de períodos V59")
    finally:
        with _local_lock:
            _thread_running = False


def trigger_daily_check_async() -> None:
    """Dispara verificação DB-only sem bloquear a requisição web."""
    global _last_local_check, _thread_running
    now = time.monotonic()
    with _local_lock:
        if _thread_running or now - _last_local_check < _LOCAL_CHECK_INTERVAL_SECONDS:
            return
        _last_local_check = now
        _thread_running = True
    threading.Thread(target=_daily_worker, name="period-accrual-v59", daemon=True).start()
