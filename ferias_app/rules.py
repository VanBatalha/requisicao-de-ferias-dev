"""Regras de negócio centralizadas.

Objetivo: manter TODAS as validações que bloqueiam uma ação em um único lugar,
para evitar espalhar lógica por blueprints/serviços.

Regra atual implementada aqui:
- Licença Certariana (PREMIUM): até 3 períodos, mínimo 10 dias por período;
  se 3 períodos, deve ser 10/10/10 (total 30);
  não pode sobrar saldo entre 1 e 9 (ou zera).
"""

from __future__ import annotations

import datetime as dt
from typing import Iterable, Optional, Set


class RuleError(ValueError):
    """Erro de validação de regra (deve virar 400 no endpoint)."""


def validate_licenca_certariana(
    email: str,
    dias_novos: float,
    *,
    dt_inicio: Optional[dt.date] = None,
    exclude_row_id: Optional[int] = None,
    include_statuses: Optional[Set[str]] = None,
) -> None:
    """Valida fracionamento da Licença Certariana.

    Levanta RuleError quando a regra for violada.
    """
    # imports locais para evitar ciclos
    from .legacy.core_legacy import (
        _colaborador_admissao,
        _janela_licenca_certariana,
        _listar_segmentos_premium,
    )

    try:
        dias = float(dias_novos)
    except Exception:
        raise RuleError("Dias inválidos.")

    if dias < 10:
        raise RuleError("Na Licença Certariana, cada período deve ter no mínimo 10 dias.")

    adm = _colaborador_admissao(email)
    if not adm:
        # sem admissão: aplica regra apenas pela matemática (mais seguro que liberar geral)
        win_start, win_end = dt.date.min, dt.date.max
    else:
        # _janela_licenca_certariana retorna (dias_base, win_start, win_end)
        _, win_start, win_end = _janela_licenca_certariana(adm, hoje=dt_inicio or None)

    existentes: list[int] = _listar_segmentos_premium(
        email,
        win_start,
        win_end,
        exclude_row_id=exclude_row_id,
        include_statuses=include_statuses,
    )

    # normaliza existentes
    segs = []
    for x in existentes or []:
        try:
            segs.append(int(round(float(x))))
        except Exception:
            segs.append(0)

    total = sum(segs) + int(round(dias))
    if total > 30:
        raise RuleError(f"Licença Certariana excede 30 dias na janela atual (tentativa: {total} dias).")

    periodos = len(segs) + 1
    if periodos > 3:
        raise RuleError("Licença Certariana permite no máximo 3 períodos na janela atual.")

    restante = 30 - total
    if restante != 0 and restante < 10:
        raise RuleError(
            "O saldo restante da Licença Certariana não pode ficar menor que 10 dias (ou deve zerar)."
        )

    if periodos == 3:
        todos = segs + [int(round(dias))]
        if total != 30 or any(v != 10 for v in todos):
            raise RuleError(
                "Quando houver 3 períodos na Licença Certariana, deve ser obrigatoriamente 3×10 (total 30)."
            )
