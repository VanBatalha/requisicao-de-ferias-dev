from __future__ import annotations

from flask import Blueprint

# Importa todas as constantes, helpers e serviços do núcleo da aplicação
from ..core import *  # noqa: F401,F403

# Mantém o mesmo nome do blueprint ("ferias") para preservar url_for('ferias.*')
# e evitar quebra nos templates existentes.
bp = Blueprint("ferias", __name__)
