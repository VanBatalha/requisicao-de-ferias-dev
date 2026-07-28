from __future__ import annotations

import calendar
import datetime as dt
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, text

from ..config import get_settings
from ..logging_config import get_logger
from ..models import Auditoria, Colaborador, PeriodoAquisitivo, SaldoPeriodoNovo, SyncState
from .postgres_service import get_session

log = get_logger(__name__)

_SYNC_NAME = "ciclos_saldos_v54"
_ADVISORY_LOCK_KEY = 5400728
_LOCAL_CHECK_INTERVAL_SECONDS = 1800
_local_lock = threading.Lock()
_last_local_check = 0.0
_thread_running = False


@dataclass(frozen=True)
class Cycle:
    tipo: str
    numero: int
    # Faixa que originou o direito. O credito passa a existir em credito_em.
    data_inicio: dt.date
    data_fim: dt.date
    credito_em: dt.date
    proximo_credito_em: dt.date
    base: float


def business_today() -> dt.date:
    """Data corrente no fuso de negocio, evitando creditos antecipados por UTC."""
    timezone_name = str(get_settings().app_timezone or "America/Fortaleza").strip()
    try:
        timezone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("APP_TIMEZONE invalido (%s); usando America/Fortaleza", timezone_name)
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
    """Periodos regulares efetivamente adquiridos.

    Exemplo MAT00116: a faixa 11/02/2026 a 10/02/2027 somente gera P8 em
    11/02/2027. O periodo em formacao nunca e criado em saldo_periodo.
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
    """Ciclos independentes da Licenca Certariana/Premium.

    Regra V54:
    - P1: 30 dias no dia seguinte ao fechamento de cinco anos;
    - P2 em diante: 15 dias a cada 30 meses;
    - o saldo disponivel anterior expira quando nasce o novo ciclo.

    As datas inicio/fim representam a faixa que originou cada credito. Para
    MAT00116, P1 cobre 11/02/2019 a 11/02/2024 e P2 cobre 12/02/2024 a
    11/08/2026, ficando disponivel em 12/08/2026.
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
    """Indica se um evento pertence ao ciclo Premium vigente hoje.

    Eventos de ciclos já expirados permanecem no histórico, mas não podem
    estornar ou creditar o saldo Premium atual.
    """
    if isinstance(event_date, dt.datetime):
        event_date = event_date.date()
    reference_date = reference_date or business_today()
    cycles = premium_cycles(admissao, reference_date)
    if not cycles or not event_date:
        return False
    current = cycles[-1]
    return current.credito_em <= event_date < current.proximo_credito_em


def _upsert_periodo_regular(session, colab: Colaborador, cycle: Cycle, is_current: bool) -> tuple[PeriodoAquisitivo, bool]:
    row = session.query(PeriodoAquisitivo).filter(
        func.upper(PeriodoAquisitivo.colaborador_matricula) == str(colab.matricula).strip().upper(),
        PeriodoAquisitivo.periodo_numero == cycle.numero,
    ).first()
    created = row is None
    if not row:
        row = PeriodoAquisitivo(
            colaborador_id=colab.id,
            colaborador_matricula=str(colab.matricula).strip().upper(),
            periodo_numero=cycle.numero,
            data_inicio=cycle.data_inicio,
            data_fim=cycle.data_fim,
            is_atual=is_current,
        )
        session.add(row)
    else:
        row.colaborador_id = colab.id
        row.colaborador_matricula = str(colab.matricula).strip().upper()
        row.data_inicio = cycle.data_inicio
        row.data_fim = cycle.data_fim
        row.is_atual = is_current
    return row, created


def _create_balance_row(
    session,
    colab: Colaborador,
    cycle: Cycle,
    *,
    initial: float,
    used: float,
    reserved: float,
    available: float,
    is_current: bool,
) -> SaldoPeriodoNovo:
    now = dt.datetime.utcnow()
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
    changed = any(float(value or 0) != 0 for value in (
        row.saldo_inicial,
        row.saldo_utilizado,
        row.saldo_reservado,
        row.saldo_disponivel,
    )) or bool(row.is_atual)
    if not changed:
        return False
    now = dt.datetime.utcnow()
    row.is_atual = False
    row.saldo_inicial = 0
    row.saldo_utilizado = 0
    row.saldo_reservado = 0
    row.saldo_disponivel = 0
    row.ultima_alteracao = now
    row.updated_at = now
    return True


