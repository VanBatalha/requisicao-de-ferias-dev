from __future__ import annotations

import json

from flask import jsonify, request, session

from .base import bp
from ..core import load_runtime_settings, save_runtime_settings
from ..rules import build_request_window_override_settings
from ..services.simulation_service import set_simulated_gestor, get_simulated_gestor, clear_simulated_gestor
from ..services.postgres_compat_service import postgres_enabled
from ..services.admin_cadastro_service import (
    buscar_colaboradores_admin,
    obter_colaborador_admin,
    atualizar_colaborador_admin,
    atualizar_user_type_por_email,
    atualizar_saldo_periodo_admin,
    excluir_saldo_periodo_admin,
    atualizar_ajuste_admin,
    excluir_ajuste_admin,
    atualizar_solicitacao_admin,
    excluir_solicitacao_admin,
)
from ..services.smartsheet_sync_service import start_sync_cadastro_background, get_sync_states

@bp.route("/api/admin/listar-usuarios")
def api_admin_listar_usuarios():
    user = _admin_required()
    if not user:
        return jsonify({"ok": False, "message": "Acesso negado"}), 403

    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"ok": True, "usuarios": []})
    try:
        rows = buscar_colaboradores_admin(q, limit=10)
        usuarios = []
        for row in rows:
            user_type = str(row.get("user_type") or "USER").strip().upper()
            grupos = ["Administrador"] if user_type == "ADMIN" else (["DP"] if user_type == "DP" else ["USER"])
            usuarios.append({
                "id": row.get("id"),
                "MATRICULA": row.get("matricula") or "",
                "MATRÍCULA": row.get("matricula") or "",
                "EMAIL DA EMPRESA": row.get("email") or "",
                "NOME COMPLETO": row.get("nome_completo") or "",
                "STATUS": row.get("status") or "",
                "Status": row.get("status") or "",
                "user_type": user_type,
                "grupos": grupos,
            })
        return jsonify({"ok": True, "usuarios": usuarios})
    except Exception as e:
        return jsonify({"ok": False, "message": f"Erro ao buscar usuários no PostgreSQL: {str(e)}"}), 500


@bp.route("/api/admin/atualizar-grupos", methods=["POST"])
def api_admin_atualizar_grupos():
    user = _admin_required()
    if not user:
        return jsonify({"ok": False, "message": "Acesso negado"}), 403

    payload = request.get_json(silent=True) or request.form
    email = (payload.get("email") or "").strip().lower()
    if not email:
        return jsonify({"ok": False, "message": "E-mail é obrigatório"}), 400

    grupos_in = payload.get("grupos", [])
    try:
        grupos = json.loads(grupos_in) if isinstance(grupos_in, str) and grupos_in.strip() else (grupos_in if isinstance(grupos_in, list) else [])
    except Exception:
        return jsonify({"ok": False, "message": "Formato de grupos inválido"}), 400

    grupos_normalizados = {str(item or "").strip().upper() for item in grupos}
    if grupos_normalizados.intersection({"ADMINISTRADOR", "ADMIN"}):
        user_type_value = "ADMIN"
        grupos_validos = ["Administrador"]
    elif grupos_normalizados.intersection({"DP", "RH"}):
        user_type_value = "DP"
        grupos_validos = ["DP"]
    else:
        user_type_value = "USER"
        grupos_validos = ["USER"]

    if not postgres_enabled():
        return jsonify({"ok": False, "message": "PostgreSQL não configurado. A atualização via Smartsheet foi desativada."}), 503
    try:
        updated = atualizar_user_type_por_email(email, user_type_value, actor_email=user.get("email") or "")
        if not updated:
            return jsonify({"ok": False, "message": "Usuário não encontrado no PostgreSQL."}), 404
        return jsonify({
            "ok": True,
            "message": f"Permissão de {email} atualizada com sucesso",
            "grupos": grupos_validos,
            "user_type": user_type_value,
        })
    except ValueError as e:
        return jsonify({"ok": False, "message": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "message": f"Erro ao atualizar permissão no PostgreSQL: {str(e)}"}), 500


# ============================================
# API: ADMIN - LIBERAÇÃO EXCEPCIONAL DAS REGRAS DE PERÍODO
# ============================================

@bp.route("/api/admin/same-month", methods=["GET"])
def api_admin_get_same_month():
    user = _admin_required()
    if not user:
        return jsonify({"ok": False, "message": "Acesso negado"}), 403

    cfg = build_request_window_override_settings(load_runtime_settings().get("same_month", {}) or {})
    return jsonify({"ok": True, "same_month": cfg})


