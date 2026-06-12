# ferias_app/__init__.py
from flask import Flask
from .config import config


def create_app(config_name='default'):
    """Cria e configura a aplicação Flask."""
    app = Flask(__name__)
    app.config.from_object(config[config_name])
    
    # Inicializa extensões
    from .services.postgres_service import init_db
    init_db()
    
    # Registra blueprints
    from .blueprints import bp
    app.register_blueprint(bp)
    
    return app