def _ensure_balance_cycles(
    session,
    colab: Colaborador,
    cycles: list[Cycle],
    tipo: str,
) -> dict[str, int]:
    """Normaliza as linhas P1..PX e preserva somente o período vigente.

    REGULAR é cumulativo: quando nasce um novo P, todo o estado consolidado do
    período vigente anterior é transferido e são acrescentados 30 dias por
    ciclo anual ainda não registrado. Isso preserva utilizado/reservado e evita
    descontar novamente solicitações futuras já lançadas.

    PREMIUM não é cumulativo: ao nascer um novo ciclo, o saldo anterior expira
    e o novo P começa somente com a base da regra (30 dias no P1; 15 nos demais).
    """
    mat = str(colab.matricula).strip().upper()
    existing = session.query(SaldoPeriodoNovo).filter(
        func.upper(SaldoPeriodoNovo.colaborador_matricula) == mat,
        func.upper(SaldoPeriodoNovo.tipo_saldo) == tipo,
    ).all()

    expected_numbers = {c.numero for c in cycles}
    deleted = 0
    for row in list(existing):
        if int(row.periodo_numero or 0) not in expected_numbers:
            session.delete(row)
            existing.remove(row)
            deleted += 1

    if not cycles:
        return {"current_created": 0, "historical_created": 0, "deleted": deleted, "zeroed": 0, "consolidated": 0}

    by_number = {int(r.periodo_numero or 0): r for r in existing}
    current_cycle = cycles[-1]
    current_row = by_number.get(current_cycle.numero)
    existing_numbers = sorted(n for n in by_number if n in expected_numbers)
    max_existing_number = max(existing_numbers, default=0)

    current_created = historical_created = zeroed = consolidated = 0

    # Calcula o estado do novo período antes de zerar as linhas históricas.
    new_current_values: tuple[float, float, float, float] | None = None
    if current_row is None:
        if tipo == "REGULAR":
            total_initial = sum(float(r.saldo_inicial or 0) for r in existing)
            total_used = sum(float(r.saldo_utilizado or 0) for r in existing)
            total_reserved = sum(float(r.saldo_reservado or 0) for r in existing)
            missing_cycles = max(current_cycle.numero - max_existing_number, 1)
            initial = total_initial + (30.0 * missing_cycles)
            used = total_used
            reserved = total_reserved
            available = initial - used - reserved
            new_current_values = (initial, used, reserved, available)
        else:
            new_current_values = (current_cycle.base, 0.0, 0.0, current_cycle.base)
    elif tipo == "REGULAR":
        # Compatibilidade com bancos ainda no formato antigo: se houver valores
        # espalhados em P históricos, consolida tudo no P vigente antes de zerar.
        historical_nonzero = any(
            int(r.periodo_numero or 0) != current_cycle.numero
            and any(float(v or 0) != 0 for v in (
                r.saldo_inicial, r.saldo_utilizado, r.saldo_reservado, r.saldo_disponivel
            ))
            for r in existing
        )
        if historical_nonzero:
            initial = sum(float(r.saldo_inicial or 0) for r in existing)
            used = sum(float(r.saldo_utilizado or 0) for r in existing)
            reserved = sum(float(r.saldo_reservado or 0) for r in existing)
            current_row.saldo_inicial = initial
            current_row.saldo_utilizado = used
            current_row.saldo_reservado = reserved
            current_row.saldo_disponivel = initial - used - reserved
            current_row.ultima_alteracao = dt.datetime.utcnow()
            current_row.updated_at = dt.datetime.utcnow()
            consolidated += 1

    for cycle in cycles:
        row = by_number.get(cycle.numero)
        is_current = cycle.numero == current_cycle.numero
        if row is None:
            if is_current:
                initial, used, reserved, available = new_current_values or (cycle.base, 0.0, 0.0, cycle.base)
                row = _create_balance_row(
                    session,
                    colab,
                    cycle,
                    initial=initial,
                    used=used,
                    reserved=reserved,
                    available=available,
                    is_current=True,
                )
                current_created += 1
            else:
                row = _create_balance_row(
                    session,
                    colab,
                    cycle,
                    initial=0,
                    used=0,
                    reserved=0,
                    available=0,
                    is_current=False,
                )
                historical_created += 1
            by_number[cycle.numero] = row
        else:
            row.colaborador_id = colab.id
            row.colaborador_matricula = mat
            row.tipo_saldo = tipo
            row.data_inicio = cycle.data_inicio
            row.data_fim = cycle.data_fim
            if is_current:
                row.is_atual = True
            elif _zero_historical(row):
                zeroed += 1

    # Garante uma única linha vigente e zera integralmente todo o histórico.
    for numero, row in by_number.items():
        if numero == current_cycle.numero:
            row.is_atual = True
        elif _zero_historical(row):
            zeroed += 1

    return {
        "current_created": current_created,
        "historical_created": historical_created,
        "deleted": deleted,
        "zeroed": zeroed,
        "consolidated": consolidated,
    }


