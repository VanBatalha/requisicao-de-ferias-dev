"""Sincronizacao automatica diaria do cadastro Smartsheet.

Executa em background no Web Service, sem bloquear a primeira resposta do usuario.
A regra de negocio usa o fuso APP_TIMEZONE, padrao America/Fortaleza.
"""
from __future__ import annotations

import os
import threading
import time
import datetime as dt
from zoneinfo import ZoneInfo

from ..config import get_settings
from ..logging_config import get_logger
from ..models import SyncState
from .postgres_service import get_session
from .smartsheet_sync_service import sync_cadastro_from_smartsheet

log = get_logger(__name__)

_started = False
_running = False
_last_check_ts = 0.0
_lock = threading.Lock()


def _bool_env(name: str, default: bool) -> bool:
    raw = str(os.getenv(name, "") or "").strip().lower()
    if raw in {"1", "true", "sim", "yes", "y", "on"}:
        return True
    if raw in {"0", "false", "nao", "no", "n", "off"}:
        return False
    return default


def _timezone() -> ZoneInfo:
    tz_name = getattr(get_settings(), "app_timezone", None) or "America/Fortaleza"
    try:
        return ZoneInfo(str(tz_name))
    except Exception:
        return ZoneInfo("America/Fortaleza")


def _last_success_local_date() -> dt.date | None:
    try:
        with get_session() as session:
            row = session.query(SyncState).filter(SyncState.sync_name == "cadastro").first()
            if not row or not row.last_success_at:
                return None
            value = row.last_success_at
            if value.tzinfo is None:
                value = value.replace(tzinfo=dt.timezone.utc)
            return value.astimezone(_timezone()).date()
    except Exception:
        log.exception("AUTO_SYNC: erro consultando sync_state")
        return None


def should_run_now(now_local: dt.datetime | None = None) -> bool:
    now_local = now_local or dt.datetime.now(_timezone())
    last_date = _last_success_local_date()
    today = now_local.date()
    if last_date == today:
        return False
    # Se ja passou de meio-dia, a sincronizacao de hoje esta vencida.
    if now_local.hour >= 12:
        return True
    # Se ontem tambem nao teve sucesso, o primeiro acesso do dia dispara em background.
    if last_date is None or last_date < (today - dt.timedelta(days=1)):
        return True
    return False


def trigger_sync_background(reason: str = "auto") -> bool:
    global _running
    if not _bool_env("AUTO_SYNC_ENABLED", True):
        return False
    with _lock:
        if _running:
            return False
        _running = True

    def worker() -> None:
        global _running
        try:
            log.info("AUTO_SYNC: iniciando sincronizacao em background (%s)", reason)
            sync_cadastro_from_smartsheet(
                triggered_by=f"auto:{reason}",
                actor_email="auto-sync@system",
                recalculate=_bool_env("AUTO_SYNC_RECALCULATE_SALDOS", False),
                include_solicitacoes=_bool_env("AUTO_SYNC_INCLUDE_SOLICITACOES", False),
            )
            log.info("AUTO_SYNC: sincronizacao concluida (%s)", reason)
        except Exception:
            log.exception("AUTO_SYNC: falha na sincronizacao (%s)", reason)
        finally:
            with _lock:
                _running = False

    threading.Thread(target=worker, name="auto-sync-cadastro", daemon=True).start()
    return True


def check_and_trigger_if_due(reason: str = "request", min_interval_seconds: int = 60) -> None:
    global _last_check_ts
    try:
        now_ts = time.monotonic()
        with _lock:
            if now_ts - _last_check_ts < min_interval_seconds:
                return
            _last_check_ts = now_ts
        if should_run_now():
            trigger_sync_background(reason)
    except Exception:
        log.exception("AUTO_SYNC: erro no check de vencimento")


def start_auto_sync_scheduler(app=None) -> None:
    """Inicia thread leve que verifica a sincronizacao diaria.

    Tambem registra before_request para cobrir cenarios em que o Render ficou
    suspenso/reiniciou e perdeu o horario de 12h.
    """
    global _started
    if _started:
        return
    _started = True

    if app is not None:
        @app.before_request
        def _auto_sync_before_request():  # noqa: ANN001
            # Nao bloqueia a requisicao; se estiver vencido, dispara thread.
            check_and_trigger_if_due("first-access")

    def loop() -> None:
        # Pequeno atraso para o app abrir porta primeiro.
        time.sleep(20)
        while True:
            check_and_trigger_if_due("scheduler", min_interval_seconds=0)
            time.sleep(300)

    threading.Thread(target=loop, name="auto-sync-scheduler", daemon=True).start()
