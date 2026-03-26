from __future__ import annotations

import datetime as dt

import smartsheet
from flask import jsonify, request, session

from .base import bp
from ..core import (
    ID_FOLHA_CADASTRO,
    ID_FOLHA_SOLICITACOES,
    atualizar_relacao_gestor,
    col_id_by_name,
    get_col_map,
    get_ferias_mes,
    get_resumo_ferias,
    get_sheet_solicitacoes,
    get_smartsheet_client,
    get_subordinados_direto,
    invalidate_sheet_cache,
    is_colaborador_ativo,
    listar_colaboradores,
    safe_lower,
    tem_grupo,
)
# OBS: o módulo legacy tem vários helpers com prefixo "_".
# Eles NÃO entram em importações genéricas, então precisam ser
# importados explicitamente quando usados aqui.
from ..services.core_support import (
    _canonical_status,
    _col_id,
    _cols_norm_map,
    _infer_saldo_tipo,
    _is_ajuste,
    _norm_email,
    _norm_status,
    _norm_title,
    _parse_date_value,
)
def _has_dp_access(email: str | None) -> bool:
    """Retorna True se o usuário tem acesso ao Painel DP.

    Fail-closed: se der erro ao consultar permissões, não libera acesso.
    """
    try:
        if not email:
            return False
        return tem_grupo(email, "DP") or tem_grupo(email, "Administrador")
    except Exception:
        return False

@bp.route("/api/dp/colaboradores")
def api_dp_colaboradores():
    user = session.get("user")
    if not user or not _has_dp_access(user.get("email")):
        return jsonify({"ok": False, "message": "Acesso negado"}), 403

    status_filter = (request.args.get("status") or "").upper().strip()

    try:
        colaboradores = listar_colaboradores()

        # Filtra por status se solicitado (aceita coluna Status/STATUS)
        if status_filter == "ATIVO":
            colaboradores = [c for c in colaboradores if is_colaborador_ativo(c)]
        elif status_filter == "INATIVO":
            colaboradores = [c for c in colaboradores if not is_colaborador_ativo(c)]

        colaboradores = sorted(
            colaboradores,
            key=lambda c: (str(c.get("NOME COMPLETO") or c.get("nome") or "").casefold(), str(c.get("EMAIL DA EMPRESA") or "").casefold())
        )

        return jsonify({
            "ok": True,
            "colaboradores": colaboradores
        })
    except Exception as e:
        print(f"ERRO em api_dp_colaboradores: {e}")
        return jsonify({"ok": False, "message": str(e)}), 500
@bp.route("/api/dp/saldos/<path:email>")
def api_dp_saldos(email):
    user = session.get("user")
    if not user or not _has_dp_access(user.get("email")):
        return jsonify({"ok": False, "message": "Acesso negado"}), 403

    email = safe_lower(email or "")
    if not email:
        return jsonify({"ok": False, "message": "Email inválido"}), 400

    try:
        resumo = get_resumo_ferias(email)
        return jsonify({
            "ok": True,
            "email": email,
            "regular": resumo["regular"],
            "premium": resumo["premium"],
        })
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