def ensure_due_periods(
    reference_date: dt.date | None = None,
    actor_email: str = "daily-period-service",
    *,
    force: bool = False,
    wait_for_lock: bool = True,
) -> dict:
    """Cria somente ciclos adquiridos e normaliza a linha vigente.

    - REGULAR: cria 30 dias apenas no fechamento anual e transfere o estado
      consolidado (inicial, utilizado e reservado) para o novo P; todas as
      linhas antigas ficam zeradas.
    - PREMIUM: P1=30 dias apos cinco anos; P2+=15 dias a cada 30 meses; o saldo
      anterior nao e carregado.
    - Ciclo vigente ja existente nao e recalculado, preservando edicoes e
      movimentacoes feitas pelo aplicativo.
    """
    reference_date = reference_date or business_today()
    with get_session() as session:
        # Requisições que movimentam saldo aguardam a trava. A verificação
        # assíncrona do Web Service usa try-lock para não enfileirar vários
        # workers do Gunicorn na primeira requisição do dia.
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

        # Reconfere o estado depois de obter a trava. Assim, workers que
        # aguardaram o primeiro processamento não repetem toda a normalização.
        state = session.query(SyncState).filter(SyncState.sync_name == _SYNC_NAME).first()
        last_business_date = _business_date_from_utc(state.last_success_at) if state else None
        if not force and last_business_date and last_business_date >= reference_date:
            return {
                "ok": True,
                "skipped": True,
                "reason": "already_processed",
                "reference_date": reference_date.isoformat(),
            }

        collaborators = session.query(Colaborador).filter(
            func.upper(func.coalesce(Colaborador.status, "ATIVO")).in_(["ATIVO", "ACTIVE"]),
            Colaborador.data_admissao.isnot(None),
            Colaborador.matricula.isnot(None),
        ).order_by(Colaborador.id).all()
        counters = defaultdict(int)
        for colab in collaborators:
            mat = str(colab.matricula).strip().upper()

            reg_cycles = regular_cycles(colab.data_admissao, reference_date)
            reg_numbers = {c.numero for c in reg_cycles}
            reg_current_number = reg_cycles[-1].numero if reg_cycles else None
            for cycle in reg_cycles:
                _, created = _upsert_periodo_regular(
                    session,
                    colab,
                    cycle,
                    cycle.numero == reg_current_number,
                )
                counters["regular_period_rows_created"] += int(created)
            future_periods = session.query(PeriodoAquisitivo).filter(
                func.upper(PeriodoAquisitivo.colaborador_matricula) == mat,
                ~PeriodoAquisitivo.periodo_numero.in_(reg_numbers or {-1}),
            ).all()
            for row in future_periods:
                session.delete(row)
                counters["future_period_rows_removed"] += 1

            reg_result = _ensure_balance_cycles(session, colab, reg_cycles, "REGULAR")
            prem_result = _ensure_balance_cycles(
                session,
                colab,
                premium_cycles(colab.data_admissao, reference_date),
                "PREMIUM",
            )
            counters["regular_current_created"] += reg_result["current_created"]
            counters["premium_current_created"] += prem_result["current_created"]
            counters["historical_balance_rows_created"] += reg_result["historical_created"] + prem_result["historical_created"]
            counters["invalid_balance_rows_removed"] += reg_result["deleted"] + prem_result["deleted"]
            counters["historical_balance_rows_zeroed"] += reg_result["zeroed"] + prem_result["zeroed"]
            counters["regular_rows_consolidated"] += reg_result["consolidated"]

        now = dt.datetime.utcnow()
        extra = {
            "reference_date": reference_date.isoformat(),
            "timezone": str(get_settings().app_timezone or "America/Fortaleza"),
            "active_collaborators": len(collaborators),
            **dict(counters),
            # Compatibilidade com o texto da tela ADMIN da V54.
            "regular_created": int(counters["regular_current_created"]),
            "premium_created": int(counters["premium_current_created"]),
            "future_rows_removed": int(counters["future_period_rows_removed"] + counters["invalid_balance_rows_removed"]),
            "rule": "regular_12m_after_close_with_carry; premium_5y_30d_then_30m_15d_expiring",
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
            action="DAILY_PERIOD_ACCRUAL_V54",
            entity_type="saldo_periodo",
            entity_id=0,
            before_data=None,
            after_data=extra,
            context={"reference_date": reference_date.isoformat()},
        ))
        return {"ok": True, **extra}


def ensure_daily_periods_current(actor_email: str = "request-period-check") -> dict:
    """Garante que a regra do dia foi aplicada antes de movimentar saldo."""
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
        log.info("Verificacao diaria de periodos V54: %s", result)
    except Exception:
        log.exception("Falha na verificacao diaria de periodos V54")
    finally:
        with _local_lock:
            _thread_running = False


def trigger_daily_check_async() -> None:
    """Dispara uma verificacao DB-only sem bloquear a requisicao web."""
    global _last_local_check, _thread_running
    now = time.monotonic()
    with _local_lock:
        if _thread_running or now - _last_local_check < _LOCAL_CHECK_INTERVAL_SECONDS:
            return
        _last_local_check = now
        _thread_running = True
    threading.Thread(target=_daily_worker, name="period-accrual-v54", daemon=True).start()
