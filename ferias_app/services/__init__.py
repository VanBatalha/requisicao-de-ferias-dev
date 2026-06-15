# ferias_app/services/__init__.py
"""Serviços da aplicação.

IMPORTANTE: Este arquivo NÃO deve criar o app ou importar blueprints.
O app é criado em ferias_app/__init__.py.
Este arquivo apenas expõe os serviços para uso interno.
"""
from __future__ import annotations

# Importações lazy para evitar loops de importação
__all__ = [
    'get_session',
    'init_db',
    'postgres_enabled',
]


def get_session():
    """Retorna uma sessão do banco de dados."""
    from .postgres_service import get_session as _get_session
    return _get_session()


def init_db():
    """Inicializa o banco de dados."""
    from .postgres_service import init_db as _init_db
    return _init_db()


def postgres_enabled():
    """Verifica se o PostgreSQL está habilitado."""
    from .postgres_service import postgres_enabled as _postgres_enabled
    return _postgres_enabled()