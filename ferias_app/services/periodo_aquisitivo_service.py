from __future__ import annotations

import datetime as dt

from ..services.core_support import (
    _add_months,
    _colaborador_admissao,
)


def completed_aquisitive_periods(admissao: dt.date | None, hoje: dt.date | None = None) -> int:
    if not admissao:
        return 0
    hoje = hoje or dt.date.today()
    if hoje < admissao:
        return 0
    count = 0
    while _add_months(admissao, (count + 1) * 12) <= hoje:
        count += 1
    return count



def periodo_bounds(admissao: dt.date, numero_periodo: int) -> tuple[dt.date, dt.date]:
    ini = _add_months(admissao, (numero_periodo - 1) * 12)
    fim_exclusivo = _add_months(admissao, numero_periodo * 12)
    return ini, fim_exclusivo - dt.timedelta(days=1)



def current_partial_period(admissao: dt.date | None, hoje: dt.date | None = None):
    """Retorna o ultimo periodo adquirido, nunca o ciclo ainda em formacao."""
    if not admissao:
        return None
    hoje = hoje or dt.date.today()
    completos = completed_aquisitive_periods(admissao, hoje)
    if completos <= 0:
        return None
    ini = _add_months(admissao, (completos - 1) * 12)
    fim = _add_months(admissao, completos * 12) - dt.timedelta(days=1)
    return {
        "numero": completos,
        "inicio": ini,
        "fim": fim,
        "completo": True,
    }



def allocate_period_balance(
    total_direito: int,
    total_usados: int,
    total_reservados: int,
    admissao: dt.date | None,
    hoje: dt.date | None = None,
    ultimo_periodo_usado: int | None = None,
):
    """Monta o saldo regular disponível por período aquisitivo.

    Regra de transição:
    - quando existe data de admissão, usa os períodos aquisitivos completos reais;
    - quando a admissão não está no cadastro, ainda assim não bloqueia a
      solicitação: infere a quantidade de períodos pelo direito/saldo atual;
    - o saldo é preenchido dos períodos mais recentes para trás, deixando o
      período mais antigo com eventual saldo parcial;
    - a distribuição da solicitação continua FIFO: consome primeiro o período
      mais antigo entre os que ainda têm saldo.
    """
    hoje = hoje or dt.date.today()
    total_direito = max(0, int(total_direito or 0))
    total_usados = max(0, int(total_usados or 0))
    total_reservados = max(0, int(total_reservados or 0))
    saldo_disponivel = max(0, total_direito - total_usados - total_reservados)
    if saldo_disponivel <= 0:
        return []

    qtd_periodos = max(1, (saldo_disponivel + 29) // 30)

    completos = completed_aquisitive_periods(admissao, hoje) if admissao else 0
    if completos > 0:
        ultimo_num = completos
    else:
        # Fallback para colaboradores sem admissão mapeada no cadastro.
        # Ex.: direito/saldo 19 dias -> P1:19, evitando erro falso de saldo.
        ultimo_num = max(1, (max(total_direito, saldo_disponivel) + 29) // 30)

    try:
        ultimo_usado = int(ultimo_periodo_usado or 0)
    except Exception:
        ultimo_usado = 0
    if ultimo_usado > 0 and ultimo_num <= ultimo_usado:
        # Quando o histórico já sinaliza o último período usado, avança para o
        # próximo período, preservando o consumo do mais antigo para o mais novo.
        ultimo_num = ultimo_usado + 1

    primeiro_num = max(1, ultimo_num - qtd_periodos + 1)
    numeros = list(range(primeiro_num, ultimo_num + 1))
    saldos_map = {n: 0 for n in numeros}
    restante = saldo_disponivel

    # Preenche os períodos mais recentes primeiro. Assim, se o saldo for 45 e o
    # último período for P7, o detalhamento disponível fica P6=15 e P7=30; uma
    # solicitação de 21 dias consome P6:15 | P7:6.
    for n in reversed(numeros):
        if restante <= 0:
            break
        alocar = min(30, restante)
        saldos_map[n] = alocar
        restante -= alocar

    periodos = []
    for n in numeros:
        saldo = int(saldos_map.get(n, 0))
        if saldo <= 0:
            continue
        if admissao:
            ini, fim = periodo_bounds(admissao, n)
        else:
            ini = fim = None
        periodos.append({
            "numero": n,
            "inicio": ini,
            "fim": fim,
            "direito": saldo,
            "usados": 0,
            "reservados": 0,
            "saldo": saldo,
            "completo": True,
            "atual": False,
            "origem_transitoria": True,
            "inferido_sem_admissao": not bool(admissao),
        })
    return periodos



def serialize_periodo_aquisitivo_alloc(alloc: list[dict]) -> str:
    parts = []
    for item in alloc or []:
        n = int(item.get("numero") or 0)
        dias = int(item.get("dias") or item.get("consumidos") or 0)
        if n > 0 and dias > 0:
            parts.append(f"P{n}:{dias}")
    return " | ".join(parts)



def get_periodo_aquisitivo_atual(email: str, hoje: dt.date | None = None):
    adm = _colaborador_admissao(email)
    atual = current_partial_period(adm, hoje or dt.date.today()) if adm else None
    if not atual:
        return None
    return {
        "numero": atual["numero"],
        "inicio": atual["inicio"],
        "fim": atual["fim"],
        "label": f"Período {atual['numero']} — {atual['inicio'].strftime('%d/%m/%Y')} a {atual['fim'].strftime('%d/%m/%Y')}",
    }
