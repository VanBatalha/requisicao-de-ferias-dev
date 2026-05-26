from __future__ import annotations

from flask import redirect, render_template, request, session, url_for

from .base import bp
from ..logging_config import get_logger
from ..services.ldap_service import authenticate
from ..services.auth_service import get_access_token
from ..services.cadastro_service import canonical_email_for
from ..services.identity_service import normalize_email_identity

log = get_logger(__name__)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html", error="")

    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""

    try:
        u = authenticate(username, password)

        ldap_email = normalize_email_identity(u.email or "")
        canonical_email = ldap_email
        try:
            token = get_access_token()
            if token:
                canonical_email = canonical_email_for(token, ldap_email, u.username, username) or ldap_email
        except Exception as canon_err:
            log.warning("Não foi possível canonicalizar usuário LDAP no cadastro: %s", canon_err)

        # Sessão mínima usada pelo restante do app.
        # `email` fica canônico em relação ao Smartsheet; o e-mail original do LDAP
        # é mantido para diagnóstico.
        session["user"] = {
            "email": canonical_email or ldap_email or "",
            "ldap_email": ldap_email or "",
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
