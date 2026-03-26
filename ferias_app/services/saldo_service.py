from __future__ import annotations

import datetime as dt

from ..legacy.core_legacy import (
    STATUS_APROVADA,
    STATUS_RESERVA,
    _col_id,
    _colaborador_admissao,
    _colaborador_por_email,
    _cols_norm_map,
    _get_sheet_solicitacoes,
    _infer_saldo_tipo,
    _is_ajuste,
    _janela_licenca_certariana,
    _norm_solicitacao,
    _norm_status,
    _parse_date_value,
    get_col_map,
    get_smartsheet_client,
    safe_lower,
)
from .periodo_aquisitivo_service import (
    allocate_period_balance,
    completed_aquisitive_periods,
    get_periodo_aquisitivo_atual,
)


def get_resumo_ferias(email: str):
    client = get_smartsheet_client()
    if not client:
        raise RuntimeError("Usuário não autenticado")

    email = safe_lower(email)
    regular_base = 0
    try:
        colab = _colaborador_por_email(email) or {}
        adm = _colaborador_admissao(email)
        if adm:
            regular_base = completed_aquisitive_periods(adm) * 30
        else:
            regular_base = colab.get("DIAS DE DIREITO") or colab.get("DIAS DIREITO") or 0
        regular_base = int(regular_base or 0)
    except Exception:
        regular_base = 0

    premium_base = 0
    prem_win_start = None
    prem_win_end = None
    try:
        adm = _colaborador_admissao(email)
        premium_base, prem_win_start, prem_win_end = _janela_licenca_certariana(adm) if adm else (0, None, None)
    except Exception:
        premium_base = 0
        prem_win_start = None
        prem_win_end = None

    regular_usados = regular_reservados = premium_usados = premium_reservados = 0
    total_solicitacoes = 0
    ajuste_regular = ajuste_premium = 0

    try:
        sheet_sol = _get_sheet_solicitacoes(client)
        cols = get_col_map(sheet_sol)
        colsN = _cols_norm_map(cols)
        col_colab = _col_id(colsN, "COLABORADOR")
        col_status = _col_id(colsN, "STATUS")
        col_dias = _col_id(colsN, "DIAS")
        col_solic = _col_id(colsN, "SOLICITAÇÃO", "SOLICITACAO")
        col_obs = _col_id(colsN, "OBSERVAÇÕES", "OBSERVACOES", "OBSERVACAO")
        col_tipo = _col_id(colsN, "SALDO TIPO", "TIPO SALDO", "TIPO_DE_SALDO", "TIPO DE SALDO")
        col_inicio = _col_id(colsN, "DATA INICIO", "DATA INÍCIO", "DATA INICIAL", "INICIO", "INÍCIO")

        for row in sheet_sol.rows:
            row_email = next((c.value for c in row.cells if c.column_id == (col_colab or -1)), None)
            if safe_lower(row_email) != email:
                continue

            solicit_raw = next((c.value for c in row.cells if c.column_id == (col_solic or -1)), "") or ""
            solicit = str(solicit_raw).strip()
            obs = next((c.value for c in row.cells if c.column_id == (col_obs or -1)), "") or ""
            explicit_tipo = next((c.value for c in row.cells if c.column_id == (col_tipo or -1)), "") or ""
            saldo_tipo = _infer_saldo_tipo(obs, explicit_tipo)
            status_raw = next((c.value for c in row.cells if c.column_id == (col_status or -1)), "") or ""
            status = _norm_status(status_raw)
            dias_raw = next((c.value for c in row.cells if c.column_id == (col_dias or -1)), 0) or 0
            try:
                dias = int(float(dias_raw))
            except Exception:
                dias = 0

            inicio_val = next((c.value for c in row.cells if c.column_id == (col_inicio or -1)), None)
            dt_ini_row = _parse_date_value(inicio_val) if inicio_val else None

            if _is_ajuste(solicit):
                if status in STATUS_APROVADA:
                    solicit_norm = _norm_solicitacao(solicit)
                    if ("premium" in solicit_norm) or ("certariana" in solicit_norm):
                        ajuste_premium += dias
                    else:
                        ajuste_regular += dias
                continue

            solicit_norm = _norm_solicitacao(solicit)
            if ("licenca maternidade" in solicit_norm) or ("licenca paternidade" in solicit_norm):
                continue

            total_solicitacoes += 1

            if saldo_tipo == "PREMIUM":
                if prem_win_start and prem_win_end and dt_ini_row and not (prem_win_start <= dt_ini_row < prem_win_end):
                    continue
                if status in STATUS_APROVADA:
                    premium_usados += dias
                elif status in STATUS_RESERVA:
                    premium_reservados += dias
            else:
                if status in STATUS_APROVADA:
                    regular_usados += dias
                elif status in STATUS_RESERVA:
                    regular_reservados += dias

        _ = col_inicio  # mantido para paridade/robustez de mapeamento
    except Exception:
        pass

    regular_direito = max(0, regular_base + ajuste_regular)
    premium_direito = max(0, premium_base + ajuste_premium)
    regular_saldo = max(0, regular_direito - regular_usados - regular_reservados)
    premium_saldo = max(0, premium_direito - premium_usados - premium_reservados)

    adm = _colaborador_admissao(email)
    regular_periodos = allocate_period_balance(regular_direito, regular_usados, regular_reservados, adm)
    periodo_atual = get_periodo_aquisitivo_atual(email)

    return {
        "regular": {
            "direito": int(regular_direito),
            "usados": int(regular_usados),
            "reservados": int(regular_reservados),
            "saldo": int(regular_saldo),
            "ajustes": int(ajuste_regular),
            "periodos": regular_periodos,
            "periodo_atual": periodo_atual,
        },
        "premium": {
            "direito": int(premium_direito),
            "usados": int(premium_usados),
            "reservados": int(premium_reservados),
            "saldo": int(premium_saldo),
            "ajustes": int(ajuste_premium),
        },
        "total_solicitacoes": int(total_solicitacoes),
    }



def distribuir_solicitacao_por_periodo(email: str, dias_solicitados: int, hoje: dt.date | None = None) -> list[dict]:
    resumo = get_resumo_ferias(email)
    periodos = resumo.get("regular", {}).get("periodos", []) or []
    restantes = int(dias_solicitados or 0)
    alloc = []
    for p in periodos:
        saldo = int(p.get("saldo") or 0)
        if saldo <= 0:
            continue
        consumir = min(saldo, restantes)
        if consumir > 0:
            alloc.append({
                "numero": int(p.get("numero") or 0),
                "inicio": p.get("inicio"),
                "fim": p.get("fim"),
                "dias": consumir,
            })
            restantes -= consumir
        if restantes <= 0:
            break
    if restantes > 0:
        raise ValueError(f"Saldo insuficiente para distribuir {dias_solicitados} dia(s). Faltam {restantes}.")
    return alloc
