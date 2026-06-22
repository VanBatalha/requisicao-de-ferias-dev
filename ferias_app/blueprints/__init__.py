"""Rotas organizadas por módulos.

Apenas importar este pacote garante que todas as rotas sejam registradas no blueprint.
"""

from .base import bp  # noqa: F401

# Importa módulos para registrar rotas via decorators
from . import pages  # noqa: F401
from . import auth  # noqa: F401
from . import admin_api  # noqa: F401
from . import dp_api  # noqa: F401
from . import solicitacoes_api  # noqa: F401
