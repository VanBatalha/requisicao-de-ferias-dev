from __future__ import annotations

from flask import jsonify, request, session

from .base import bp
from ..core import (
    get_subordinados,
    get_user_role,
    is_gestor,
    safe_lower,
    tem_grupo,
)
from ..services.solicitacoes_service import processar_solicitacao
from ..services.relatorio_service import gerar_relatorio_lancamento


@bp.route("/api/solicitar-ferias", methods=["POST"])
def api_solicitar_ferias():
    payload, status = processar_solicitacao(request.form.to_dict(flat=True), session.get("user"))
    return jsonify(payload), status


@bp.route("/api/relatorio-lancamento", methods=["GET"])
def api_relatorio_lancamento():
    """Gera relatório de lançamentos de férias para gestor."""
    user = session.get("user")
    if not user:
        return jsonify({"ok": False, "message": "Não autenticado"}), 401
    
    user_email = safe_lower(user.get("email") or "")
    role = get_user_role(user_email)
    
    # Apenas gestores e DP podem gerar relatório
    is_dp_or_admin = role in ("DP", "admin")
    is_gestor_user = is_gestor(user_email)
    
    if not (is_dp_or_admin or is_gestor_user):
        return jsonify({"ok": False, "message": "Acesso negado"}), 403
    
    try:
        mes = request.args.get("mes", type=int)
        ano = request.args.get("ano", type=int)
        
        # Valida mês e ano
        if mes and not (1 <= mes <= 12):
            return jsonify({"ok": False, "message": "Mês inválido"}), 400
        
        if is_dp_or_admin:
            # DP pode gerar relatório para todos
            # (parâmetro opcional: email específico)
            target_email = safe_lower(request.args.get("email", "") or user_email)
            subs = get_subordinados(target_email) if target_email != user_email else []
        else:
            # Gestor vê seu próprio relatório
            target_email = user_email
            subs = get_subordinados(user_email)
        
        relatorio = gerar_relatorio_lancamento(target_email, subs or [], mes, ano)
        return jsonify(relatorio)
    
    except Exception as e:
        return jsonify({"ok": False, "message": f"Erro ao gerar relatório: {str(e)}"}), 500

