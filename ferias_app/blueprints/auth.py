from __future__ import annotations

from flask import redirect, render_template, request, session, url_for
from .base import bp
from ..logging_config import get_logger
from ..services.ldap_service import authenticate
from ..services.postgres_service import get_session

log = get_logger(__name__)

@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html", error="")
    
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    
    try:
        # 1. Autentica no LDAP
        u = authenticate(username, password)
        
        # 2. Verifica se o email existe no banco de colaboradores
        email = (u.email or "").lower().strip()
        if not email:
            raise ValueError("Email não encontrado no LDAP.")
        
        # 3. Consulta o banco de dados
        from ..models import Colaborador
        db = get_session()
        colaborador = db.query(Colaborador).filter(
            Colaborador.email == email,
            Colaborador.status == 'Ativo'  # ⚠️ CRÍTICO: só permite login se estiver ATIVO
        ).first()
        
        if not colaborador:
            log.warning("Tentativa de login de usuário inativo ou não cadastrado: %s", email)
            raise ValueError("Usuário não autorizado. Verifique se seu email está cadastrado e ativo no sistema.")
        
        # 4. Sessão mínima usada pelo restante do app
        session["user"] = {
            "email": email,
            "name": u.name or username,
            "id": colaborador.id,  # ID agora é o número da matrícula
            "username": u.username,
            "dn": u.dn,
            "groups": u.groups,
            "matricula": colaborador.matricula,
        }
        
        return redirect(url_for("ferias.home"))
        
    except Exception as e:
        log.info("Falha no login LDAP para '%s': %s", username, e)
        return render_template("login.html", error=str(e)), 401

@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("ferias.home"))
