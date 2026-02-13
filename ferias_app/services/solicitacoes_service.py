from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Optional, Tuple

import smartsheet
from flask import session

from ..config import get_settings
from ..logging_config import get_logger
from ..utils import safe_lower, parse_date, format_date
from .auth_service import get_access_token
from .smartsheet_service import get_sheet, add_rows, columns_map

log = get_logger(__name__)

# Colunas esperadas na folha de SOLICITAÇÕES
COL_GESTOR_SOLICITANTE = "GESTOR SOLICITANTE"
COL_SOLICITACAO = "SOLICITAÇÃO"
COL_DATA_INICIO = "DATA INICIO"
COL_DATA_FIM = "DATA FIM"
COL_SALDO_TIPO = "SALDO TIPO"
COL_DIAS = "DIAS"
COL_STATUS = "STATUS"
COL_OBSERVACOES = "OBSERVAÇÕES"
COL_COLABORADOR = "COLABORADOR"
COL_CRIADO_POR = "Criado_por"

def _sheet_and_columns(token: str):
    s = get_settings()
    if not s.id_folha_solicitacoes:
        raise ValueError("ID_FOLHA_SOLICITACOES não configurado.")
    sheet = get_sheet(token, s.id_folha_solicitacoes)
    cmap = columns_map(sheet)
    return sheet, cmap

def _build_cell(cmap: Dict[str,int], title: str, value: Any):
    cid = cmap.get(title.upper())
    if not cid:
        return None
    return smartsheet.models.Cell({"column_id": cid, "value": value})

def validar_licenca_cerariana(formato: str, inicios: List[str]) -> Tuple[bool, str, List[Tuple[dt.date, dt.date]]]:
    """Regras DP:
    - até 3 períodos; mínimo 10 dias cada
    - se 3 períodos: obrigatoriamente 3x10
    - se 2 períodos: ambos >=10 e soma = 30 (ex: 20+10, 16+14, etc.)
    Obs: aqui validamos apenas o formato/quantidade e que as datas existam.
    O cálculo de fim é feito por duração.
    """
    formato = (formato or "").strip()
    starts = [parse_date(x) for x in inicios if (x or "").strip()]
    starts = [s for s in starts if s]
    if not starts:
        return False, "Informe ao menos a data de início do(s) período(s).", []

    if formato == "3x10" or (len(starts) == 3):
        if len(starts) != 3:
            return False, "Formato 3x10 exige 3 datas de início.", []
        durations = [10,10,10]
    else:
        # 2 períodos flexível (>=10 cada, soma 30) — aqui não coletamos durações do form,
        # então assumimos 15/15 caso o front não envie.
        # Se o front enviar durações, adapte aqui.
        if len(starts) != 2:
            return False, "Informe 2 datas de início para dois períodos.", []
        # default 15/15
        durations = [15,15]

    segs=[]
    for st, dur in zip(starts, durations):
        fi = st + dt.timedelta(days=dur-1)
        segs.append((st, fi))
    return True, "", segs

def criar_solicitacao_padrao(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Cria uma solicitação (linha) na folha de solicitações."""
    token = get_access_token()
    if not token:
        raise ValueError("Não autenticado (token ausente).")

    sheet, cmap = _sheet_and_columns(token)

    gestor_email = safe_lower(payload.get("gestor_email",""))
    colaborador_email = safe_lower(payload.get("colaborador_email",""))
    tipo_solicitacao = (payload.get("tipo_solicitacao") or "").strip()
    saldo_tipo = (payload.get("saldo_tipo") or "REGULAR").strip().upper()
    observacoes = (payload.get("observacoes") or "").strip()
    data_inicio = parse_date(payload.get("data_inicio",""))
    data_fim = parse_date(payload.get("data_fim",""))
    status = (payload.get("status") or "PENDENTE").strip().upper()

    if not colaborador_email:
        raise ValueError("Colaborador não informado.")
    if not data_inicio or not data_fim:
        raise ValueError("Data início/fim inválida.")
    dias = (data_fim - data_inicio).days + 1
    if dias <= 0:
        raise ValueError("Intervalo de datas inválido.")

    criado_por = safe_lower((session.get("user") or {}).get("email",""))

    row = smartsheet.models.Row()
    row.to_bottom = True
    row.cells = []

    for title, value in [
        (COL_GESTOR_SOLICITANTE, gestor_email),
        (COL_COLABORADOR, colaborador_email),
        (COL_SOLICITACAO, tipo_solicitacao),
        (COL_DATA_INICIO, format_date(data_inicio)),
        (COL_DATA_FIM, format_date(data_fim)),
        (COL_SALDO_TIPO, saldo_tipo),
        (COL_DIAS, dias),
        (COL_STATUS, status),
        (COL_OBSERVACOES, observacoes),
        (COL_CRIADO_POR, criado_por),
    ]:
        cell = _build_cell(cmap, title, value)
        if cell:
            row.cells.append(cell)

    result = add_rows(token, get_settings().id_folha_solicitacoes, [row])
    inserted = []
    try:
        inserted = [r.id for r in result.result]  # sdk
    except Exception:
        pass
    return {"ok": True, "inserted_ids": inserted}
