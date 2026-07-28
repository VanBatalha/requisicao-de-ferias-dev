from __future__ import annotations

import calendar
import datetime as dt
import threading
import time
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, text

from ..config import get_settings
from ..logging_config import get_logger
from ..models import Auditoria, Colaborador, PeriodoAquisitivo, SaldoPeriodoNovo, Solicitacao, SyncState
from .postgres_service import get_session

log = get_logger(__name__)

_SYNC_NAME = "ciclos_saldos_v56"
_ADVISORY_LOCK_KEY = 5600728
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

    Regra V55:
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


def _normalize_status(value: object) -> str:
    raw = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(ch for ch in raw if not unicodedata.combining(ch)).strip().upper()


def _premium_usage_from_requests(session, matricula: str, cycle: Cycle) -> tuple[float, float]:
    """Reconstroi utilizado/reservado do ciclo Premium atual.

    A reconstrução é usada somente quando a base ainda possui a estrutura anual
    legada ou quando um novo ciclo Premium acaba de nascer. Ajustes Premium
    antigos ficam no histórico e não são reaplicados automaticamente.
    """
    rows = session.query(Solicitacao).filter(
        func.upper(func.trim(Solicitacao.colaborador_matricula)) == matricula,
        func.upper(func.trim(func.coalesce(Solicitacao.saldo_tipo, Solicitacao.tipo_ferias, "REGULAR"))) == "PREMIUM",
        Solicitacao.data_inicio >= cycle.credito_em,
        Solicitacao.data_inicio < cycle.proximo_credito_em,
    ).all()

    used = 0.0
    reserved = 0.0
    for row in rows:
        tipo = _normalize_status(getattr(row, "tipo_solicitacao", None))
        solicitacao = _normalize_status(getattr(row, "solicitacao", None))
        if bool(getattr(row, "is_ajuste", False)) or tipo == "AJUSTE" or "AJUSTE" in solicitacao:
            continue
        try:
            days = abs(float(row.dias if row.dias is not None else (row.dias_solicitados or 0)))
        except (TypeError, ValueError):
            days = 0.0
        status = _normalize_status(getattr(row, "status", None))
        if status in {"APROVADO", "APROVADA"}:
            used += days
        elif status in {"PENDENTE", "EM ANALISE", "ANALISE", "RESERVA", "RESERVADO", "RESERVADA"}:
            reserved += days
    return used, reserved


