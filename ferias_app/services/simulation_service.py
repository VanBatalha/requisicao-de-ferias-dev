"""Serviço para simulação de gestor no painel admin."""
from __future__ import annotations

from flask import session

from ..logging_config import get_logger

log = get_logger(__name__)


def set_simulated_gestor(gestor_email: str) -> None:
    """Define o email do gestor a ser simulado."""
    if not session:
        return
    session["_simulated_gestor"] = gestor_email.lower().strip() if gestor_email else None
    session.modified = True
    log.info("Simulação iniciada para gestor: %s", gestor_email)


def get_simulated_gestor() -> str | None:
    """Retorna o email do gestor simulado, ou None se não está em simulação."""
    if not session:
        return None
    return session.get("_simulated_gestor")


def clear_simulated_gestor() -> None:
    """Limpa a simulação de gestor."""
    if not session:
        return
    session.pop("_simulated_gestor", None)
    session.modified = True
    log.info("Simulação encerrada")


def is_in_simulation() -> bool:
    """Verifica se está em modo de simulação."""
    return bool(get_simulated_gestor())
