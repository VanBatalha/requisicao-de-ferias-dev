from __future__ import annotations

from flask import Blueprint

# Mantém o mesmo nome do blueprint ("ferias") para preservar url_for('ferias.*')
# e evitar quebra nos templates existentes.
bp = Blueprint("ferias", __name__)
