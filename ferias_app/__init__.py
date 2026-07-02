from __future__ import annotations

import os

from flask import Flask, request, jsonify

from .blueprints import bp
from .config import get_settings
from .logging_config import setup_logging
from .services.auth_service import inject_user_context


def create_app(run_db_migrations: bool = False) -> Flask:
    """Cria e configura a aplicação Flask (app factory).

    No Web Service, run_db_migrations deve ficar False.
    Scripts manuais podem chamar create_app(run_db_migrations=True).
    """

    setup_logging()
    settings = get_settings()

    try:
        from .logging_config import get_logger as _get_logger
        _get_logger(__name__).info("Gestao Ferias build V40 carregado")
    except Exception:
        pass

    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    app = Flask(
        __name__,
        template_folder=os.path.join(base_dir, "templates"),
    )

    app.secret_key = settings.secret_key

    # Inicializa o banco de dados PostgreSQL
    from .services.postgres_service import init_db
    init_db(run_migrations=run_db_migrations)

    # Rotas (Blueprint)
    app.register_blueprint(bp)

    # Contexto global para templates
    app.context_processor(inject_user_context)

    @app.teardown_appcontext
    def _close_db_session(exception=None):  # noqa: ANN001
        # Fecha a sessão SQLAlchemy ao final de cada request.
        # Isso evita cache de objetos entre requisições e devolve a conexão ao pool.
        try:
            from flask import g
            db_session = getattr(g, "_db_session", None)
            if db_session is not None:
                db_session.close()
                g._db_session = None
        except Exception:
            pass


    # Log de exceções (ajuda debug no Render)
    from .logging_config import get_logger  # noqa: E402
    log = get_logger(__name__)

    @app.errorhandler(Exception)
    def _handle_exception(e):  # noqa: ANN001
        # Loga traceback completo no Render
        try:
            log.exception("Unhandled exception: %s", e)
        except Exception:
            pass

        # Se for HTTPException (ex.: 404/403), deixa o Flask responder normalmente
        from werkzeug.exceptions import HTTPException  # noqa: E402
        if isinstance(e, HTTPException):
            return e

        # Para erros 500, retorna uma página simples (evita loops)
                # Para rotas de API, devolve JSON para facilitar debug no front-end
        try:
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "message": "Internal Server Error", "detail": str(e)}), 500
        except Exception:
            pass

        # Para erros 500 em páginas HTML, retorna texto simples (evita loops)
        return ("Internal Server Error", 500)

    return app
