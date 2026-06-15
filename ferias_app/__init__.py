# ferias_app/__init__.py
"""Inicialização da aplicação Flask."""
from __future__ import annotations

import os
from flask import Flask, request, jsonify

from .config import get_settings
from .logging_config import setup_logging, get_logger


def create_app() -> Flask:
    """Cria e configura a aplicação Flask (app factory).
    
    Returns:
        Flask: Instância configurada da aplicação
    """
    setup_logging()
    log = get_logger(__name__)
    
    settings = get_settings()
    
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    app = Flask(
        __name__,
        template_folder=os.path.join(base_dir, "templates"),
    )
    app.secret_key = settings.SECRET_KEY
    
    # Configurações do app
    app.config['DATABASE_URL'] = settings.DATABASE_URL
    app.config['DB_SCHEMA'] = settings.DB_SCHEMA
    
    # Inicializa o banco de dados PostgreSQL
    try:
        from .services.postgres_service import init_db
        init_db()
        log.info("✅ Banco de dados PostgreSQL inicializado")
    except Exception as e:
        log.warning(f"⚠️ Falha ao inicializar PostgreSQL: {e}")
    
    # Registra blueprints
    try:
        from .blueprints import bp
        app.register_blueprint(bp)
        log.info("✅ Blueprints registrados")
    except Exception as e:
        log.error(f"❌ Erro ao registrar blueprints: {e}")
    
    # Contexto global para templates
    try:
        from .services.auth_service import inject_user_context
        app.context_processor(inject_user_context)
    except Exception as e:
        log.warning(f"⚠️ Falha ao injetar contexto de usuário: {e}")
    
    @app.teardown_appcontext
    def _close_db_session(exception=None):
        """Fecha a sessão SQLAlchemy ao final de cada request."""
        try:
            from flask import g
            db_session = getattr(g, "_db_session", None)
            if db_session is not None:
                db_session.close()
                g._db_session = None
        except Exception:
            pass
    
    @app.errorhandler(Exception)
    def _handle_exception(e):
        """Handler global de exceções."""
        try:
            log.exception("Unhandled exception: %s", e)
        except Exception:
            pass
        
        from werkzeug.exceptions import HTTPException
        if isinstance(e, HTTPException):
            return e
        
        try:
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "message": "Internal Server Error", "detail": str(e)}), 500
        except Exception:
            pass
        
        return ("Internal Server Error", 500)
    
    # ============================================
    # INICIALIZAÇÃO DO SCHEDULER (AGENDADOR AUTOMÁTICO)
    # ============================================
    if not app.testing:
        try:
            from .services.scheduler_service import start_scheduler
            with app.app_context():
                start_scheduler()
            log.info("✅ Scheduler de sincronização automática inicializado com sucesso.")
        except Exception as e:
            log.warning(f"⚠️ Falha ao iniciar scheduler: {e}. Sincronização automática desabilitada.")
    
    log.info("✅ Aplicação Flask criada com sucesso")
    return app