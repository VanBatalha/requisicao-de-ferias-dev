"""Regras de negócio centralizadas.

Objetivo: manter TODAS as validações que bloqueiam uma ação em um único lugar,
para evitar espalhar lógica por blueprints/serviços.

Regra atual implementada aqui:
- Licença Certariana (PREMIUM): até 3 períodos, mínimo 10 dias por período;
  se 3 períodos, deve ser 10/10/10 (total 30);
  se 2 períodos, ambos devem ter >= 10 dias, e o saldo restante deve ser 0 ou >= 10;
  não pode sobrar saldo entre 1 e 9.
"""

from __future__ import annotations
import datetime as dt
import logging

logger = logging.getLogger(__name__)

from typing import Iterable, Optional, Set


class RuleError(ValueError):
    """Erro de validação de regra (deve virar 400 no endpoint)."""


def validate_licenca_certariana(
    email: str,
    dias_novos: float,
    *,
    dt_inicio: Optional[dt.date] = None,
    dt_fim: Optional[dt.date] = None,
    exclude_row_id: Optional[int] = None,
    include_statuses: Optional[Set[str]] = None,
) -> None:
    """Valida fracionamento da Licença Certariana.
    
    Regras:
    - 1 período: >= 10 dias, restante deve ser 0 ou >= 10
    - 2 períodos: ambos >= 10 dias, restante deve ser 0 ou >= 10
    - 3 períodos: obrigatoriamente 10+10+10 (total 30)
    
    Levanta RuleError quando a regra for violada.
    """
    # imports locais para evitar ciclos
    from .legacy.core_legacy import (
        _colaborador_admissao,
        _janela_licenca_certariana,
        _listar_periodos_premium,
    )

    try:
        dias = float(dias_novos)
    except Exception:
        raise RuleError("Dias inválidos.")

    # Validação 1: Mínimo 10 dias por período
    if dias < 10:
        raise RuleError("Na Licença Certariana, cada período deve ter no mínimo 10 dias.")

    adm = _colaborador_admissao(email)
    if not adm:
        # sem admissão: aplica regra apenas pela matemática (mais seguro que liberar geral)
        direito_total = 30
        win_start, win_end = dt.date.min, dt.date.max
    else:
        # _janela_licenca_certariana retorna (dias_base, win_start, win_end)
        dias_base, win_start, win_end = _janela_licenca_certariana(adm, hoje=dt_inicio or None)
        try:
            direito_total = int(dias_base or 0)
        except Exception:
            direito_total = 0

        if direito_total <= 0:
            raise RuleError("Licença Certariana indisponível (direito total = 0).")

    existentes = _listar_periodos_premium(
        email,
        win_start,
        win_end,
        exclude_row_id=exclude_row_id,
        include_statuses=include_statuses,
        force_refresh=True,
    )

    # Log explícito para auditoria (Render)
    try:
        ex_list = [
            f"{p.get('ini')}→{p.get('fim')} dias={p.get('dias')} status={p.get('status')} sol={p.get('solicitacao')} row={p.get('row_id')}"
            for p in (existentes or [])
        ]
        logger.warning(
            "[CERTARIANA] colab=%s direito_total=%s win_start=%s win_end=%s novo=%s(%s→%s) existentes=%s",
            email,
            direito_total,
            win_start,
            win_end,
            dias,
            dt_inicio,
            dt_fim,
            ex_list,
        )
        print(
            f"[CERTARIANA] colab={email} direito_total={direito_total} win_start={win_start} win_end={win_end} novo={dias}({dt_inicio}→{dt_fim}) existentes={ex_list}"
        )
    except Exception:
        # nunca deixar log quebrar a validação
        pass

    # normaliza existentes (e valida overlap quando possível)
    segs: list[int] = []
    if dt_inicio:
        ini_novo = dt_inicio
    else:
        # sem dt_inicio: não dá para validar sobreposição; aplica apenas matemática
        ini_novo = None

    fim_novo = dt_fim
    if ini_novo and not fim_novo:
        fim_novo = ini_novo + dt.timedelta(days=int(round(dias)) - 1)

    for seg in existentes or []:
        try:
            di = int(round(float(seg.get("dias") or 0)))
        except Exception:
            di = 0
        segs.append(di)

        # Overlap (se tivermos datas do novo e do existente)
        if ini_novo and fim_novo:
            ini_e = seg.get("ini")
            fim_e = seg.get("fim")
            if ini_e and fim_e:
                if not (fim_novo < ini_e or ini_novo > fim_e):
                    raise RuleError(
                        "Este período conflita (sobrepõe) com outro período de Licença Certariana já registrado."
                    )

    total = sum(segs) + int(round(dias))
    
    # Validação 2: Não pode exceder direito total
    if total > direito_total:
        raise RuleError(
            f"Licença Certariana excede o direito total ({direito_total} dias) na janela atual (tentativa: {total} dias)."
        )

    periodos = len(segs) + 1
    
    # Validação 3: Máximo 3 períodos
    if periodos > 3:
        raise RuleError("Licença Certariana permite no máximo 3 períodos na janela atual.")

    restante = direito_total - total
    
    # Validação 4: Saldo restante deve ser 0 ou >= 10 (nunca entre 1 e 9)
    if restante != 0 and restante < 10:
        raise RuleError(
            "O saldo restante da Licença Certariana não pode ficar menor que 10 dias (ou deve zerar)."
        )

    # Validação 5: Se 2 períodos, ambos precisam ter >= 10 dias.
    #
    # Observação importante:
    # deixar saldo 10 É permitido, pois isso viabiliza um 3º período de 10 dias,
    # exatamente como a regra do negócio descreve (3×10). O que não pode é sobrar
    # saldo entre 1 e 9 dias, já tratado acima.
    if periodos == 2:
        for seg_dias in segs:
            if seg_dias < 10:
                raise RuleError(
                    "Para fracionar em 2 períodos, ambos devem ter no mínimo 10 dias. "
                    "Verifique o período anterior."
                )

    # Validação 6: Se 3 períodos, deve ser obrigatoriamente 10+10+10
    if periodos == 3:
        todos = segs + [int(round(dias))]
        if not (direito_total == 30 and total == 30 and all(v == 10 for v in todos)):
            raise RuleError(
                "Quando houver 3 períodos na Licença Certariana, deve ser obrigatoriamente 3×10 (total 30)."
            )