def _ensure_regular_balance_cycles(
    session,
    colab: Colaborador,
    cycles: list[Cycle],
) -> dict[str, int]:
    """Mantém saldo REGULAR somente no último período adquirido.

    Todo o valor REGULAR já existente é consolidado no P vigente. Assim, uma
    base antiga com P1..PX preenchidos é normalizada sem perder os ajustes e
    movimentos já aplicados. Quando um novo ciclo anual nasce em uma base já
    normalizada, acrescenta 30 dias ao saldo consolidado anterior.
    """
    mat = str(colab.matricula).strip().upper()
    existing = session.query(SaldoPeriodoNovo).filter(
        func.upper(func.trim(SaldoPeriodoNovo.colaborador_matricula)) == mat,
        func.upper(func.trim(SaldoPeriodoNovo.tipo_saldo)) == "REGULAR",
    ).order_by(SaldoPeriodoNovo.periodo_numero, SaldoPeriodoNovo.id).all()

    total_initial = sum(float(row.saldo_inicial or 0) for row in existing)
    total_used = sum(float(row.saldo_utilizado or 0) for row in existing)
    total_reserved = sum(float(row.saldo_reservado or 0) for row in existing)
    expected_numbers = {cycle.numero for cycle in cycles}
    invalid_rows = [row for row in existing if int(row.periodo_numero or 0) not in expected_numbers]

    deleted = 0
    for row in invalid_rows:
        session.delete(row)
        deleted += 1
    valid_existing = [row for row in existing if row not in invalid_rows]

    if not cycles:
        return {
            "current_created": 0,
            "historical_created": 0,
            "deleted": deleted,
            "zeroed": 0,
            "consolidated": 0,
            "recalculated": 0,
        }

    by_number = {int(row.periodo_numero or 0): row for row in valid_existing}
    current_cycle = cycles[-1]
    current_row = by_number.get(current_cycle.numero)
    current_created = historical_created = zeroed = consolidated = 0

    if current_row is None:
        if existing:
            # Estrutura legada com período futuro: o crédito/ajustes já estavam
            # distribuídos nela, portanto apenas consolidamos sem somar 30.
            if invalid_rows:
                new_initial = total_initial
            else:
                # Virada normal de um ciclo em base já corrigida.
                new_initial = total_initial + current_cycle.base
            new_used = total_used
            new_reserved = total_reserved
        else:
            # Cadastro antigo sem qualquer linha: reconhece todos os ciclos já
            # adquiridos na primeira normalização.
            new_initial = current_cycle.base * len(cycles)
            new_used = 0.0
            new_reserved = 0.0
        current_row = _create_balance_row(
            session,
            colab,
            current_cycle,
            initial=new_initial,
            used=new_used,
            reserved=new_reserved,
            available=new_initial - new_used - new_reserved,
            is_current=True,
        )
        by_number[current_cycle.numero] = current_row
        current_created += 1
        consolidated += 1
    else:
        # Se há qualquer valor histórico, concentra tudo no P vigente.
        current_row.saldo_inicial = total_initial
        current_row.saldo_utilizado = total_used
        current_row.saldo_reservado = total_reserved
        current_row.saldo_disponivel = total_initial - total_used - total_reserved
        current_row.ultima_alteracao = dt.datetime.utcnow()
        current_row.updated_at = dt.datetime.utcnow()
        current_row.is_atual = True
        consolidated += 1

    for cycle in cycles:
        row = by_number.get(cycle.numero)
        is_current = cycle.numero == current_cycle.numero
        if row is None:
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
            by_number[cycle.numero] = row
            historical_created += 1
        row.colaborador_id = colab.id
        row.colaborador_matricula = mat
        row.tipo_saldo = "REGULAR"
        row.data_inicio = cycle.data_inicio
        row.data_fim = cycle.data_fim
        if is_current:
            row.is_atual = True
        elif _zero_historical(row):
            zeroed += 1

    return {
        "current_created": current_created,
        "historical_created": historical_created,
        "deleted": deleted,
        "zeroed": zeroed,
        "consolidated": consolidated,
        "recalculated": 0,
    }


def _ensure_premium_balance_cycles(
    session,
    colab: Colaborador,
    cycles: list[Cycle],
) -> dict[str, int]:
    """Aplica os ciclos Premium 5 anos + 30 meses.

    Linhas anuais Premium são excluídas. Ao detectar a estrutura legada, o
    ciclo vigente é reconstruído com a base fixa da regra e somente com
    solicitações não-ajuste pertencentes à janela atual.
    """
    mat = str(colab.matricula).strip().upper()
    existing = session.query(SaldoPeriodoNovo).filter(
        func.upper(func.trim(SaldoPeriodoNovo.colaborador_matricula)) == mat,
        func.upper(func.trim(SaldoPeriodoNovo.tipo_saldo)) == "PREMIUM",
    ).order_by(SaldoPeriodoNovo.periodo_numero, SaldoPeriodoNovo.id).all()

    expected_numbers = {cycle.numero for cycle in cycles}
    invalid_rows = [row for row in existing if int(row.periodo_numero or 0) not in expected_numbers]
    legacy_structure = bool(invalid_rows)
    deleted = 0
    for row in invalid_rows:
        session.delete(row)
        deleted += 1
    valid_existing = [row for row in existing if row not in invalid_rows]

    if not cycles:
        return {
            "current_created": 0,
            "historical_created": 0,
            "deleted": deleted,
            "zeroed": 0,
            "consolidated": 0,
            "recalculated": 0,
        }

    by_number = {int(row.periodo_numero or 0): row for row in valid_existing}
    current_cycle = cycles[-1]
    current_row = by_number.get(current_cycle.numero)
    current_created = historical_created = zeroed = recalculated = 0

    if current_row is None or legacy_structure:
        used, reserved = _premium_usage_from_requests(session, mat, current_cycle)
        initial = current_cycle.base
        if current_row is None:
            current_row = _create_balance_row(
                session,
                colab,
                current_cycle,
                initial=initial,
                used=used,
                reserved=reserved,
                available=initial - used - reserved,
                is_current=True,
            )
            by_number[current_cycle.numero] = current_row
            current_created += 1
        else:
            current_row.saldo_inicial = initial
            current_row.saldo_utilizado = used
            current_row.saldo_reservado = reserved
            current_row.saldo_disponivel = initial - used - reserved
            current_row.ultima_alteracao = dt.datetime.utcnow()
            current_row.updated_at = dt.datetime.utcnow()
            current_row.is_atual = True
        recalculated += 1

    for cycle in cycles:
        row = by_number.get(cycle.numero)
        is_current = cycle.numero == current_cycle.numero
        if row is None:
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
            by_number[cycle.numero] = row
            historical_created += 1
        row.colaborador_id = colab.id
        row.colaborador_matricula = mat
        row.tipo_saldo = "PREMIUM"
        row.data_inicio = cycle.data_inicio
        row.data_fim = cycle.data_fim
        if is_current:
            row.is_atual = True
        elif _zero_historical(row):
            zeroed += 1

    return {
        "current_created": current_created,
        "historical_created": historical_created,
        "deleted": deleted,
        "zeroed": zeroed,
        "consolidated": 0,
        "recalculated": recalculated,
    }

