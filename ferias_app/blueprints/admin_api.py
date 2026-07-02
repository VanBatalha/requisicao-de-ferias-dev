from __future__ import annotations

import json

import smartsheet
from flask import g, jsonify, request, session

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
from ..services.simulation_service import set_simulated_gestor, get_simulated_gestor, clear_simulated_gestor, is_in_simulation
from ..services.postgres_compat_service import postgres_enabled
from ..services.admin_cadastro_service import (
    buscar_colaboradores_admin,
    obter_colaborador_admin,
    atualizar_colaborador_admin,
    atualizar_user_type_por_email,
)
from ..services.smartsheet_sync_service import start_sync_cadastro_background, get_sync_states

@bp.route("/api/admin/listar-usuarios")
def api_admin_listar_usuarios():
    user = session.get("user")
    if not user or not tem_grupo(user.get("email"), "Administrador"):
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
    if not user or not tem_grupo(user.get("email"), "Administrador"):
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

    if postgres_enabled():
        try:
            updated = atualizar_user_type_por_email(email, user_type_value, actor_email=user.get("email") or "")
            if not updated:
                return jsonify({"ok": False, "message": "Usuário não encontrado no PostgreSQL."}), 404

            # invalida caches para refletir imediatamente
            try:
                for attr in ("_colaboradores_list_cache", "_cadastro_colaboradores", "_user_type_map", "_sheet_cadastro"):
                    if hasattr(g, attr):
                        delattr(g, attr)
            except Exception:
                pass

            print(f"[ADMIN] USER TYPE atualizado no PostgreSQL para {safe_lower(email)}: {user_type_value}")
            return jsonify({
                "ok": True,
                "message": f"Permissão de {email} atualizada com sucesso",
                "grupos": grupos_validos,
                "user_type": user_type_value,
            })
        except Exception as e:
            print(f"ERRO em api_admin_atualizar_grupos(PostgreSQL): {e}")
            import traceback
            traceback.print_exc()
            return jsonify({"ok": False, "message": f"Erro ao atualizar permissão no PostgreSQL: {str(e)}"}), 500

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
    if not user or not tem_grupo(user.get("email"), "Administrador"):
        return jsonify({"ok": False, "message": "Acesso negado"}), 403

    cfg = build_request_window_override_settings(load_runtime_settings().get("same_month", {}) or {})
    return jsonify({"ok": True, "same_month": cfg})


@bp.route("/api/admin/same-month", methods=["POST"])
def api_admin_set_same_month():
    user = session.get("user")
    if not user or not tem_grupo(user.get("email"), "Administrador"):
        return jsonify({"ok": False, "message": "Acesso negado"}), 403

    payload = request.get_json(silent=True) or {}
    settings = load_runtime_settings()
    settings["same_month"] = build_request_window_override_settings(payload)
    save_runtime_settings(settings)

    return jsonify({"ok": True, "same_month": settings["same_month"]})

# ============================================
# API: ADMIN - SIMULAÇÃO DE GESTOR
# ============================================

@bp.route("/api/admin/simular-gestor", methods=["POST"])
def api_admin_simular_gestor():
    """Inicia simulação de um gestor no painel admin."""
    user = session.get("user")
    if not user or not tem_grupo(user.get("email"), "Administrador"):
        return jsonify({"ok": False, "message": "Acesso negado"}), 403

    payload = request.get_json(silent=True) or {}
    gestor_email = (payload.get("gestor_email") or "").strip().lower()

    if not gestor_email:
        return jsonify({"ok": False, "message": "Email do gestor é obrigatório"}), 400

    # Verifica se o gestor existe e está ativo
    try:
        colaboradores = listar_colaboradores()
        gestor_existe = any(
            safe_lower(c.get("EMAIL DA EMPRESA") or "") == gestor_email and is_colaborador_ativo(c)
            for c in colaboradores
        )
        if not gestor_existe:
            return jsonify({"ok": False, "message": "Gestor não encontrado ou inativo"}), 404

        # Inicia simulação
        set_simulated_gestor(gestor_email)
        return jsonify({
            "ok": True,
            "message": f"Simulação iniciada para {gestor_email}",
            "simulated_gestor": gestor_email
        })
    except Exception as e:
        return jsonify({"ok": False, "message": f"Erro ao simular gestor: {str(e)}"}), 500


@bp.route("/api/admin/sair-simulacao", methods=["POST"])
def api_admin_sair_simulacao():
    """Encerra simulação de gestor."""
    user = session.get("user")
    if not user or not tem_grupo(user.get("email"), "Administrador"):
        return jsonify({"ok": False, "message": "Acesso negado"}), 403

    try:
        clear_simulated_gestor()
        return jsonify({
            "ok": True,
            "message": "Simulação encerrada com sucesso"
        })
    except Exception as e:
        return jsonify({"ok": False, "message": f"Erro ao encerrar simulação: {str(e)}"}), 500