@bp.route("/api/admin/same-month", methods=["POST"])
def api_admin_set_same_month():
    user = _admin_required()
    if not user:
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
    user = _admin_required()
    if not user:
        return jsonify({"ok": False, "message": "Acesso negado"}), 403

    payload = request.get_json(silent=True) or {}
    gestor_email = (payload.get("gestor_email") or "").strip().lower()

    if not gestor_email:
        return jsonify({"ok": False, "message": "Email do gestor é obrigatório"}), 400

    # Verifica o gestor exclusivamente no PostgreSQL.
    try:
        from sqlalchemy import func
        from ..models import Colaborador
        from ..services.postgres_service import get_db_session

        db = get_db_session()
        gestor = db.query(Colaborador.id).filter(
            func.lower(Colaborador.email) == gestor_email,
            func.upper(func.coalesce(Colaborador.status, "ATIVO")).in_(["ATIVO", "ACTIVE"]),
        ).first()
        if not gestor:
            return jsonify({"ok": False, "message": "Gestor não encontrado ou inativo"}), 404

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
    user = _admin_required()
    if not user:
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
    user = _admin_required()
    if not user:
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
    """Valida ADMIN somente pelo PostgreSQL/sessão, sem fallback Smartsheet."""
    user = session.get("user")
    if not user:
        return None
    if str(user.get("user_type") or "").strip().upper() in {"ADMIN", "ADMINISTRADOR"}:
        return user

    matricula = str(user.get("matricula") or "").strip().upper()
    if not matricula:
        return None
    try:
        from ..models import ColaboradorComplemento, PermissaoUsuario
        from ..services.postgres_service import get_db_session
        db = get_db_session()
        roles = {
            str(role or "").strip().upper()
            for (role,) in db.query(PermissaoUsuario.role).filter(
                PermissaoUsuario.colaborador_matricula == matricula
            ).all()
            if role
        }
        if roles.intersection({"ADMIN", "ADMINISTRADOR"}):
            user["user_type"] = "ADMIN"
            session["user"] = user
            return user
        comp = db.query(ColaboradorComplemento.user_type).filter(
            ColaboradorComplemento.colaborador_matricula == matricula
        ).first()
        if comp and str(comp[0] or "").strip().upper() in {"ADMIN", "ADMINISTRADOR"}:
            user["user_type"] = "ADMIN"
            session["user"] = user
            return user
    except Exception:
        return None
    return None


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


@bp.route("/api/admin/cadastro/colaborador/<int:colaborador_id>/saldo/<int:saldo_id>", methods=["POST", "PUT"])
def api_admin_cadastro_atualizar_saldo(colaborador_id: int, saldo_id: int):
    user = _admin_required()
    if not user:
        return jsonify({"ok": False, "message": "Acesso negado"}), 403
    payload = request.get_json(silent=True) or {}
    try:
        row = atualizar_saldo_periodo_admin(
            colaborador_id, saldo_id, payload, actor_email=user.get("email") or ""
        )
        return jsonify({"ok": True, "message": "Saldo atualizado com sucesso.", "colaborador": row})
    except ValueError as e:
        try:
            from ..services.postgres_service import get_db_session
            get_db_session().rollback()
        except Exception:
            pass
        return jsonify({"ok": False, "message": str(e)}), 400
    except Exception as e:
        try:
            from ..services.postgres_service import get_db_session
            get_db_session().rollback()
        except Exception:
            pass
        return jsonify({"ok": False, "message": f"Erro ao atualizar saldo: {str(e)}"}), 500


@bp.route("/api/admin/cadastro/colaborador/<int:colaborador_id>/saldo/<int:saldo_id>", methods=["DELETE"])
def api_admin_cadastro_excluir_saldo(colaborador_id: int, saldo_id: int):
    user = _admin_required()
    if not user:
        return jsonify({"ok": False, "message": "Acesso negado"}), 403
    try:
        row = excluir_saldo_periodo_admin(
            colaborador_id, saldo_id, actor_email=user.get("email") or ""
        )
        return jsonify({"ok": True, "message": "Linha de saldo excluída.", "colaborador": row})
    except ValueError as e:
        try:
            from ..services.postgres_service import get_db_session
            get_db_session().rollback()
        except Exception:
            pass
        return jsonify({"ok": False, "message": str(e)}), 400
    except Exception as e:
        try:
            from ..services.postgres_service import get_db_session
            get_db_session().rollback()
        except Exception:
            pass
        return jsonify({"ok": False, "message": f"Erro ao excluir saldo: {str(e)}"}), 500