@bp.route("/api/dp/ajustes/lancar", methods=["POST"])
def api_dp_ajustes_lancar():
    try:
        user = session.get("user")
        if not user or not _has_dp_access(user.get("email")):
            return jsonify({"ok": False, "message": "Acesso negado"}), 403

        try:
            client = get_smartsheet_client()
        except Exception as e:
            return jsonify({"ok": False, "message": f"Erro ao criar cliente Smartsheet: {e}"}), 500

        if not client:
            return jsonify({"ok": False, "message": "Smartsheet não configurado (token ausente)"}), 401

        payload = request.get_json(silent=True) or {}
        colab_email = safe_lower(payload.get("colaborador_email") or payload.get("email") or "")
        solicitacao_raw = (payload.get("solicitacao") or "").strip()
        obs_user = (payload.get("observacoes") or "").strip()

        try:
            dias = int(float(payload.get("dias") or 0))
        except Exception:
            dias = 0

        if not colab_email:
            return jsonify({"ok": False, "message": "Colaborador inválido"}), 400

        ns = _norm_title(solicitacao_raw)
        if ns in ("ajuste ferias", "ajuste férias"):
            solicitacao = "AJUSTE FÉRIAS"
            saldo_tipo = "REGULAR"
        elif ns in ("ajuste premium",):
            solicitacao = "AJUSTE PREMIUM"
            saldo_tipo = "PREMIUM"
        elif ns in ("ajuste certariana", "ajuste licenca certariana", "ajuste licença certariana",
                    "ajuste licenca", "ajuste licença"):
            solicitacao = "AJUSTE CERTARIANA"
            saldo_tipo = "PREMIUM"
        else:
            return jsonify({"ok": False, "message": "Solicitação inválida"}), 400

        if dias == 0:
            return jsonify({"ok": False, "message": "Dias deve ser diferente de zero"}), 400

        dp_email = safe_lower(user.get("email") or "")
        obs_final = obs_user
        complemento = f"Ajuste feito pelo DP ({dp_email})"
        if complemento.lower() not in obs_final.lower():
            obs_final = (obs_final + ("\n" if obs_final else "") + complemento).strip()

        hoje = dt.date.today().strftime("%Y-%m-%d")

        sheet_sol = get_sheet_solicitacoes(client)
        cols = get_col_map(sheet_sol)
        colsN = _cols_norm_map(cols)
        cols_objN = {_norm_title(c.title): c for c in getattr(sheet_sol, "columns", [])}

        def _get_col_obj(*names):
            for n in names:
                c = cols_objN.get(_norm_title(n))
                if c:
                    return c
            return None

        col_colab_obj = _get_col_obj("COLABORADOR")
        col_solic_obj = _get_col_obj("SOLICITAÇÃO", "SOLICITACAO")
        col_inicio_obj = _get_col_obj("DATA INICIO", "DATA INÍCIO", "DATA INICIAL")
        col_fim_obj = _get_col_obj("DATA FIM", "DATA FINAL")
        col_dias_obj = _get_col_obj("DIAS")
        col_status_obj = _get_col_obj("STATUS")
        col_gestor_obj = _get_col_obj("GESTOR SOLICITANTE")
        col_tipo_obj = _get_col_obj("SALDO TIPO", "TIPO SALDO", "TIPO_DE_SALDO", "TIPO DE SALDO")
        col_obs_obj = _get_col_obj("OBSERVAÇÕES", "OBSERVACOES", "OBSERVACAO")

        col_colab = col_colab_obj.id if col_colab_obj else _col_id(colsN, "COLABORADOR")
        col_solic = col_solic_obj.id if col_solic_obj else _col_id(colsN, "SOLICITAÇÃO", "SOLICITACAO")
        col_inicio = col_inicio_obj.id if col_inicio_obj else _col_id(colsN, "DATA INICIO", "DATA INÍCIO", "DATA INICIAL")
        col_fim = col_fim_obj.id if col_fim_obj else _col_id(colsN, "DATA FIM", "DATA FINAL")
        col_dias = col_dias_obj.id if col_dias_obj else _col_id(colsN, "DIAS")
        col_status = col_status_obj.id if col_status_obj else _col_id(colsN, "STATUS")
        col_gestor = col_gestor_obj.id if col_gestor_obj else _col_id(colsN, "GESTOR SOLICITANTE")
        col_tipo = col_tipo_obj.id if col_tipo_obj else _col_id(colsN, "SALDO TIPO", "TIPO SALDO", "TIPO_DE_SALDO", "TIPO DE SALDO")
        col_obs = col_obs_obj.id if col_obs_obj else _col_id(colsN, "OBSERVAÇÕES", "OBSERVACOES", "OBSERVACAO")

        if not col_colab:
            return jsonify({"ok": False, "message": "Coluna COLABORADOR não encontrada na folha de solicitações."}), 500
        if not col_dias:
            return jsonify({"ok": False, "message": "Coluna DIAS não encontrada na folha de solicitações."}), 500

        def _picklist_match(col_obj, value):
            if not col_obj or value is None:
                return value
            opts = getattr(col_obj, "options", None) or []
            if not opts:
                return value
            if value in opts:
                return value
            v = str(value).strip().lower()
            for o in opts:
                if str(o).strip().lower() == v:
                    return o
            return None

        new_row = smartsheet.models.Row()
        new_row.to_top = True
        new_row.cells = []

        def add_cell(col_id, value):
            if isinstance(col_id, int) and col_id > 0 and value is not None:
                new_row.cells.append(smartsheet.models.Cell({"column_id": col_id, "value": value}))

        solicitacao_val = _picklist_match(col_solic_obj, solicitacao) or solicitacao
        saldo_tipo_val = _picklist_match(col_tipo_obj, saldo_tipo) or saldo_tipo
        status_val = (
            _picklist_match(col_status_obj, "APROVADA") or
            _picklist_match(col_status_obj, "Aprovada") or
            _picklist_match(col_status_obj, "APROVADO") or
            _picklist_match(col_status_obj, "Aprovado") or
            "APROVADA"
        )

        add_cell(col_colab, colab_email)
        add_cell(col_solic, solicitacao_val)
        add_cell(col_inicio, hoje)
        add_cell(col_fim, hoje)
        add_cell(col_tipo, saldo_tipo_val)
        add_cell(col_dias, dias)
        add_cell(col_status, status_val)
        add_cell(col_gestor, dp_email)
        add_cell(col_obs, obs_final)

        if not getattr(new_row, "cells", None):
            return jsonify({"ok": False, "message": "Nenhuma célula válida foi montada para o ajuste."}), 500

        resp = client.Sheets.add_rows(ID_FOLHA_SOLICITACOES, [new_row])
        if resp is None:
            return jsonify({
                "ok": False,
                "message": "Smartsheet retornou resposta vazia (None).",
                "sheet_id": ID_FOLHA_SOLICITACOES,
            }), 500

        result = getattr(resp, "result", None)
        if isinstance(result, list):
            if len(result) == 0:
                return jsonify({
                    "ok": False,
                    "message": "Smartsheet não criou a linha (result vazio).",
                    "sheet_id": ID_FOLHA_SOLICITACOES,
                }), 500
        else:
            err = result
            return jsonify({
                "ok": False,
                "message": "Erro do Smartsheet ao inserir linha.",
                "sheet_id": ID_FOLHA_SOLICITACOES,
                "smartsheet": {
                    "code": getattr(err, "code", None),
                    "name": getattr(err, "name", None),
                    "message": getattr(err, "message", None),
                    "errorCode": getattr(err, "errorCode", None),
                    "refId": getattr(err, "refId", None),
                    "statusCode": getattr(err, "statusCode", None),
                    "recommendation": getattr(err, "recommendation", None),
                }
            }), 500

        invalidate_sheet_cache(ID_FOLHA_SOLICITACOES)

        inserted_ids = []
        try:
            inserted_ids = [r.id for r in (resp.result or []) if getattr(r, "id", None)]
        except Exception:
            inserted_ids = []

        retorno = {
            "ok": True,
            "message": "Ajuste lançado com sucesso.",
            "sheet_id": ID_FOLHA_SOLICITACOES,
            "inserted_ids": inserted_ids,
            "row_id": inserted_ids[0] if inserted_ids else None,
        }

        try:
            resumo = get_resumo_ferias(colab_email)
            retorno["regular"] = resumo.get("regular")
            retorno["premium"] = resumo.get("premium")
        except Exception as e:
            retorno["warning"] = f"Ajuste salvo, mas não foi possível recalcular os saldos imediatamente: {e}"

        return jsonify(retorno)

    except Exception as e:
        return jsonify({"ok": False, "message": f"Erro ao lançar ajuste: {e}"}), 500

