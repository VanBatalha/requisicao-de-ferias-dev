# ferias_app/services/__init__.py
"""Serviços do aplicativo de gestão de férias."""
"""Serviços do aplicativo de gestão de férias."""

try:
    from .postgres_service import get_session, init_db, postgres_enabled
except ImportError:
    # Fallback se postgres_service não estiver disponível
    def get_session():
        raise NotImplementedError("PostgreSQL não configurado")
    def init_db():
        pass
    def postgres_enabled():
        return False

try:
    from .normalization_service import normalize_email, normalize_status
except ImportError:
    # Fallback se normalization_service não estiver disponível
    def normalize_email(email):
        return email.strip().lower() if email else ""
    def normalize_status(status):
        return status.strip().upper() if status else ""

__all__ = [
    'get_session',
    'init_db',
    'postgres_enabled',
    'normalize_email',
    'normalize_status',
]
