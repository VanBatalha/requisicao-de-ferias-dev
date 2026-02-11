"""Compat layer (Stage 3)

Este módulo existe para manter compatibilidade com imports antigos (ex.: `from ferias_app.core import ...`).

A lógica foi reorganizada em:
- ferias_app/services/*
- ferias_app/utils.py
- ferias_app/config.py

O legado completo permanece em `ferias_app/legacy/core_legacy.py` como fallback.
"""

from __future__ import annotations

# Novos módulos (preferidos)
from .config import get_settings, Settings  # noqa: F401
from .utils import *  # noqa: F401,F403

from .services.auth_service import (
    build_authorize_url,
    exchange_code_for_token,
    fetch_current_user,
    save_session_user,
    get_access_token,
    inject_user_context,
)  # noqa: F401

from .services.cadastro_service import (
    listar_colaboradores,
    get_user_type as get_user_type_from_sheet,
    subordinados_do_gestor,
)  # noqa: F401

from .services.permissions_service import (
    get_user_role,
    get_user_type,
    tem_grupo,
    is_gestor,
    get_subordinados,
)  # noqa: F401

from .services.solicitacoes_service import (
    criar_solicitacao_padrao,
    validar_licenca_cerariana,
)  # noqa: F401

# Fallback: expõe tudo do legado para não quebrar pontos ainda não migrados
from .legacy.core_legacy import *  # noqa: F401,F403