@bp.route("/api/dp/colaborador/<email>")
def api_dp_colaborador(email):
    user = session.get("user")
    if not user or not _has_dp_access(user.get("email")):
        return jsonify({"ok": False, "message": "Acesso negado"}), 403
    
    try:
        colaboradores = listar_colaboradores()
        email_lower = safe_lower(email)
        colab = next(
            (c for c in colaboradores if safe_lower(c.get("EMAIL DA EMPRESA")) == email_lower),
            None
        )
        
        if not colab:
            return jsonify({"ok": False, "message": "Colaborador não encontrado"}), 404
        
        return jsonify({
            "ok": True,
            "colaborador": colab
        })
    except Exception as e:
        print(f"ERRO em api_dp_colaborador: {e}")
        return jsonify({"ok": False, "message": str(e)}), 500


# ============================================
# API: dp - GESTORES (relação Gestor -> Subordinados)
# ============================================

@bp.route("/api/dp/gestores/relacao", methods=["GET", "POST"])
def api_dp_gestores_relacao():
    user = session.get("user")
    if not user or not _has_dp_access(user.get("email")):
        return jsonify({"ok": False, "message": "Acesso negado"}), 403

    if request.method == "GET":
        try:
            gestor = _norm_email(request.args.get("gestor") or "")
            if not gestor:
                return jsonify({"ok": True, "gestor": "", "subordinados": []})
            subs = get_subordinados_direto(gestor)
            return jsonify({"ok": True, "gestor": gestor, "subordinados": subs})
        except Exception as e:
            return jsonify({"ok": False, "message": f"Erro ao carregar relação: {e}"}), 500

    payload = request.get_json(silent=True) or {}
    gestor = _norm_email(payload.get("gestor") or "")
    subordinados = payload.get("subordinados") or payload.get("subordinates") or []
    if isinstance(subordinados, str):
        subordinados = [subordinados]

    if not gestor:
        return jsonify({"ok": False, "message": "Gestor é obrigatório"}), 400

    try:
        atualizar_relacao_gestor(gestor, subordinados)
        return jsonify({"ok": True, "message": "Relação atualizada com sucesso."})
    except Exception as e:
        return jsonify({"ok": False, "message": f"Erro ao salvar relação: {e}"}), 500


