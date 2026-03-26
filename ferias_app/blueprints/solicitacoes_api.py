from __future__ import annotations

from flask import jsonify, request, session

from .base import bp
from ..services.solicitacoes_service import processar_solicitacao


@bp.route("/api/solicitar-ferias", methods=["POST"])
def api_solicitar_ferias():
    payload, status = processar_solicitacao(request.form.to_dict(flat=True), session.get("user"))
    return jsonify(payload), status
