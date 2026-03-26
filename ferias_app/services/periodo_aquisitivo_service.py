from __future__ import annotations

import datetime as dt

from ..legacy.core_legacy import (
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
    if not admissao:
        return None
    hoje = hoje or dt.date.today()
    completos = completed_aquisitive_periods(admissao, hoje)
    ini = _add_months(admissao, completos * 12)
    prox = _add_months(admissao, (completos + 1) * 12)
    if hoje < ini:
        return None
    return {
        "numero": completos + 1,
        "inicio": ini,
        "fim": prox - dt.timedelta(days=1),
        "completo": False,
    }



def allocate_period_balance(
    total_direito: int,
    total_usados: int,
    total_reservados: int,
    admissao: dt.date | None,
    hoje: dt.date | None = None,
):
    """Distribui o saldo disponível atual nos últimos períodos completos.

    Regra temporária de transição até o histórico estar totalmente saneado.
    """
    hoje = hoje or dt.date.today()
    saldo_disponivel = max(0, int(total_direito or 0) - int(total_usados or 0) - int(total_reservados or 0))
    if not admissao or saldo_disponivel <= 0:
        return []

    completos = completed_aquisitive_periods(admissao, hoje)
    if completos <= 0:
        return []

    qtd_periodos = max(1, (saldo_disponivel + 29) // 30)
    ultimo_num = completos
    primeiro_num = max(1, ultimo_num - qtd_periodos + 1)
    numeros = list(range(primeiro_num, ultimo_num + 1))
    saldos_map = {n: 0 for n in numeros}
    restante = saldo_disponivel

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
        ini, fim = periodo_bounds(admissao, n)
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
