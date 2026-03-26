from __future__ import annotations

import datetime as dt

from ..legacy.core_legacy import (
    _canonical_status,
    _col_id,
    _colaborador_por_email,
    _cols_norm_map,
    _get_sheet_solicitacoes,
    _infer_saldo_tipo,
    _is_ajuste,
    _parse_date_value,
    formatar_data_br,
    get_col_map,
    get_smartsheet_client,
    parse_data,
    safe_lower,
)


def _iter_solicitacoes(sheet_sol):
    cols = get_col_map(sheet_sol)
    colsN = _cols_norm_map(cols)
    return {
        "col_colab": _col_id(colsN, "COLABORADOR"),
        "col_inicio": _col_id(colsN, "DATA INICIO", "DATA INÍCIO", "DATA INICIAL"),
        "col_fim": _col_id(colsN, "DATA FIM", "DATA FINAL"),
        "col_dias": _col_id(colsN, "DIAS"),
        "col_status": _col_id(colsN, "STATUS"),
        "col_solic": _col_id(colsN, "SOLICITAÇÃO", "SOLICITACAO"),
        "col_obs": _col_id(colsN, "OBSERVAÇÕES", "OBSERVACOES", "OBSERVACAO"),
        "col_tipo": _col_id(colsN, "SALDO TIPO", "TIPO SALDO", "TIPO_DE_SALDO", "TIPO DE SALDO"),
    }



def listar_solicitacoes(email: str):
    client = get_smartsheet_client()
    if not client:
        return []
    try:
        sheet_sol = _get_sheet_solicitacoes(client)
        m = _iter_solicitacoes(sheet_sol)
        dados = []
        for row in sheet_sol.rows:
            row_email = next((c.value for c in row.cells if c.column_id == (m["col_colab"] or -1)), None)
            if not row_email or safe_lower(row_email) != safe_lower(email):
                continue
            solicit = str(next((c.value for c in row.cells if c.column_id == (m["col_solic"] or -1)), "") or "").strip()
            if _is_ajuste(solicit):
                continue
            inicio_raw = next((c.value for c in row.cells if c.column_id == (m["col_inicio"] or -1)), "") or ""
            fim_raw = next((c.value for c in row.cells if c.column_id == (m["col_fim"] or -1)), "") or ""
            dias = next((c.value for c in row.cells if c.column_id == (m["col_dias"] or -1)), 0) or 0
            status = next((c.value for c in row.cells if c.column_id == (m["col_status"] or -1)), "") or ""
            obs = next((c.value for c in row.cells if c.column_id == (m["col_obs"] or -1)), "") or ""
            explicit_tipo = next((c.value for c in row.cells if c.column_id == (m["col_tipo"] or -1)), "") or ""
            saldo_tipo = _infer_saldo_tipo(obs, explicit_tipo)
            dados.append((row.id, formatar_data_br(inicio_raw), formatar_data_br(fim_raw), dias, status, solicit or "", saldo_tipo, obs or ""))
        return dados
    except Exception:
        return []



def listar_solicitacoes_equipes(emails: list[str]):
    client = get_smartsheet_client()
    if not client:
        return []
    allowed = {safe_lower(e) for e in (emails or []) if safe_lower(e)}
    if not allowed:
        return []
    try:
        sheet_sol = _get_sheet_solicitacoes(client)
        m = _iter_solicitacoes(sheet_sol)
        dados = []
        for row in sheet_sol.rows:
            row_email_n = safe_lower(next((c.value for c in row.cells if c.column_id == (m["col_colab"] or -1)), None))
            if not row_email_n or row_email_n not in allowed:
                continue
            solicit = str(next((c.value for c in row.cells if c.column_id == (m["col_solic"] or -1)), "") or "").strip()
            if _is_ajuste(solicit):
                continue
            inicio_raw = next((c.value for c in row.cells if c.column_id == (m["col_inicio"] or -1)), "") or ""
            fim_raw = next((c.value for c in row.cells if c.column_id == (m["col_fim"] or -1)), "") or ""
            dias = next((c.value for c in row.cells if c.column_id == (m["col_dias"] or -1)), 0) or 0
            status = next((c.value for c in row.cells if c.column_id == (m["col_status"] or -1)), "") or ""
            obs = next((c.value for c in row.cells if c.column_id == (m["col_obs"] or -1)), "") or ""
            explicit_tipo = next((c.value for c in row.cells if c.column_id == (m["col_tipo"] or -1)), "") or ""
            saldo_tipo = _infer_saldo_tipo(obs, explicit_tipo)
            dados.append((row.id, row_email_n, formatar_data_br(inicio_raw), formatar_data_br(fim_raw), dias, status, solicit or "", saldo_tipo, obs or ""))
        return dados
    except Exception:
        return []



