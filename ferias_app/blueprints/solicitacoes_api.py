from __future__ import annotations

from flask import Blueprint, jsonify, request, session
from datetime import date
import math

import smartsheet

from ferias_app.legacy.core_legacy import (
    SHEET_ID_SOLICITACOES,
    get_smartsheet_client,
    obter_email_usuario_logado,
    obter_permissoes_usuario,
    parse_date,
    dias_entre,
)

bp = Blueprint("solicitacoes_api", __name__)


# -------------------------
# Helpers
# -------------------------

def _cell_value(row, col_id):
    # Smartsheet SDK: cell.value is the raw value, display_value is formatted.
    for c in getattr(row, "cells", []) or []:
        if c.column_id == col_id:
            v = c.value
            if v is None:
                v = getattr(c, "display_value", None)
            return v
    return None


def _iter_sheet_rows(sheet_id: int, page_size: int = 500):
    """Itera páginas do Smartsheet sem carregar a folha inteira na memória."""
    client = get_smartsheet_client()
    page = 1
    while True:
        sheet = client.Sheets.get_sheet(sheet_id, page_size=page_size, page=page)
        rows = list(getattr(sheet, "rows", []) or [])
        if not rows:
            break
        yield sheet, rows
        # Se veio menos que page_size, acabou.
        if len(rows) < page_size:
            break
        page += 1


def _get_column_ids(sheet):
    cols = {}
    for col in getattr(sheet, "columns", []) or []:
        title = (col.title or "").strip().upper()
        cols[title] = col.id
    return cols


def _fetch_existing_premium_periods(colaborador_email: str):
    """Busca períodos PREMIUM já registrados para o colaborador.

    Retorna uma lista de dicts: {dias:int, inicio:date|None, fim:date|None, status:str|None, solicitacao:str|None}
    """
    colaborador_email = (colaborador_email or "").strip().lower()
    out = []

    for sheet, rows in _iter_sheet_rows(SHEET_ID_SOLICITACOES):
        col_ids = _get_column_ids(sheet)

        cid_colab = col_ids.get("COLABORADOR")
        cid_saldo = col_ids.get("SALDO TIPO")
        cid_dias = col_ids.get("DIAS")
        cid_inicio = col_ids.get("DATA INICIO")
        cid_fim = col_ids.get("DATA FIM")
        cid_status = col_ids.get("STATUS")
        cid_sol = col_ids.get("SOLICITAÇÃO") or col_ids.get("SOLICITACAO")

        if not (cid_colab and cid_saldo and cid_dias):
            # Se a folha não tiver as colunas mínimas, não tem como validar.
            return []

        for r in rows:
            colab = _cell_value(r, cid_colab)
            if not colab or str(colab).strip().lower() != colaborador_email:
                continue

            saldo = _cell_value(r, cid_saldo)
            if not saldo or str(saldo).strip().upper() != "PREMIUM":
                continue

            # Ignora ajustes (não entram na regra de fracionamento do benefício)
            sol = _cell_value(r, cid_sol) if cid_sol else None
            if sol and str(sol).strip().upper().startswith("AJUSTE"):
                continue

            status = _cell_value(r, cid_status) if cid_status else None
            if status and str(status).strip().upper() in {"REPROVADA", "CANCELADA"}:
                continue

            dias_raw = _cell_value(r, cid_dias)
            try:
                dias_int = int(float(str(dias_raw).replace(",", ".")))
            except Exception:
                continue

            if dias_int <= 0:
                continue

            ini = _cell_value(r, cid_inicio) if cid_inicio else None
            fim = _cell_value(r, cid_fim) if cid_fim else None
            try:
                ini_d = parse_date(str(ini)) if ini else None
            except Exception:
                ini_d = None
            try:
                fim_d = parse_date(str(fim)) if fim else None
            except Exception:
                fim_d = None

            out.append(
                {
                    "dias": dias_int,
                    "inicio": ini_d,
                    "fim": fim_d,
                    "status": str(status).strip() if status is not None else None,
                    "solicitacao": str(sol).strip() if sol is not None else None,
                }
            )

    return out