@bp.route("/api/dp/gestores/superior", methods=["GET", "POST"])
def api_dp_gestor_superior():
    """Lê/atualiza a coluna GESTOR SUPERIOR do colaborador (cadastro)."""
    user = session.get("user")
    if not user or not _has_dp_access(user.get("email")):
        return jsonify({"ok": False, "message": "Acesso negado"}), 403

    try:
        client = get_smartsheet_client()
    except Exception as e:
        return jsonify({"ok": False, "message": f"Erro ao criar cliente Smartsheet: {e}"}), 500

    if not client:
        return jsonify({"ok": False, "message": "Smartsheet não configurado (token ausente)"}), 401

    sheet = client.Sheets.get_sheet(ID_FOLHA_CADASTRO)
    # Busca colunas por nome com tolerância a variações (acentos/maiúsculas)
    col_email = col_id_by_name(sheet, *["EMAIL DA EMPRESA", "EMAIL", "E-MAIL", "EMAIL EMPRESA"])
    col_sup = col_id_by_name(sheet, *["GESTOR SUPERIOR", "GESTOR SUPERIOR ", "GESTOR_SUPERIOR"])
    if not col_email or not col_sup:
        return jsonify({"ok": False, "message": "Colunas de EMAIL e/ou GESTOR SUPERIOR não encontradas no cadastro."}), 500

    if request.method == "GET":
        colaborador = _norm_email(request.args.get("colaborador") or "")
        if not colaborador:
            return jsonify({"ok": True, "colaborador": "", "gestor_superior": ""})
        for row in sheet.rows:
            row_email = _norm_email(next((c.value for c in row.cells if c.column_id == col_email), ""))
            if row_email == colaborador:
                valor = next((c.value for c in row.cells if c.column_id == col_sup), "") or ""
                return jsonify({"ok": True, "colaborador": colaborador, "gestor_superior": str(valor).strip()})
        return jsonify({"ok": False, "message": "Colaborador não encontrado"}), 404

    payload = request.get_json(silent=True) or {}
    colaborador = _norm_email(payload.get("colaborador") or "")
    valor = (payload.get("gestor_superior") or payload.get("valor") or "").strip()
    if not colaborador:
        return jsonify({"ok": False, "message": "Colaborador é obrigatório"}), 400
    if not valor:
        return jsonify({"ok": False, "message": "Gestor Superior é obrigatório"}), 400

    # normaliza valores especiais
    if safe_lower(valor) in ("dp",):
        valor_out = "DP"
    elif safe_lower(valor) in ("gestor",):
        valor_out = "GESTOR"
    else:
        valor_out = _norm_email(valor)

    # atualiza a linha
    target_row_id = None
    for row in sheet.rows:
        row_email = _norm_email(next((c.value for c in row.cells if c.column_id == col_email), ""))
        if row_email == colaborador:
            target_row_id = row.id
            break

    if not target_row_id:
        return jsonify({"ok": False, "message": "Colaborador não encontrado"}), 404

    try:
        row_update = smartsheet.models.Row()
        row_update.id = target_row_id
        cell = smartsheet.models.Cell()
        cell.column_id = col_sup
        cell.value = valor_out
        row_update.cells = [cell]
        client.Sheets.update_rows(ID_FOLHA_CADASTRO, [row_update])
        try:
            if hasattr(g, "_cadastro_colaboradores"):
                delattr(g, "_cadastro_colaboradores")
        except Exception:
            pass
        return jsonify({"ok": True, "message": "Gestor Superior atualizado com sucesso."})
    except Exception as e:
        return jsonify({"ok": False, "message": f"Erro ao atualizar Gestor Superior: {e}"}), 500

