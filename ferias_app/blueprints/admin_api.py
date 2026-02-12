from __future__ import annotations

from .base import bp
from ..core import *  # noqa: F401,F403

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

    client = get_smartsheet_client()
    if not client:
        return jsonify({"ok": False, "message": "Não autenticado"}), 401

    try:
        sheet = get_sheet_cadastro(client)
        if not sheet:
            return jsonify({"ok": False, "message": "Folha de cadastro não encontrada"}), 404

        col_email = _col_id_by_name(sheet, "EMAIL DA EMPRESA", "EMAIL")
        col_user_type = _col_id_by_name(sheet, "USER TYPE", "USER_TYPE", "USERTYPE", "TIPO USUARIO", "TIPO DE USUARIO")

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
        _invalidate_sheet_cache(ID_FOLHA_CADASTRO)
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
# API: ADMIN - LIBERAÇÃO EXCEPCIONAL (MÊS VIGENTE)
# ============================================

@bp.route("/api/admin/same-month", methods=["GET"])
def api_admin_get_same_month():
    user = session.get("user")
    if not user or not tem_grupo(user.get("email"), "Administrador"):
        return jsonify({"ok": False, "message": "Acesso negado"}), 403

    cfg = _load_runtime_settings().get("same_month", {})
    return jsonify({"ok": True, "same_month": cfg})


@bp.route("/api/admin/same-month", methods=["POST"])
def api_admin_set_same_month():
    user = session.get("user")
    if not user or not tem_grupo(user.get("email"), "Administrador"):
        return jsonify({"ok": False, "message": "Acesso negado"}), 403

    payload = request.get_json(silent=True) or {}

    enabled = bool(payload.get("enabled", False))
    until_raw = (payload.get("until") or "").strip()
    # valida data (mantém string original no formato ISO)
    until_dt = _parse_iso_date(until_raw)
    until = until_dt.strftime("%Y-%m-%d") if until_dt else ""

    scope_in = payload.get("scope") or {}
    scope = {
        "all": bool(scope_in.get("all", False)),
        "gestores": bool(scope_in.get("gestores", False)),
        "groups": [str(g).strip() for g in (scope_in.get("groups") or []) if str(g).strip()],
        "users": [safe_lower(u) for u in (scope_in.get("users") or []) if safe_lower(u)],
    }

    settings = _load_runtime_settings()
    settings["same_month"] = {
        "enabled": enabled,
        "until": until,
        "scope": scope,
    }
    _save_runtime_settings(settings)

    return jsonify({"ok": True, "same_month": settings["same_month"]})

# ============================================
# API: dp - COLABORADORES (Planilha 360944526 - COLABORADORES (Planilha 3609445264215940)
# ============================================