def _validar_fracionamento_certariana(colaborador_email: str, dias_novo: int, dias_existentes=None):
    """Regras do DP para Licença Certariana (PREMIUM):
    - total do benefício = 30 dias
    - até 3 períodos
    - mínimo 10 dias por período
    - se forem 3 períodos: deve ser 3x10 (total 30)
    - não pode deixar "resto" entre 1 e 9 dias (pois forçaria um período <10)
    """
    direito_total = 30

    if dias_existentes is None:
        existentes = _fetch_existing_premium_periods(colaborador_email)
        dias_existentes = [p["dias"] for p in existentes]
    else:
        dias_existentes = list(dias_existentes)

    if dias_novo < 10:
        return False, "Na Licença Certariana, cada período deve ter no mínimo 10 dias."

    total_usado = sum(dias_existentes) + dias_novo
    qtd_periodos = len(dias_existentes) + 1

    if qtd_periodos > 3:
        return False, "Na Licença Certariana, é permitido no máximo 3 períodos."

    if total_usado > direito_total:
        return False, f"Somatório excede {direito_total} dias da Licença Certariana (total solicitado: {total_usado})."

    restante = direito_total - total_usado
    if 1 <= restante <= 9:
        return (
            False,
            f"Fracionamento inválido: restariam {restante} dias (o mínimo por período é 10).",
        )

    if qtd_periodos == 3:
        all_days = dias_existentes + [dias_novo]
        if total_usado != direito_total or any(d != 10 for d in all_days):
            return False, "Com 3 períodos, a Licença Certariana deve ser 3x10 (total 30)."

    return True, f"Fracionamento OK. Saldo restante: {restante}."




def _inserir_solicitacao_smartsheet(
    gestor_email: str,
    colaborador_email: str,
    solicitacao: str,
    dt_inicio: date,
    dt_fim: date,
    saldo_tipo: str,
    dias: int,
    status: str,
    observacoes: str,
):
    client = get_smartsheet_client()
    sheet = client.Sheets.get_sheet(SHEET_ID_SOLICITACOES, include="columns")
    col_ids = _get_column_ids(sheet)

    def cid(name: str):
        return col_ids.get(name.strip().upper())

    required = [
        "GESTOR SOLICITANTE",
        "COLABORADOR",
        "SOLICITAÇÃO",
        "DATA INICIO",
        "DATA FIM",
        "SALDO TIPO",
        "DIAS",
        "STATUS",
        "OBSERVAÇÕES",
    ]
    missing = [c for c in required if cid(c) is None]
    if missing:
        raise RuntimeError(f"Planilha de solicitações sem colunas necessárias: {', '.join(missing)}")

    row = smartsheet.models.Row()
    row.to_bottom = True
    row.cells = [
        smartsheet.models.Cell({"column_id": cid("GESTOR SOLICITANTE"), "value": gestor_email}),
        smartsheet.models.Cell({"column_id": cid("COLABORADOR"), "value": colaborador_email}),
        smartsheet.models.Cell({"column_id": cid("SOLICITAÇÃO"), "value": solicitacao}),
        smartsheet.models.Cell({"column_id": cid("DATA INICIO"), "value": dt_inicio.isoformat()}),
        smartsheet.models.Cell({"column_id": cid("DATA FIM"), "value": dt_fim.isoformat()}),
        smartsheet.models.Cell({"column_id": cid("SALDO TIPO"), "value": saldo_tipo}),
        smartsheet.models.Cell({"column_id": cid("DIAS"), "value": dias}),
        smartsheet.models.Cell({"column_id": cid("STATUS"), "value": status}),
        smartsheet.models.Cell({"column_id": cid("OBSERVAÇÕES"), "value": observacoes or ""}),
    ]

    result = client.Sheets.add_rows(SHEET_ID_SOLICITACOES, [row])
    inserted = [r.id for r in (getattr(result, "result", []) or [])]
    return inserted