# ============================================
# API: dp - FÉRIAS (Planilha 2890766507528068)
# ============================================

@bp.route("/api/dp/ferias-mes")
def api_dp_ferias_mes():
    user = session.get("user")
    if not user or not _has_dp_access(user.get("email")):
        return jsonify({"ok": False, "message": "Acesso negado"}), 403
    
    mes = request.args.get("mes", type=int)
    ano = request.args.get("ano", type=int)
    
    if not mes or not ano:
        hoje = dt.date.today()
        mes = hoje.month
        ano = hoje.year
    
    try:
        ferias = get_ferias_mes(mes, ano)
        
        return jsonify({
            "ok": True,
            "ferias": ferias,
            "mes": mes,
            "ano": ano
        })
    except Exception as e:
        print(f"ERRO em api_dp_ferias_mes: {e}")
        return jsonify({"ok": False, "message": str(e)}), 500

# ============================================
# API: DP ALTERAR STATUS
# ============================================

@bp.route("/api/dp/atualizar-status-solicitacao", methods=["POST"])
def api_dp_atualizar_status():
    """DP pode alterar status das solicitacoes"""
    user = session.get("user")
    if not user or not _has_dp_access(user.get("email")):
        return jsonify({"ok": False, "message": "Acesso negado"}), 403
    
    payload = request.get_json(silent=True) or {}
    row_id = payload.get("row_id")
    novo_status = (payload.get("status") or "").strip()
    
    if not row_id or not novo_status:
        return jsonify({"ok": False, "message": "row_id e status sao obrigatorios"}), 400
    
    # Status permitidos
    status_permitidos = ["APROVADA", "CANCELADA", "REPROVADO", "EM ANÁLISE", "EM ANALISE", "PENDENTE"]
    novo_status_upper = novo_status.upper()
    if novo_status_upper not in status_permitidos:
        return jsonify({"ok": False, "message": f"Status nao permitido. Use um de: {', '.join(status_permitidos)}"}), 400
    
    try:
        client = get_smartsheet_client()
        sheet_sol = get_sheet_solicitacoes(client)
        cols_sol = get_col_map(sheet_sol)
        
        row_id_int = int(row_id)
        col_status = col_id_by_name(sheet_sol, "STATUS")

        if not col_status:
            return jsonify({"ok": False, "message": "Coluna STATUS nao encontrada"}), 500
        
        row_update = smartsheet.models.Row()
        row_update.id = row_id_int
        row_update.cells = [{"column_id": col_status, "value": _canonical_status(novo_status_upper)}]
        
        client.Sheets.update_rows(ID_FOLHA_SOLICITACOES, [row_update])
        invalidate_sheet_cache(ID_FOLHA_SOLICITACOES)
        
        return jsonify({"ok": True, "message": f"Status atualizado para {novo_status_upper}"})
    except Exception as e:
        print(f"ERRO em api_dp_atualizar_status: {e}")
        return jsonify({"ok": False, "message": str(e)}), 500

