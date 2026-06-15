import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    # Variáveis de ambiente (padrão Flask em maiúsculas)
    SECRET_KEY = os.getenv('SECRET_KEY', 'chave-secreta-dev')
    DATABASE_URL = os.getenv('DATABASE_URL')
    DB_SCHEMA = os.getenv('DB_SCHEMA', 'app_ferias')
    
    LDAP_HOST = os.getenv('LDAP_HOST', '')
    LDAP_BASE_DN = os.getenv('LDAP_BASE_DN', '')
    
    SMARTSHEET_SERVICE_TOKEN = os.getenv('SMARTSHEET_SERVICE_TOKEN', '')
    ID_FOLHA_COLABORADORES = os.getenv('ID_FOLHA_COLABORADORES', '')
    
    DEBUG = os.getenv('DEBUG', 'False').lower() == 'true'

    # --- PROPERTIES EM MINÚSCULAS ---
    # Isso garante compatibilidade com o código existente do seu repositório
    # que tenta acessar settings.database_url, settings.db_schema, etc.
    @property
    def database_url(self):
        return self.DATABASE_URL

    @property
    def db_schema(self):
        return self.DB_SCHEMA

    @property
    def secret_key(self):
        return self.SECRET_KEY

    @property
    def ldap_host(self):
        return self.LDAP_HOST

    @property
    def ldap_base_dn(self):
        return self.LDAP_BASE_DN

    @property
    def smartsheet_service_token(self):
        return self.SMARTSHEET_SERVICE_TOKEN

    @property
    def id_folha_colaboradores(self):
        return self.ID_FOLHA_COLABORADORES


class DevelopmentConfig(Config):
    DEBUG = True
    # Se não tiver DATABASE_URL no .env, usa SQLite local para testes
    if not Config.DATABASE_URL:
        DATABASE_URL = 'sqlite:///dev.db'


class ProductionConfig(Config):
    DEBUG = False


class TestingConfig(Config):
    TESTING = True
    DATABASE_URL = 'sqlite:///test.db'


config = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'testing': TestingConfig,
    'default': DevelopmentConfig
}

def get_settings():
    """Retorna a instância de configuração baseada no ambiente."""
    env = os.getenv('FLASK_ENV', 'production')
    if env == 'development':
        return DevelopmentConfig()
    elif env == 'testing':
        return TestingConfig()
    else:
        return ProductionConfig()
