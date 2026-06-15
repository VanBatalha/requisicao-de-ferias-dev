# ferias_app/services/scheduler_service.py
"""Serviço de agendamento automático para sincronização diária com Smartsheet."""
from __future__ import annotations

import atexit
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

from ..logging_config import get_logger

log = get_logger(__name__)

_scheduler: Optional[BackgroundScheduler] = None


def start_scheduler():
    """Inicia o agendador de background para sincronização diária.
    
    Agenda a sincronização do Smartsheet para todos os dias às 12:00 
    no horário de Fortaleza (UTC-3).
    """
    global _scheduler
    
    if _scheduler is not None and _scheduler.running:
        log.warning("Scheduler já está em execução.")
        return
    
    # Fuso horário de Fortaleza
    tz_fortaleza = pytz.timezone('America/Fortaleza')
    
    # Cria o scheduler
    _scheduler = BackgroundScheduler()
    
    # Configura o job: todos os dias às 12:00
    trigger = CronTrigger(hour=12, minute=0, timezone=tz_fortaleza)
    
    def job_sync_cadastro():
        """Job que executa a sincronização de cadastro."""
        try:
            log.info("🕒 Iniciando sincronização automática de cadastro (12:00 Fortaleza)...")
            
            # Importa aqui para evitar circular dependency
            from .sync_service import sincronizar_cadastro_smartsheet
            
            result = sincronizar_cadastro_smartsheet()
            
            log.info("✅ Sincronização automática concluída: %s", result)
            
        except Exception as e:
            log.exception("❌ Erro na sincronização automática: %s", e)
    
    # Adiciona o job ao scheduler
    _scheduler.add_job(
        func=job_sync_cadastro,
        trigger=trigger,
        id='sync_diario_cadastro',
        name='Sincronização Diária de Cadastro Smartsheet',
        replace_existing=True
    )
    
    # Inicia o scheduler
    _scheduler.start()
    
    log.info("🕒 Scheduler iniciado: Sincronização diária configurada para 12:00 (Horário de Fortaleza).")
    
    # Registra shutdown gracioso
    atexit.register(lambda: stop_scheduler())


def stop_scheduler():
    """Para o scheduler graciosamente."""
    global _scheduler
    
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
        log.info("Scheduler parado.")
    
    _scheduler = None


def get_scheduler_status() -> dict:
    """Retorna o status atual do scheduler.
    
    Returns:
        dict: Status do scheduler com informações dos jobs agendados
    """
    if _scheduler is None:
        return {
            "running": False,
            "jobs": []
        }
    
    jobs = []
    for job in _scheduler.get_jobs():
        jobs.append({
            "id": job.id,
            "name": job.name,
            "next_run_time": job.next_run_time.isoformat() if job.next_run_time else None,
            "trigger": str(job.trigger)
        })
    
    return {
        "running": _scheduler.running,
        "jobs": jobs
    }