# ============================================
# LICENÇA CERTARIANA (PREMIUM) — REGRAS DE FRACIONAMENTO
# ============================================

def _listar_segmentos_premium(colaborador_email: str, win_start: dt.date | None, win_end: dt.date | None,
                             exclude_row_id: int | None = None,
                             include_statuses: set[str] | None = None) -> list[dict]:
    """Lista segmentos PREMIUM (Licença Certariana) do colaborador, filtrando pela janela.

    Retorna dicts: {row_id, ini, fim, dias, status}
    - Considera apenas linhas que NÃO são ajustes.
    - Determina o tipo PREMIUM via coluna 'SALDO TIPO' ou marker em OBSERVAÇÕES.
    - Se win_start/win_end forem None, não filtra por janela.
    """
    client = get_smartsheet_client()
    if not client:
        return []

    colaborador_email = safe_lower(colaborador_email)
    if not colaborador_email:
        return []

    sheet_sol = get_sheet_solicitacoes(client)
    cols = get_col_map(sheet_sol)
    colsN = _cols_norm_map(cols)

    col_colab = _col_id(colsN, "COLABORADOR")
    col_status = _col_id(colsN, "STATUS")
    col_dias = _col_id(colsN, "DIAS")
    col_solic = _col_id(colsN, "SOLICITAÇÃO", "SOLICITACAO")
    col_obs = _col_id(colsN, "OBSERVAÇÕES", "OBSERVACOES", "OBSERVACAO")
    col_tipo = _col_id(colsN, "SALDO TIPO", "TIPO SALDO", "TIPO_DE_SALDO", "TIPO DE SALDO")
    col_inicio = _col_id(colsN, "DATA INICIO", "DATA INÍCIO", "DATA INICIAL", "INICIO", "INÍCIO")
    col_fim = _col_id(colsN, "DATA FIM", "DATA FINAL", "FIM")

    out = []
    for row in sheet_sol.rows:
        try:
            if exclude_row_id is not None and int(row.id) == int(exclude_row_id):
                continue
        except Exception:
            pass

        solicit = next((c.value for c in row.cells if c.column_id == (col_solic or -1)), "") or ""
        if _is_ajuste(solicit):
            continue

        row_key = next((c.value for c in row.cells if c.column_id == (col_colab or -1)), None)
        if not row_key or safe_lower(str(row_key)) != colaborador_email:
            continue

        status = next((c.value for c in row.cells if c.column_id == (col_status or -1)), "") or ""
        st_norm = _norm_status(status)
        if include_statuses and st_norm not in include_statuses:
            continue

        dias_val = next((c.value for c in row.cells if c.column_id == (col_dias or -1)), 0) or 0
        try:
            dias = int(float(dias_val or 0))
        except Exception:
            dias = 0

        obs = next((c.value for c in row.cells if c.column_id == (col_obs or -1)), "") or ""
        explicit_tipo = next((c.value for c in row.cells if c.column_id == (col_tipo or -1)), "") or ""

        saldo_tipo = _infer_saldo_tipo(obs, explicit_tipo)
        if saldo_tipo != "PREMIUM":
            continue

        ini_val = next((c.value for c in row.cells if c.column_id == (col_inicio or -1)), None)
        fim_val = next((c.value for c in row.cells if c.column_id == (col_fim or -1)), None)

        ini = _parse_date_value(ini_val) if ini_val else None
        fim = _parse_date_value(fim_val) if fim_val else None

        # fallback: se não tiver data fim, estima por dias
        if ini and not fim and dias > 0:
            fim = ini + dt.timedelta(days=dias - 1)

        if not ini or not fim:
            continue

        if win_start and win_end:
            if not (win_start <= ini < win_end):
                continue

        out.append({
            "row_id": getattr(row, "id", None),
            "ini": ini,
            "fim": fim,
            "dias": int(dias),
            "status": str(status or ""),
            "status_norm": st_norm,
        })

    out.sort(key=lambda x: x["ini"])
    return out