# -------------------------
# Routes
# -------------------------

@bp.route("/api/solicitar-ferias", methods=["POST"])
def api_solicitar_ferias():
    # Quem está logado é sempre o gestor solicitante.
    gestor_email = obter_email_usuario_logado()
    if not gestor_email:
        return jsonify({"ok": False, "message": "Usuário não autenticado."}), 401

    perm = obter_permissoes_usuario(gestor_email)
    user_type = (perm.get("user_type") or "").upper()

    # FormData enviado pelo front
    colaborador_email = (request.form.get("colaborador") or "").strip()
    tipo_solicitacao = (request.form.get("tipo") or "Gozo").strip()
    saldo_tipo = (request.form.get("saldo_tipo") or "").strip().upper()
    obs = (request.form.get("observacoes") or "").strip()

    data_inicio = (request.form.get("data_inicio") or "").strip()
    data_fim = (request.form.get("data_fim") or "").strip()

    if not colaborador_email:
        return jsonify({"ok": False, "message": "Informe o colaborador."}), 400

    if saldo_tipo not in {"REGULAR", "PREMIUM"}:
        return jsonify({"ok": False, "message": "Saldo tipo inválido (REGULAR/PREMIUM)."}), 400

    # Permissão: gestor só pode para equipe; DP/ADMIN para todos.
    if user_type not in {"ADMIN", "DP"}:
        # Se for GESTOR, precisa ser gestor direto do colaborador.
        # Função existente no legado.
        from ferias_app.legacy.core_legacy import gestor_pode_solicitar_para

        ok_team = gestor_pode_solicitar_para(gestor_email, colaborador_email)
        if not ok_team:
            return jsonify({"ok": False, "message": "Você só pode solicitar para colaboradores da sua equipe."}), 403

    # Parse datas
    try:
        dt_inicio = parse_date(data_inicio)
        dt_fim = parse_date(data_fim)
    except Exception:
        return jsonify({"ok": False, "message": "Datas inválidas."}), 400

    dias = dias_entre(dt_inicio, dt_fim)
    if dias <= 0:
        return jsonify({"ok": False, "message": "Período inválido (dias <= 0)."}), 400

    # Validação específica do PREMIUM (Licença Certariana)
    premium_existentes = None
    if saldo_tipo == "PREMIUM":
        premium_existentes = _fetch_existing_premium_periods(colaborador_email)
        dias_existentes = [p["dias"] for p in premium_existentes]

        ok, msg = _validar_fracionamento_certariana(colaborador_email, dias, dias_existentes=dias_existentes)
        if not ok:
            return jsonify({"ok": False, "message": msg}), 400

    # Gravação no Smartsheet
    try:
        inserted_ids = _inserir_solicitacao_smartsheet(
            gestor_email=gestor_email,
            colaborador_email=colaborador_email,
            solicitacao=tipo_solicitacao,
            dt_inicio=dt_inicio,
            dt_fim=dt_fim,
            saldo_tipo=saldo_tipo,
            dias=dias,
            status="PENDENTE",
            observacoes=obs,
        )
    except Exception as e:
        return jsonify({"ok": False, "message": f"Erro ao salvar solicitação: {e}"}), 500

    restante = None
    if saldo_tipo == "PREMIUM":
        restante = 30 - (sum(dias_existentes) + dias)


    return jsonify(
        {
            "ok": True,
            "message": f"Solicitação registrada ({tipo_solicitacao}) com {dias} dia(s)."
            + (f" Saldo restante: {restante}." if saldo_tipo == "PREMIUM" else ""),
            "inserted_ids": inserted_ids,
            "dias": dias,
            "saldo_tipo": saldo_tipo,
            "saldo_restante": restante,
        }
    ), 200
