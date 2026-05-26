from __future__ import annotations

import json

import smartsheet
from flask import g, jsonify, redirect, request, session, url_for

from .base import bp
from ..core import (
    ID_FOLHA_CADASTRO,
    load_runtime_settings,
    save_runtime_settings,
    col_id_by_name,
    get_sheet_cadastro,
    get_smartsheet_client,
    get_user_grupos,
    get_user_type,
    invalidate_sheet_cache,
    is_colaborador_ativo,
    listar_colaboradores,
    safe_lower,
    tem_grupo,
)
from ..rules import build_request_window_override_settings


def _current_or_original_admin_email() -> str:
    current = (session.get("user") or {}).get("email") or ""
    original = (session.get("impersonator_user") or {}).get("email") or ""
    return original or current


def _is_admin_session() -> bool:
    return bool(_current_or_original_admin_email()) and tem_grupo(_current_or_original_admin_email(), "Administrador")


@bp.route("/api/admin/simular-usuario", methods=["POST"])
def api_admin_simular_usuario():
    user = session.get("user")
    if not user or not _is_admin_session():
        return jsonify({"ok": False, "message": "Acesso negado"}), 403
    if session.get("is_impersonating"):
        return jsonify({"ok": False, "message": "Encerre a simulação atual antes de iniciar outra."}), 400

    payload = request.get_json(silent=True) or request.form
    email = safe_lower(payload.get("email") or "")
    if not email:
        return jsonify({"ok": False, "message": "E-mail é obrigatório."}), 400

    try:
        colaboradores = listar_colaboradores()
        colab = next((c for c in colaboradores if safe_lower(c.get("EMAIL DA EMPRESA") or "") == email), None)
        if not colab:
            return jsonify({"ok": False, "message": "Usuário não encontrado no cadastro."}), 404
        nome = colab.get("NOME COMPLETO") or colab.get("NOME") or email
    except Exception as e:
        return jsonify({"ok": False, "message": f"Erro ao localizar usuário para simulação: {e}"}), 500

    session["impersonator_user"] = dict(user)
    session["is_impersonating"] = True
    session["user"] = {
        "email": email,
        "name": nome,
        "id": f"simulado:{email}",
        "username": email,
        "groups": [],
        "simulated_by": user.get("email"),
    }
    return jsonify({"ok": True, "message": f"Simulação iniciada como {nome}.", "redirect_url": url_for("ferias.ferias")})


@bp.route("/admin/parar-simulacao", methods=["GET", "POST"])
def admin_parar_simulacao():
    original = session.get("impersonator_user")
    if original:
        session["user"] = original
        session.pop("impersonator_user", None)
        session.pop("is_impersonating", None)
    return redirect(url_for("ferias.painel_admin"))


