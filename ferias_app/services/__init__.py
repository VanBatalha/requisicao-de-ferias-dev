# ferias_app/services/__init__.py
"""Serviços do aplicativo de gestão de férias."""

# Não criar o app aqui! Isso causa loop de importação.
# O app é criado em ferias_app/__init__.py

# Apenas exponha os serviços que outros módulos precisam
from .postgres_service import get_session, init_db, postgres_enabled
from .normalization_service import normalize_email, normalize_status

__all__ = [
    'get_session',
    'init_db',
    'postgres_enabled',
    'normalize_email',
    'normalize_status',
]
