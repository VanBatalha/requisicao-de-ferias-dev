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

    return app