@bp.route("/api/admin/listar-usuarios")
def api_admin_listar_usuarios():
    user = session.get("user")
    if not user or not _is_admin_session():
        return jsonify({"ok": False, "message": "Acesso negado"}), 403

    q = (request.args.get("q") or "").strip().lower()

    try:
        colaboradores = listar_colaboradores()

        # filtra somente Status = Ativo
        colaboradores = [c for c in colaboradores if is_colaborador_ativo(c)]

        # se não houver busca, não devolve tudo (evita listar milhares)
        if q:
            def _match(c):
                nome = str(c.get("NOME COMPLETO") or "").lower()
                email = str(c.get("EMAIL DA EMPRESA") or "").lower()
                return q in nome or q in email
            colaboradores = [c for c in colaboradores if _match(c)]
        else:
            colaboradores = []

        # limita retorno
        colaboradores = colaboradores[:10]

        # Adiciona grupos de cada usuário
        for colab in colaboradores:
            email = colab.get("EMAIL DA EMPRESA")
            colab["user_type"] = get_user_type(email)
            colab["grupos"] = get_user_grupos(email)

        return jsonify({"ok": True, "usuarios": colaboradores})
    except Exception as e:
        print(f"ERRO em api_admin_listar_usuarios: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "message": f"Erro ao buscar usuários: {str(e)}"}), 500

@bp.route("/api/admin/atualizar-grupos", methods=["POST"])
def api_admin_atualizar_grupos():
    user = session.get("user")
    if not user or not _is_admin_session():
        return jsonify({"ok": False, "message": "Acesso negado"}), 403

    payload = request.get_json(silent=True) or request.form

    email = (payload.get("email") or "").strip()
    if not email:
        return jsonify({"ok": False, "message": "Email é obrigatório"}), 400

    grupos_in = payload.get("grupos", [])
    grupos = []

    try:
        if isinstance(grupos_in, str):
            grupos = json.loads(grupos_in) if grupos_in.strip() else []
        elif isinstance(grupos_in, list):
            grupos = grupos_in
        else:
            grupos = []
    except Exception:
        return jsonify({"ok": False, "message": "Formato de grupos inválido"}), 400

    # normaliza: só aceita grupos conhecidos
    grupos_validos = []
    for g_ in grupos:
        g_ = str(g_).strip()
        if g_ in ("Administrador", "DP", "RH", "USER"):
            # compatibilidade: RH equivale a DP
            if g_ == "RH":
                g_ = "DP"
            grupos_validos.append(g_)

    # converte grupos -> USER TYPE (ADMIN | DP | USER)
    if "Administrador" in grupos_validos:
        user_type_value = "ADMIN"
        grupos_validos = ["Administrador"]
    elif "DP" in grupos_validos:
        user_type_value = "DP"
        grupos_validos = ["DP"]
    else:
        user_type_value = "USER"
        grupos_validos = ["USER"]

    client = get_smartsheet_client()
    if not client:
        return jsonify({"ok": False, "message": "Não autenticado"}), 401

    try:
        sheet = get_sheet_cadastro(client)
        if not sheet:
            return jsonify({"ok": False, "message": "Folha de cadastro não encontrada"}), 404

        col_email = col_id_by_name(sheet, "EMAIL DA EMPRESA", "EMAIL")
        col_user_type = col_id_by_name(sheet, "USER TYPE", "USER_TYPE", "USERTYPE", "TIPO USUARIO", "TIPO DE USUARIO")

        if not col_email:
            return jsonify({"ok": False, "message": "Coluna 'EMAIL DA EMPRESA' não encontrada no cadastro."}), 400
        if not col_user_type:
            return jsonify({"ok": False, "message": "Coluna 'USER TYPE' não encontrada no cadastro. Crie a coluna 'USER TYPE' na planilha 3609445264215940."}), 400

        email_lower = safe_lower(email)
        row_id = None
        for row in sheet.rows:
            row_email = None
            for cell in row.cells:
                if cell.column_id == col_email:
                    row_email = safe_lower(cell.value)
                    break
            if row_email == email_lower:
                row_id = row.id
                break

        if not row_id:
            return jsonify({"ok": False, "message": "Usuário não encontrado na planilha de cadastro."}), 404

        row_update = smartsheet.models.Row()
        row_update.id = row_id
        row_update.cells = [{"column_id": col_user_type, "value": user_type_value}]
        client.Sheets.update_rows(ID_FOLHA_CADASTRO, [row_update])

        # invalida caches para refletir imediatamente
        invalidate_sheet_cache(ID_FOLHA_CADASTRO)
        try:
            if hasattr(g, "_colaboradores_list_cache"):
                delattr(g, "_colaboradores_list_cache")
            if hasattr(g, "_cadastro_colaboradores"):
                delattr(g, "_cadastro_colaboradores")
            if hasattr(g, "_user_type_map"):
                delattr(g, "_user_type_map")
            if hasattr(g, "_sheet_cadastro"):
                delattr(g, "_sheet_cadastro")
        except Exception:
            pass

        print(f"[ADMIN] USER TYPE atualizado para {email_lower}: {user_type_value}")
        return jsonify({"ok": True, "message": f"Permissão de {email} atualizada com sucesso", "grupos": grupos_validos, "user_type": user_type_value})

    except Exception as e:
        print(f"ERRO em api_admin_atualizar_grupos: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "message": f"Erro ao atualizar permissão: {str(e)}"}), 500


# ============================================
# API: ADMIN - LIBERAÇÃO EXCEPCIONAL DAS REGRAS DE PERÍODO
# ============================================

@bp.route("/api/admin/same-month", methods=["GET"])
def api_admin_get_same_month():
    user = session.get("user")
    if not user or not _is_admin_session():
        return jsonify({"ok": False, "message": "Acesso negado"}), 403

    cfg = build_request_window_override_settings(load_runtime_settings().get("same_month", {}) or {})
    return jsonify({"ok": True, "same_month": cfg})


@bp.route("/api/admin/same-month", methods=["POST"])
def api_admin_set_same_month():
    user = session.get("user")
    if not user or not _is_admin_session():
        return jsonify({"ok": False, "message": "Acesso negado"}), 403

    payload = request.get_json(silent=True) or {}
    settings = load_runtime_settings()
    settings["same_month"] = build_request_window_override_settings(payload)
    save_runtime_settings(settings)

    return jsonify({"ok": True, "same_month": settings["same_month"]})

# ============================================
# API: dp - COLABORADORES (Planilha 360944526 - COLABORADORES (Planilha 3609445264215940)
# ============================================

