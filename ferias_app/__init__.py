from __future__ import annotations

import os

from flask import Flask, request, jsonify

from .blueprints import bp
from .config import get_settings
from .logging_config import setup_logging
from .services.auth_service import inject_user_context


def create_app() -> Flask:
    """Cria e configura a aplicação Flask (app factory)."""

    setup_logging()
    settings = get_settings()

    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    app = Flask(
        __name__,
        template_folder=os.path.join(base_dir, "templates"),
    )

    app.secret_key = settings.secret_key

    # Inicializa o banco de dados PostgreSQL
    from .services.postgres_service import init_db
    init_db()

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
        # Loga traceback completo no Render. Em produção, o Flask pode entregar
        # InternalServerError como HTTPException; neste caso o erro real fica em
        # original_exception. Sem isso o log mostra só a página 500 genérica.
        from werkzeug.exceptions import HTTPException, InternalServerError  # noqa: E402
        original = getattr(e, "original_exception", None)
        erro_real = original or e
        try:
            log.exception(
                "Unhandled exception path=%s method=%s erro=%s",
                getattr(request, "path", ""),
                getattr(request, "method", ""),
                erro_real,
                exc_info=erro_real,
            )
        except Exception:
            pass

        # Se for HTTPException que não seja 500, deixa o Flask responder normalmente.
        if isinstance(e, HTTPException) and not isinstance(e, InternalServerError):
            return e

        try:
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "message": "Internal Server Error", "detail": str(erro_real)}), 500
        except Exception:
            pass

        return (
            "Internal Server Error - veja o traceback completo nos logs do Render. "
            f"Rota: {getattr(request, 'path', '')}. Erro: {erro_real}",
            500,
        )

    return app
