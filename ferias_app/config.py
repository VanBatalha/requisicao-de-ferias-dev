# ferias_app/config.py
"""Configurações da aplicação."""
from __future__ import annotations

import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    """Configuração base da aplicação."""
    SECRET_KEY = os.getenv('SECRET_KEY', 'dev-secret-key-change-in-production')
    
    # Banco de dados
    DATABASE_URL = os.getenv('DATABASE_URL')
    DB_SCHEMA = os.getenv('DB_SCHEMA', 'app_ferias')
    
    # LDAP
    LDAP_HOST = os.getenv('LDAP_HOST', '')
    LDAP_BASE_DN = os.getenv('LDAP_BASE_DN', '')
    
    # Smartsheet
    SMARTSHEET_SERVICE_TOKEN = os.getenv('SMARTSHEET_SERVICE_TOKEN', '')
    ID_FOLHA_COLABORADORES = os.getenv('ID_FOLHA_COLABORADORES', '')
    
    # Debug
    DEBUG = os.getenv('DEBUG', 'False').lower() == 'true'
    
    @staticmethod
    def init_app(app):
        pass


class DevelopmentConfig(Config):
    """Configuração para desenvolvimento."""
    DEBUG = True
    if not Config.DATABASE_URL:
        DATABASE_URL = 'sqlite:///dev.db'


class ProductionConfig(Config):
    """Configuração para produção."""
    DEBUG = False


class TestingConfig(Config):
    """Configuração para testes."""
    TESTING = True
    DATABASE_URL = 'sqlite:///test.db'


config = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'testing': TestingConfig,
    'default': DevelopmentConfig
}


def get_settings():
    """Retorna as configurações da aplicação baseado no ambiente.
    
    Esta função é usada por serviços (como postgres_service) que precisam 
    acessar as configurações fora do contexto do Flask app.
    
    Returns:
        Config: Instância da classe de configuração apropriada
    """
    env = os.getenv('FLASK_ENV', 'production')
    
    if env == 'development':
        return DevelopmentConfig()
    elif env == 'testing':
        return TestingConfig()
    else:
        return ProductionConfig()