from __future__ import annotations

from flask import redirect, render_template, request, session, url_for
from sqlalchemy import func
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
        # get_session() é um context manager. Na V13 ele estava sendo usado como
        # se fosse uma Session direta, gerando o erro:
        # '_GeneratorContextManager' object has no attribute 'query'.
        from ..models import Colaborador
        with get_session() as db:
            colaborador = db.query(Colaborador).filter(
                func.lower(Colaborador.email) == email,
                func.upper(func.coalesce(Colaborador.status, 'ATIVO')).in_(['ATIVO', 'ACTIVE'])
            ).first()
            
            if not colaborador:
                log.warning("Tentativa de login de usuário inativo ou não cadastrado: %s", email)
                raise ValueError("Usuário não autorizado. Verifique se seu email está cadastrado e ativo no sistema.")
            
            colaborador_id = colaborador.id
            colaborador_matricula = colaborador.matricula
        
        # 4. Sessão mínima usada pelo restante do app
        session["user"] = {
            "email": email,
            "name": u.name or username,
            "id": colaborador_id,  # ID interno/número da matrícula
            "username": u.username,
            "dn": u.dn,
            "groups": u.groups,
            "matricula": colaborador_matricula,
        }
        
        return redirect(url_for("ferias.home"))
        
    except Exception as e:
        log.info("Falha no login LDAP para '%s': %s", username, e)
        return render_template("login.html", error=str(e)), 401

@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("ferias.home"))