@bp.route("/api/admin/status-simulacao", methods=["GET"])
def api_admin_status_simulacao():
    """Retorna status atual da simulação."""
    user = session.get("user")
    if not user or not tem_grupo(user.get("email"), "Administrador"):
        return jsonify({"ok": False, "message": "Acesso negado"}), 403

    simulated_gestor = get_simulated_gestor()
    return jsonify({
        "ok": True,
        "in_simulation": bool(simulated_gestor),
        "simulated_gestor": simulated_gestor
    })

# ============================================
# API: dp - COLABORADORES (Planilha 360944526 - COLABORADORES (Planilha 3609445264215940)
# ============================================



# ============================================
# API: ADMIN - EDIÇÃO DE CADASTRO POSTGRESQL
# ============================================

def _admin_required():
    user = session.get("user")
    if not user or not tem_grupo(user.get("email"), "Administrador"):
        return None
    return user


@bp.route("/api/admin/cadastro/colaboradores", methods=["GET"])
def api_admin_cadastro_buscar_colaboradores():
    user = _admin_required()
    if not user:
        return jsonify({"ok": False, "message": "Acesso negado"}), 403

    q = (request.args.get("q") or "").strip()
    try:
        rows = buscar_colaboradores_admin(q, limit=20)
        return jsonify({"ok": True, "colaboradores": rows})
    except Exception as e:
        return jsonify({"ok": False, "message": f"Erro ao buscar colaboradores: {str(e)}"}), 500


@bp.route("/api/admin/cadastro/colaborador/<int:colaborador_id>", methods=["GET"])
def api_admin_cadastro_obter_colaborador(colaborador_id: int):
    user = _admin_required()
    if not user:
        return jsonify({"ok": False, "message": "Acesso negado"}), 403

    try:
        row = obter_colaborador_admin(colaborador_id)
        if not row:
            return jsonify({"ok": False, "message": "Colaborador não encontrado"}), 404
        return jsonify({"ok": True, "colaborador": row})
    except Exception as e:
        return jsonify({"ok": False, "message": f"Erro ao carregar colaborador: {str(e)}"}), 500


@bp.route("/api/admin/cadastro/colaborador/<int:colaborador_id>", methods=["POST", "PUT"])
def api_admin_cadastro_atualizar_colaborador(colaborador_id: int):
    user = _admin_required()
    if not user:
        return jsonify({"ok": False, "message": "Acesso negado"}), 403

    payload = request.get_json(silent=True) or {}
    try:
        row = atualizar_colaborador_admin(colaborador_id, payload, actor_email=user.get("email") or "")
        return jsonify({"ok": True, "message": "Cadastro atualizado com sucesso", "colaborador": row})
    except ValueError as e:
        return jsonify({"ok": False, "message": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "message": f"Erro ao atualizar cadastro: {str(e)}"}), 500


# ============================================
# API: ADMIN - SINCRONIZAÇÃO SMARTSHEET -> POSTGRESQL
# ============================================

@bp.route("/api/admin/sync-cadastro", methods=["POST"])
def api_admin_sync_cadastro():
    user = _admin_required()
    if not user:
        return jsonify({"ok": False, "message": "Acesso negado"}), 403

    payload = request.get_json(silent=True) or {}
    recalculate = bool(payload.get("recalculate", False))
    # O botão do painel ADMIN sincroniza cadastro/permissões/hierarquia.
    # Solicitações e recálculo de saldos são rotinas separadas e só entram se
    # forem pedidos explicitamente no payload, evitando timeout HTTP.
    include_solicitacoes = bool(payload.get("include_solicitacoes", False))
    try:
        result = start_sync_cadastro_background(
            triggered_by="manual",
            actor_email=user.get("email") or "",
            recalculate=recalculate,
            include_solicitacoes=include_solicitacoes,
        )
        return jsonify(result), 202
    except Exception as e:
        return jsonify({"ok": False, "message": f"Erro ao iniciar sincronização: {str(e)}"}), 500


@bp.route("/api/admin/sync-state", methods=["GET"])
def api_admin_sync_state():
    user = _admin_required()
    if not user:
        return jsonify({"ok": False, "message": "Acesso negado"}), 403
    try:
        return jsonify(get_sync_states())
    except Exception as e:
        return jsonify({"ok": False, "message": f"Erro ao consultar sincronização: {str(e)}"}), 500