def listar_solicitacoes_todas():
    client = get_smartsheet_client()
    if not client:
        return []
    try:
        sheet_sol = _get_sheet_solicitacoes(client)
        m = _iter_solicitacoes(sheet_sol)
        dados = []
        for row in sheet_sol.rows:
            row_email_n = safe_lower(next((c.value for c in row.cells if c.column_id == (m["col_colab"] or -1)), None))
            if not row_email_n:
                continue
            solicit = str(next((c.value for c in row.cells if c.column_id == (m["col_solic"] or -1)), "") or "").strip()
            if _is_ajuste(solicit):
                continue
            inicio_raw = next((c.value for c in row.cells if c.column_id == (m["col_inicio"] or -1)), "") or ""
            fim_raw = next((c.value for c in row.cells if c.column_id == (m["col_fim"] or -1)), "") or ""
            dias = next((c.value for c in row.cells if c.column_id == (m["col_dias"] or -1)), 0) or 0
            status = next((c.value for c in row.cells if c.column_id == (m["col_status"] or -1)), "") or ""
            obs = next((c.value for c in row.cells if c.column_id == (m["col_obs"] or -1)), "") or ""
            explicit_tipo = next((c.value for c in row.cells if c.column_id == (m["col_tipo"] or -1)), "") or ""
            saldo_tipo = _infer_saldo_tipo(obs, explicit_tipo)
            dados.append((row.id, row_email_n, formatar_data_br(inicio_raw), formatar_data_br(fim_raw), dias, status, solicit or "", saldo_tipo, obs or ""))
        return dados
    except Exception:
        return []



def get_ferias_mes(mes, ano):
    client = get_smartsheet_client()
    if not client:
        return []
    try:
        mes = int(mes)
        ano = int(ano)
        primeiro = dt.date(ano, mes, 1)
        ultimo = dt.date(ano, mes + 1, 1) - dt.timedelta(days=1) if mes < 12 else dt.date(ano, 12, 31)
        sheet_sol = _get_sheet_solicitacoes(client)
        m = _iter_solicitacoes(sheet_sol)
        ferias = []
        for row in sheet_sol.rows:
            email = next((c.value for c in row.cells if c.column_id == (m["col_colab"] or -1)), None)
            if not email:
                continue
            email = safe_lower(email)
            solicit = str(next((c.value for c in row.cells if c.column_id == (m["col_solic"] or -1)), "") or "").strip()
            if _is_ajuste(solicit):
                continue
            inicio_raw = next((c.value for c in row.cells if c.column_id == (m["col_inicio"] or -1)), None)
            fim_raw = next((c.value for c in row.cells if c.column_id == (m["col_fim"] or -1)), None)
            dt_inicio = _parse_date_value(inicio_raw)
            dt_fim = _parse_date_value(fim_raw)
            if not dt_inicio or not dt_fim or dt_inicio > ultimo or dt_fim < primeiro:
                continue
            dias = next((c.value for c in row.cells if c.column_id == (m["col_dias"] or -1)), 0) or 0
            status = next((c.value for c in row.cells if c.column_id == (m["col_status"] or -1)), "") or "PENDENTE"
            status = _canonical_status(status)
            obs = next((c.value for c in row.cells if c.column_id == (m["col_obs"] or -1)), "") or ""
            explicit_tipo = str(next((c.value for c in row.cells if c.column_id == (m["col_tipo"] or -1)), "") or "").strip()
            saldo_tipo = str(_infer_saldo_tipo(obs, explicit_tipo) or explicit_tipo or "-").strip() or "-"
            colab = _colaborador_por_email(email) or {}
            nome = colab.get("NOME COMPLETO") or colab.get("NOME") or email
            cargo = colab.get("CARGO") or colab.get("FUNÇÃO") or colab.get("FUNCAO") or ""
            setor = colab.get("SETOR") or colab.get("DEPARTAMENTO") or ""
            ferias.append({
                "row_id": row.id,
                "email": email,
                "nome_completo": nome,
                "cargo": cargo,
                "setor": setor,
                "data_inicio": formatar_data_br(dt_inicio),
                "data_fim": formatar_data_br(dt_fim),
                "dias": dias,
                "status": status,
                "solicitacao": solicit or "-",
                "saldo_tipo": saldo_tipo or "-",
            })
        ferias.sort(key=lambda x: (parse_data(x.get("data_inicio")) or dt.date.min))
        return ferias
    except Exception:
        return []