def _validar_fracionamento_certariana(
    direito_total: int,
    dt_inicio: dt.date,
    dt_fim: dt.date,
    dias_novos: int,
    segmentos_existentes: list[dict],
) -> tuple[bool, str]:
    """Valida as regras de fracionamento da Licença Certariana.

    Regras (DP):
    - Até 3 períodos.
    - Cada período >= 10 dias.
    - Se forem 3 períodos: somente 3×10 (total 30).
    - Se forem 2 períodos: nenhum < 10 (ex.: 20+10, 16+14, etc.).
    - 1 período por solicitação (este endpoint), reconhecendo os períodos anteriores.
    """
    try:
        direito_total = int(direito_total or 0)
    except Exception:
        direito_total = 0

    if dias_novos < 10:
        return False, "Para Licença Certariana, o período mínimo é de 10 dias."

    if direito_total <= 0:
        return False, "Licença Certariana indisponível (direito total = 0)."

    # Não permitir sobreposição com períodos já registrados (aprovados ou reservados)
    for seg in segmentos_existentes:
        ini = seg.get("ini")
        fim = seg.get("fim")
        if not ini or not fim:
            continue
        if not (dt_fim < ini or dt_inicio > fim):
            return False, "Este período conflita (sobrepõe) com outro período de Licença Certariana já registrado."

    seg_dias = [int(s.get("dias") or 0) for s in segmentos_existentes]
    seg_count = len(seg_dias)
    used_sum = sum(seg_dias)

    if seg_count >= 3:
        return False, "Já existem 3 períodos de Licença Certariana registrados nesta janela. Não é possível adicionar outro."

    total_after = used_sum + int(dias_novos)
    if total_after > direito_total:
        return False, f"Total de dias excede o direito da Licença Certariana ({direito_total} dias) nesta janela."

    seg_after = seg_count + 1
    remaining = direito_total - total_after

    # Mínimo por período = 10, então saldo 1-9 é impossível
    if 0 < remaining < 10:
        return False, f"Este fracionamento deixaria um saldo de {remaining} dia(s), mas o mínimo por período é 10."

    # 3 períodos: somente 3×10 (assumindo direito 30)
    if seg_after == 3:
        all10 = all(d == 10 for d in (seg_dias + [dias_novos]))
        if not (direito_total == 30 and total_after == 30 and all10):
            return False, "Para utilizar 3 períodos, a Licença Certariana deve ser fracionada em 3×10 dias (total 30)."
        return True, ""

    # Se após este lançamento ainda restar saldo e ele iria virar um 3º período, exige 3×10
    if seg_after == 2 and remaining > 0:
        all10 = all(d == 10 for d in (seg_dias + [dias_novos]))
        if not (direito_total == 30 and remaining == 10 and all10):
            return False, "Para deixar um 3º período, a Licença Certariana deve seguir 3×10 (cada período com 10 dias)."
        return True, ""

    return True, ""


# ============================================
# API: SOLICITAÇÃO DE FÉRIAS
# ============================================