def ensure_due_periods(
    reference_date: dt.date | None = None,
    actor_email: str = "daily-period-service",
    *,
    force: bool = False,
    wait_for_lock: bool = True,
) -> dict:
    """Cria somente ciclos adquiridos e normaliza a linha vigente.

    - REGULAR: cria 30 dias apenas no fechamento anual, consolida todo o saldo
      no último P adquirido e mantém as linhas históricas zeradas.
    - PREMIUM: P1=30 dias após cinco anos; P2+=15 dias a cada 30 meses; o saldo
      anterior expira quando nasce um novo ciclo.
    - Ciclos vigentes já corrigidos preservam edições e movimentações feitas
      pelo aplicativo.
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

            reg_result = _ensure_regular_balance_cycles(session, colab, reg_cycles)
            prem_result = _ensure_premium_balance_cycles(
                session,
                colab,
                premium_cycles(colab.data_admissao, reference_date),
            )
            counters["regular_current_created"] += reg_result["current_created"]
            counters["premium_current_created"] += prem_result["current_created"]
            counters["historical_balance_rows_created"] += reg_result["historical_created"] + prem_result["historical_created"]
            counters["invalid_balance_rows_removed"] += reg_result["deleted"] + prem_result["deleted"]
            counters["historical_balance_rows_zeroed"] += reg_result["zeroed"] + prem_result["zeroed"]
            counters["regular_rows_consolidated"] += reg_result["consolidated"]
            counters["premium_rows_recalculated"] += prem_result["recalculated"]

        now = dt.datetime.utcnow()
        extra = {
            "reference_date": reference_date.isoformat(),
            "timezone": str(get_settings().app_timezone or "America/Fortaleza"),
            "active_collaborators": len(collaborators),
            **dict(counters),
            # Compatibilidade com os campos exibidos na tela ADMIN.
            "regular_created": int(counters["regular_current_created"]),
            "premium_created": int(counters["premium_current_created"]),
            "future_rows_removed": int(counters["future_period_rows_removed"] + counters["invalid_balance_rows_removed"]),
            "rule": "regular_12m_consolidated_in_current_period; premium_5y_30d_then_30m_15d_non_cumulative",
            "version": "v56",
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
            action="DAILY_PERIOD_ACCRUAL_V56",
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
        log.info("Verificacao diaria de periodos V56: %s", result)
    except Exception:
        log.exception("Falha na verificacao diaria de periodos V56")
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
    threading.Thread(target=_daily_worker, name="period-accrual-v56", daemon=True).start()
