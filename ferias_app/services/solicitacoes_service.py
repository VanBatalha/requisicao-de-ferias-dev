from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Optional, Tuple

import smartsheet
from flask import session

from ..config import get_settings
from ..logging_config import get_logger
from ..utils import safe_lower, parse_date, format_date
from .identity_service import emails_equivalentes, normalize_email_identity
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


def processar_solicitacao(payload: Dict[str, Any], user: Dict[str, Any] | None):
    """Processa a criação de solicitação mantendo o blueprint fino.

    Retorna `(payload_json, status_code)`.
    """
    from ..services.core_support import (
        STATUS_APROVADA,
        STATUS_RESERVA,
        _add_months,
        _col_id,
        _colaborador_admissao,
        _colaborador_regime,
        _cols_norm_map,
        _infer_saldo_tipo,
        _janela_licenca_certariana,
        _norm_status,
        _parse_date_value,
        add_rows_rest,
        ensure_primary_cell,
        get_col_map,
        get_smartsheet_client,
        listar_emails_colaboradores,
        safe_lower,
    )
    from .permissions_service import get_user_role, is_gestor, get_subordinados
    from .saldo_service import get_resumo_ferias, distribuir_solicitacao_por_periodo
    from .periodo_aquisitivo_service import serialize_periodo_aquisitivo_alloc
    from .smartsheet_adapter import get_sheet_solicitacoes, col_id_by_name, invalidate_sheet_cache
    from ..config import get_settings
    from ..rules import (
        RuleError,
        get_afastamento_dias,
        normalize_tipo_solicitacao,
        validate_intervalo_datas,
        validate_licenca_certariana,
        validate_premium_balance,
        validate_request_period,
    )

    if not user:
        return {"ok": False, "message": "Não autenticado."}, 401

    gestor_email = normalize_email_identity(user.get("email") or "")
    if not gestor_email:
        return {"ok": False, "message": "Usuário inválido."}, 400

    role = get_user_role(gestor_email)
    is_dp_or_admin = role in ("DP", "admin")
    if not (is_dp_or_admin or is_gestor(gestor_email)):
        return {"ok": False, "message": "Apenas gestores (ou DP/Admin) podem solicitar férias."}, 403

    colaborador_email = normalize_email_identity(payload.get("colaborador_email") or payload.get("colaborador") or "")
    tipo_solicitacao = (payload.get("tipo_solicitacao") or payload.get("tipo_solicitacao_out") or "").strip()
    data_inicio_str = payload.get("data_inicio")
    data_fim_str = payload.get("data_fim")
    observacoes = (payload.get("observacoes") or "").strip()
    saldo_tipo_req = (payload.get("saldo_tipo") or payload.get("tipo_ferias") or "REGULAR").strip().upper()
    if saldo_tipo_req not in ("REGULAR", "PREMIUM"):
        saldo_tipo_req = "REGULAR"

    certariana_segmentos = []
    cert_total_dias = 0

    if not colaborador_email:
        return {"ok": False, "message": "Selecione o colaborador."}, 400

    if emails_equivalentes(colaborador_email, gestor_email):
        return {
            "ok": False,
            "message": "Você pode consultar o próprio saldo, mas a solicitação de férias para si próprio deve ser feita pelo fluxo responsável/DP.",
        }, 403

    if not tipo_solicitacao:
        if saldo_tipo_req == "PREMIUM":
            tipo_solicitacao = "Gozo"
        else:
            return {"ok": False, "message": "Selecione o tipo de solicitação (Venda ou Gozo)."}, 400

    try:
        tipo_solicitacao_out = normalize_tipo_solicitacao(tipo_solicitacao)
    except RuleError as ve:
        return {"ok": False, "message": str(ve)}, 400

    is_afastamento = tipo_solicitacao_out in ("LICENÇA MATERNIDADE", "LICENÇA PATERNIDADE")

    if is_dp_or_admin:
        permitidos = set(listar_emails_colaboradores(only_ativos=True))
        if not any(emails_equivalentes(colaborador_email, item) for item in permitidos):
            return {"ok": False, "message": "Colaborador não encontrado (ou não está Ativo no cadastro)."}, 400
    else:
        permitidos = set(get_subordinados(gestor_email))
        if not any(emails_equivalentes(colaborador_email, item) for item in permitidos):
            return {"ok": False, "message": "Colaborador não pertence à sua equipe (ou não está vinculado ao seu gestor)."}, 403

    try:
        if is_afastamento:
            if not data_inicio_str:
                return {"ok": False, "message": "Para este afastamento, informe a Data início."}, 400
            dt_inicio = parse_date(data_inicio_str)
            if not dt_inicio:
                return {"ok": False, "message": "Data início inválida."}, 400
            dias_afastamento = get_afastamento_dias(tipo_solicitacao_out)
            dt_fim = dt_inicio + dt.timedelta(days=dias_afastamento - 1)
            data_fim_str = format_date(dt_fim)
        else:
            dt_inicio = parse_date(data_inicio_str or "")
            dt_fim = parse_date(data_fim_str or "")
            dias_novos = validate_intervalo_datas(dt_inicio, dt_fim)
            ok_periodo, msg = validate_request_period(dt_inicio, dt_fim, requester_email=gestor_email)
            if not ok_periodo:
                return {"ok": False, "message": msg}, 400
    except Exception:
        return {"ok": False, "message": "Datas inválidas."}, 400

    if (not is_dp_or_admin) and (not is_afastamento):
        try:
            regime = (_colaborador_regime(colaborador_email) or "").strip().upper()
            adm = _colaborador_admissao(colaborador_email)
            if regime == "CLT" and adm:
                resumo_tmp = get_resumo_ferias(colaborador_email)
                if resumo_tmp.get("total_solicitacoes", 0) <= 0:
                    liberado_em = _add_months(adm, 21)
                    if dt_inicio < liberado_em:
                        return {
                            "ok": False,
                            "message": f"Para regime CLT, a 1ª solicitação só é permitida a partir de {liberado_em.strftime('%d/%m/%Y')} (1 ano e 9 meses de empresa).",
                        }, 400
        except Exception:
            pass

    try:
        resumo = get_resumo_ferias(colaborador_email)
        dias_novos = (dt_fim - dt_inicio).days + 1
        if saldo_tipo_req == "PREMIUM":
            include_statuses = STATUS_APROVADA | STATUS_RESERVA
            try:
                validate_licenca_certariana(colaborador_email, float(dias_novos), dt_inicio=dt_inicio, dt_fim=dt_fim, include_statuses=include_statuses)
            except RuleError as ve:
                return {"ok": False, "message": str(ve)}, 400
            except Exception as e:
                return {"ok": False, "message": f"Erro ao validar fracionamento da Licença Certariana: {e}"}, 500

        reg_saldo = int(resumo["regular"]["saldo"])
        prem_saldo = int(resumo["premium"]["saldo"])
        periodo_alloc = []
        periodo_alloc_txt = ""
        saldo_tipo_final = saldo_tipo_req

        if is_afastamento:
            saldo_tipo_final = "REGULAR"
            periodo_alloc_txt = tipo_solicitacao_out
        elif saldo_tipo_req == "REGULAR":
            if dias_novos > reg_saldo:
                return {"ok": False, "message": f"Saldo insuficiente. Regular: {reg_saldo} dias. Para usar Licença Certariana, selecione 'Licença Certariana' em Tipo de Férias e informe um período válido conforme a regra: até 3 períodos, mínimo de 10 dias por período; se forem 3, deve ser 3×10."}, 400
            try:
                periodo_alloc = distribuir_solicitacao_por_periodo(colaborador_email, dias_novos)
                periodo_alloc_txt = serialize_periodo_aquisitivo_alloc(periodo_alloc)
            except Exception as e:
                return {"ok": False, "message": f"Não foi possível distribuir a solicitação por período aquisitivo: {e}"}, 400
        else:
            try:
                validate_premium_balance(int(prem_saldo), int(dias_novos))
            except RuleError as ve:
                return {"ok": False, "message": str(ve)}, 400
            saldo_tipo_final = "PREMIUM"
    except Exception as e:
        return {"ok": False, "message": f"Erro ao montar resumo/validações: {e}"}, 500

    if saldo_tipo_final == "PREMIUM" and not is_dp_or_admin:
        try:
            adm_c = _colaborador_admissao(colaborador_email)
            if not adm_c and prem_saldo <= 0:
                return {"ok": False, "message": "Licença Certariana ainda não está disponível para este colaborador."}, 400
            dias_base, win_start, win_end = _janela_licenca_certariana(adm_c, hoje=dt_inicio) if adm_c else (0, None, None)
            _ = dias_base
            if not (win_start and win_end):
                if prem_saldo <= 0:
                    return {"ok": False, "message": "Licença Certariana ainda não está disponível para este colaborador."}, 400
            elif not (win_start <= dt_inicio < win_end and win_start <= dt_fim < win_end):
                return {"ok": False, "message": f"Licença Certariana só pode ser utilizada entre {win_start.strftime('%d/%m/%Y')} e {(win_end - dt.timedelta(days=1)).strftime('%d/%m/%Y')} (não cumulativa e válida por 2 anos após a conquista)."}, 400
        except Exception:
            pass

    marker = f"Saldo: {saldo_tipo_final}"
    if marker.lower() not in observacoes.lower():
        observacoes = (observacoes + ("\n" if observacoes else "") + marker).strip()

    def add_cell_unique(cells_by_id: dict, col_id, value):
        try:
            cid = int(col_id) if col_id is not None else 0
        except Exception:
            cid = 0
        if cid > 0:
            cells_by_id[cid] = value

    def build_cells(cells_dict: dict):
        return [smartsheet.models.Cell({"column_id": cid, "value": value}) for cid, value in cells_dict.items()]

    def is_duplicate_solicitacao(sheet_sol):
        try:
            cols = get_col_map(sheet_sol)
            colsN = _cols_norm_map(cols)
            col_colab = _col_id(colsN, "COLABORADOR")
            col_solic = _col_id(colsN, "SOLICITAÇÃO", "SOLICITACAO")
            col_inicio = _col_id(colsN, "DATA INICIO", "DATA INÍCIO", "DATA INICIAL")
            col_fim = _col_id(colsN, "DATA FIM", "DATA FINAL")
            col_dias = _col_id(colsN, "DIAS")
            col_status = _col_id(colsN, "STATUS")
            col_tipo = _col_id(colsN, "SALDO TIPO", "TIPO SALDO", "TIPO_DE_SALDO", "TIPO DE SALDO", "TIPO DE FERIAS", "TIPO DE FÉRIAS")
            alvo_email = safe_lower(colaborador_email)
            alvo_tipo = str(tipo_solicitacao_out or "").strip().upper()
            alvo_saldo = str(saldo_tipo_final or "").strip().upper()
            alvo_inicio = str(data_inicio_str or "").strip()
            alvo_fim = str(data_fim_str or "").strip()
            alvo_dias = int(float(dias_novos or 0))
            for row in sheet_sol.rows:
                def _cell(cid):
                    return next((c.value for c in row.cells if c.column_id == (cid or -1)), None)
                row_email = safe_lower(str(_cell(col_colab) or ""))
                if row_email != alvo_email:
                    continue
                row_status = _norm_status(_cell(col_status) or "")
                if row_status not in (STATUS_RESERVA | STATUS_APROVADA):
                    continue
                row_tipo = str(_cell(col_solic) or "").strip().upper()
                row_saldo = str(_cell(col_tipo) or "").strip().upper()
                row_inicio = _parse_date_value(_cell(col_inicio))
                row_fim = _parse_date_value(_cell(col_fim))
                row_dias = _cell(col_dias) or 0
                try:
                    row_dias = int(float(row_dias or 0))
                except Exception:
                    row_dias = 0
                row_inicio = row_inicio.strftime("%Y-%m-%d") if row_inicio else str(_cell(col_inicio) or "").strip()
                row_fim = row_fim.strftime("%Y-%m-%d") if row_fim else str(_cell(col_fim) or "").strip()
                if row_tipo == alvo_tipo and row_saldo == alvo_saldo and row_inicio == alvo_inicio and row_fim == alvo_fim and row_dias == alvo_dias:
                    return True
        except Exception:
            return False
        return False

    try:
        client = get_smartsheet_client()
        if not client:
            return {"ok": False, "message": "Smartsheet client não inicializado (sem token)."}, 500
        sheet_sol = get_sheet_solicitacoes(client)
        col_colab = col_id_by_name(sheet_sol, "COLABORADOR")
        col_gestor = col_id_by_name(sheet_sol, "GESTOR SOLICITANTE")
        col_solic = col_id_by_name(sheet_sol, "SOLICITAÇÃO", "SOLICITACAO")
        col_inicio = col_id_by_name(sheet_sol, "DATA INICIO", "DATA INÍCIO", "DATA INICIAL")
        col_fim = col_id_by_name(sheet_sol, "DATA FIM", "DATA FINAL")
        col_dias = col_id_by_name(sheet_sol, "DIAS")
        col_status = col_id_by_name(sheet_sol, "STATUS")
        col_obs = col_id_by_name(sheet_sol, "OBSERVAÇÕES", "OBSERVACOES", "OBSERVAÇÃO", "OBSERVACAO")
        col_saldo_tipo = col_id_by_name(sheet_sol, "SALDO TIPO", "SALDO_TIPO", "TIPO DE FERIAS", "TIPO DE FÉRIAS", "TIPO FERIAS")
        col_periodo_aq = col_id_by_name(sheet_sol, "PERIODO_AQUISITIVO", "PERÍODO AQUISITIVO", "PERIODO AQUISITIVO")
        rows_to_add = []
        if is_duplicate_solicitacao(sheet_sol):
            return {"ok": False, "message": "Já existe uma solicitação igual para este colaborador nesse período. A duplicidade foi bloqueada."}, 400
        new_row = smartsheet.models.Row()
        new_row.to_top = True
        cells = {}
        add_cell_unique(cells, col_colab, colaborador_email)
        add_cell_unique(cells, col_gestor, gestor_email)
        add_cell_unique(cells, col_saldo_tipo, saldo_tipo_final)
        add_cell_unique(cells, col_solic, tipo_solicitacao_out)
        add_cell_unique(cells, col_inicio, data_inicio_str)
        add_cell_unique(cells, col_fim, data_fim_str)
        add_cell_unique(cells, col_dias, dias_novos)
        add_cell_unique(cells, col_status, "PENDENTE")
        add_cell_unique(cells, col_obs, observacoes)
        if col_periodo_aq and periodo_alloc_txt:
            add_cell_unique(cells, col_periodo_aq, periodo_alloc_txt)
        new_row.cells = build_cells(cells)
        ensure_primary_cell(sheet_sol, new_row, colaborador_email)
        rows_to_add.append(new_row)
        inserted_ids = add_rows_rest(get_settings().id_folha_solicitacoes, rows_to_add, timeout=25)
        invalidate_sheet_cache(get_settings().id_folha_solicitacoes)

        # Confirmação defensiva: evita retornar sucesso quando a API aceitou a
        # chamada, mas a linha não aparece no sheet após refresh.
        sheet_confirm = get_sheet_solicitacoes(client, force_refresh=True)
        inserted_set = {int(x) for x in (inserted_ids or []) if x}

        def _confirm_row_exists(sheet_obj) -> bool:
            try:
                cols = get_col_map(sheet_obj)
                colsN = _cols_norm_map(cols)
                c_colab = _col_id(colsN, "COLABORADOR")
                c_inicio = _col_id(colsN, "DATA INICIO", "DATA INÍCIO", "DATA INICIAL")
                c_fim = _col_id(colsN, "DATA FIM", "DATA FINAL")
                c_dias = _col_id(colsN, "DIAS")
                c_solic = _col_id(colsN, "SOLICITAÇÃO", "SOLICITACAO")
                for row in getattr(sheet_obj, "rows", []) or []:
                    if inserted_set and int(getattr(row, "id", 0) or 0) in inserted_set:
                        return True

                    def _cell(cid):
                        return next((c.value for c in row.cells if c.column_id == (cid or -1)), None)

                    row_email = normalize_email_identity(_cell(c_colab) or "")
                    row_inicio = _parse_date_value(_cell(c_inicio))
                    row_fim = _parse_date_value(_cell(c_fim))
                    row_inicio_s = row_inicio.strftime("%Y-%m-%d") if row_inicio else str(_cell(c_inicio) or "").strip()
                    row_fim_s = row_fim.strftime("%Y-%m-%d") if row_fim else str(_cell(c_fim) or "").strip()
                    try:
                        row_dias = int(float(_cell(c_dias) or 0))
                    except Exception:
                        row_dias = 0
                    row_solic = str(_cell(c_solic) or "").strip().upper()
                    if (
                        emails_equivalentes(row_email, colaborador_email)
                        and row_inicio_s == str(data_inicio_str or "").strip()
                        and row_fim_s == str(data_fim_str or "").strip()
                        and row_dias == int(dias_novos or 0)
                        and row_solic == str(tipo_solicitacao_out or "").strip().upper()
                    ):
                        return True
            except Exception:
                return bool(inserted_set)
            return False

        if not _confirm_row_exists(sheet_confirm):
            return {
                "ok": False,
                "message": "A solicitação foi enviada ao Smartsheet, mas não foi possível confirmar a gravação da linha. Tente novamente ou acione o DP antes de considerar a solicitação registrada.",
                "inserted_ids": inserted_ids,
            }, 502
    except Exception as e:
        return {"ok": False, "message": f"Erro ao salvar solicitação: {e}"}, 500

    saldo_base = 0 if is_afastamento else (reg_saldo if saldo_tipo_final == "REGULAR" else prem_saldo)
    saldo_atualizado = saldo_base if is_afastamento else (saldo_base - dias_novos)
    return {
        "ok": True,
        "sheet_id": get_settings().id_folha_solicitacoes,
        "inserted_ids": inserted_ids,
        "row_id": inserted_ids[0] if inserted_ids else None,
        "message": f"Solicitação registrada com sucesso. Saldo restante: {saldo_atualizado}.",
        "saldo_atualizado": saldo_atualizado,
        "saldo_tipo": saldo_tipo_final,
        "periodo_aquisitivo": periodo_alloc_txt,
        "periodos_consumidos": periodo_alloc,
    }, 200
