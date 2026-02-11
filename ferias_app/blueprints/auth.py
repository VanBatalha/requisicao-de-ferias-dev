from __future__ import annotations

from flask import redirect, render_template, request, session, url_for

from .base import bp
from ..logging_config import get_logger
from ..services.ldap_service import authenticate

log = get_logger(__name__)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html", error="")

    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""

    try:
        u = authenticate(username, password)

        # Sessão mínima usada pelo restante do app
        session["user"] = {
            "email": u.email or "",
            "name": u.name or username,
            "id": u.username,
            "username": u.username,
            "dn": u.dn,
            "groups": u.groups,
        }

        return redirect(url_for("ferias.home"))
    except Exception as e:
        log.info("Falha no login LDAP para '%s': %s", username, e)
        return render_template("login.html", error=str(e)), 401


@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("ferias.home"))