@bp.route("/api/admin/cadastro/colaborador/<int:colaborador_id>/ajuste/<int:ajuste_id>", methods=["POST", "PUT"])
def api_admin_cadastro_atualizar_ajuste(colaborador_id: int, ajuste_id: int):
    user = _admin_required()
    if not user:
        return jsonify({"ok": False, "message": "Acesso negado"}), 403
    payload = request.get_json(silent=True) or {}
    try:
        row = atualizar_ajuste_admin(
            colaborador_id, ajuste_id, payload, actor_email=user.get("email") or ""
        )
        return jsonify({"ok": True, "message": "Ajuste atualizado e saldo recalculado.", "colaborador": row})
    except ValueError as e:
        try:
            from ..services.postgres_service import get_db_session
            get_db_session().rollback()
        except Exception:
            pass
        return jsonify({"ok": False, "message": str(e)}), 400
    except Exception as e:
        try:
            from ..services.postgres_service import get_db_session
            get_db_session().rollback()
        except Exception:
            pass
        return jsonify({"ok": False, "message": f"Erro ao atualizar ajuste: {str(e)}"}), 500


@bp.route("/api/admin/cadastro/colaborador/<int:colaborador_id>/ajuste/<int:ajuste_id>", methods=["DELETE"])
def api_admin_cadastro_excluir_ajuste(colaborador_id: int, ajuste_id: int):
    user = _admin_required()
    if not user:
        return jsonify({"ok": False, "message": "Acesso negado"}), 403
    try:
        row = excluir_ajuste_admin(
            colaborador_id, ajuste_id, actor_email=user.get("email") or ""
        )
        return jsonify({"ok": True, "message": "Ajuste excluído e efeito estornado do saldo.", "colaborador": row})
    except ValueError as e:
        try:
            from ..services.postgres_service import get_db_session
            get_db_session().rollback()
        except Exception:
            pass
        return jsonify({"ok": False, "message": str(e)}), 400
    except Exception as e:
        try:
            from ..services.postgres_service import get_db_session
            get_db_session().rollback()
        except Exception:
            pass
        return jsonify({"ok": False, "message": f"Erro ao excluir ajuste: {str(e)}"}), 500



@bp.route("/api/admin/cadastro/colaborador/<int:colaborador_id>/solicitacao/<int:solicitacao_id>", methods=["POST", "PUT"])
def api_admin_cadastro_atualizar_solicitacao(colaborador_id: int, solicitacao_id: int):
    user = _admin_required()
    if not user:
        return jsonify({"ok": False, "message": "Acesso negado"}), 403
    payload = request.get_json(silent=True) or {}
    try:
        row = atualizar_solicitacao_admin(
            colaborador_id,
            solicitacao_id,
            payload,
            actor_email=user.get("email") or "",
        )
        return jsonify({
            "ok": True,
            "message": "Solicitação atualizada e saldo conciliado.",
            "colaborador": row,
        })
    except ValueError as e:
        try:
            from ..services.postgres_service import get_db_session
            get_db_session().rollback()
        except Exception:
            pass
        return jsonify({"ok": False, "message": str(e)}), 400
    except Exception as e:
        try:
            from ..services.postgres_service import get_db_session
            get_db_session().rollback()
        except Exception:
            pass
        return jsonify({"ok": False, "message": f"Erro ao atualizar solicitação: {str(e)}"}), 500


@bp.route("/api/admin/cadastro/colaborador/<int:colaborador_id>/solicitacao/<int:solicitacao_id>", methods=["DELETE"])
def api_admin_cadastro_excluir_solicitacao(colaborador_id: int, solicitacao_id: int):
    user = _admin_required()
    if not user:
        return jsonify({"ok": False, "message": "Acesso negado"}), 403
    try:
        row = excluir_solicitacao_admin(
            colaborador_id,
            solicitacao_id,
            actor_email=user.get("email") or "",
        )
        return jsonify({
            "ok": True,
            "message": "Solicitação excluída e efeito estornado do saldo.",
            "colaborador": row,
        })
    except ValueError as e:
        try:
            from ..services.postgres_service import get_db_session
            get_db_session().rollback()
        except Exception:
            pass
        return jsonify({"ok": False, "message": str(e)}), 400
    except Exception as e:
        try:
            from ..services.postgres_service import get_db_session
            get_db_session().rollback()
        except Exception:
            pass
        return jsonify({"ok": False, "message": f"Erro ao excluir solicitação: {str(e)}"}), 500


# ============================================
# API: ADMIN - CICLOS E SALDOS (SOMENTE POSTGRESQL)
# ============================================

@bp.route("/api/admin/verificar-periodos-saldos", methods=["POST"])
def api_admin_verificar_periodos_saldos():
    user = _admin_required()
    if not user:
        return jsonify({"ok": False, "message": "Acesso negado"}), 403
    try:
        from ..services.period_accrual_service import ensure_due_periods
        result = ensure_due_periods(
            actor_email=user.get("email") or "admin",
            force=True,
            wait_for_lock=True,
        )
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "message": f"Erro ao verificar períodos e saldos: {str(e)}"}), 500


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
