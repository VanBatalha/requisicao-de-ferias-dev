from __future__ import annotations

import os

from flask import Flask

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

    # Rotas (Blueprint)
    app.register_blueprint(bp)

    # Contexto global para templates
    app.context_processor(inject_user_context)


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
        return ("Internal Server Error", 500)

    return app
