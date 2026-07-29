from __future__ import annotations

from flask import Blueprint, jsonify

# Mantem o mesmo nome do blueprint ("ferias") para preservar url_for('ferias.*')
# e evitar quebra nos templates existentes.
bp = Blueprint("ferias", __name__)


@bp.get("/healthz")
def healthz():
    """Health check leve para Docker/Caddy, sem ocupar conexao PostgreSQL."""
    return jsonify({"ok": True, "service": "gestao-ferias", "build": "v58"})
