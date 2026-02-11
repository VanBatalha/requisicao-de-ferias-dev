from __future__ import annotations

from flask import redirect, request, session, url_for

from .base import bp
from ..logging_config import get_logger
from ..services.auth_service import (
    build_authorize_url,
    exchange_code_for_token,
    fetch_current_user,
    save_session_user,
)

log = get_logger(__name__)

@bp.route("/login")
def login():
    return redirect(build_authorize_url())

@bp.route("/callback")
def callback():
    error = request.args.get("error")
    if error:
        return f"Erro na autorização: {error}", 400

    code = request.args.get("code")
    state = request.args.get("state")
    if not code:
        return "Código ausente no callback.", 400
    if not state or state != session.get("oauth_state"):
        return "State inválido. Possível CSRF.", 400

    try:
        tokens = exchange_code_for_token(code)
        if not tokens.access_token:
            return "Token inválido (access_token vazio).", 400
        user = fetch_current_user(tokens.access_token)
        save_session_user(user, tokens)
        return redirect(url_for("ferias.home"))
    except Exception as e:
        log.exception("Erro no callback OAuth")
        return f"Erro ao autenticar: {e}", 500

@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("ferias.home"))
