"""Compat layer (Stage 3)

Este módulo existe para manter compatibilidade com imports antigos (ex.: `from ferias_app.core import ...`).

A lógica foi reorganizada em:
- ferias_app/services/*
- ferias_app/utils.py
- ferias_app/config.py

As funções de suporte remanescentes foram consolidadas em `ferias_app/services/core_support.py`.
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

from .services.smartsheet_adapter import (
    get_sheet_solicitacoes as svc_get_sheet_solicitacoes,
    get_sheet_cadastro as svc_get_sheet_cadastro,
    col_id_by_name as svc_col_id_by_name,
    invalidate_sheet_cache as svc_invalidate_sheet_cache,
)  # noqa: F401

from .services.periodo_aquisitivo_service import (
    serialize_periodo_aquisitivo_alloc as svc_serialize_periodo_aquisitivo_alloc,
)  # noqa: F401

from .services.saldo_service import (
    get_resumo_ferias as svc_get_resumo_ferias,
    distribuir_solicitacao_por_periodo as svc_distribuir_solicitacao_por_periodo,
)  # noqa: F401

from .services.solicitacao_query_service import (
    listar_solicitacoes as svc_listar_solicitacoes,
    listar_solicitacoes_equipes as svc_listar_solicitacoes_equipes,
    listar_solicitacoes_todas as svc_listar_solicitacoes_todas,
    get_ferias_mes as svc_get_ferias_mes,
)  # noqa: F401

# Compat: expõe funções de suporte remanescentes para não quebrar imports antigos
from .services.core_support import *  # noqa: F401,F403

# Import explícito de helpers "privados" usados pelos wrappers abaixo.
# Observação: nomes com "_" não entram no import *.
from .services.core_support import (  # noqa: F401
    _is_ativo,
    _listar_colaboradores_cached,
    _get_sheet_solicitacoes,
    _get_sheet_cadastro,
    _col_id_by_name as _legacy_col_id_by_name,
    _invalidate_sheet_cache as _legacy_invalidate_sheet_cache,
    _serialize_periodo_aquisitivo_alloc as _legacy_serialize_periodo_aquisitivo_alloc,
)


# ------------------------------------------------------------
# Wrappers públicos para helpers legados que começam com "_" .
# Atenção: `from module import *` não importa nomes iniciados com "_".
# Estes wrappers evitam NameError nos blueprints/templates.
# ------------------------------------------------------------

def is_colaborador_ativo(colab: dict) -> bool:
    """Alias público para `_is_ativo` (suporte interno)."""
    try:
        return _is_ativo(colab)  # type: ignore[name-defined]
    except Exception:
        return False

def listar_colaboradores_cached():
    """Alias público para `_listar_colaboradores_cached` (suporte interno)."""
    return _listar_colaboradores_cached()  # type: ignore[name-defined]


def get_sheet_solicitacoes(smartsheet_client):
    """Retorna a sheet de Solicitações (helper interno).

    Motivo: o blueprint antigo referenciava `_get_sheet_solicitacoes`, mas
    nomes iniciados com "_" não entram em `import *`.
    """
    return _get_sheet_solicitacoes(smartsheet_client)  # type: ignore[name-defined]


def get_sheet_cadastro(smartsheet_client):
    """Retorna a sheet de Cadastro (helper interno).

    Motivo: o blueprint antigo referenciava `_get_sheet_cadastro`, mas
    nomes iniciados com "_" não entram em `import *`.
    """
    return _get_sheet_cadastro(smartsheet_client)  # type: ignore[name-defined]


def col_id_by_name(sheet, *candidate_names):
    """Retorna o columnId (int) da primeira coluna cujo título bater com algum dos nomes candidatos.

    A versão refatorada evita importar helpers com prefixo '_' via 'import *'.
    Por isso expomos uma função pública e usamos ela nas blueprints.
    """

    return _legacy_col_id_by_name(sheet, *candidate_names)




def load_runtime_settings() -> dict:
    """Wrapper público para carregar configurações runtime."""
    from .services.core_support import _load_runtime_settings
    return _load_runtime_settings()


def save_runtime_settings(payload: dict) -> None:
    """Wrapper público para salvar configurações runtime."""
    from .services.core_support import _save_runtime_settings
    _save_runtime_settings(payload)


def parse_iso_date(s: str):
    """Wrapper público para parse de data ISO (YYYY-MM-DD)."""
    from .services.core_support import _parse_iso_date
    return _parse_iso_date(s)


def serialize_periodo_aquisitivo_alloc(alloc: list[dict]) -> str:
    """Alias público para `_serialize_periodo_aquisitivo_alloc` (suporte interno).

    Necessário porque `from module import *` não expõe nomes iniciados com `_`.
    """
    return _legacy_serialize_periodo_aquisitivo_alloc(alloc)

def invalidate_sheet_cache(sheet_id: int | str) -> None:
    """Invalida o cache interno de um sheet (usado para reduzir chamadas na API)."""
    _legacy_invalidate_sheet_cache(sheet_id)


# Backward-compat: alguns trechos antigos ainda chamam pelo nome com '_'
_invalidate_sheet_cache = invalidate_sheet_cache


# ------------------------------------------------------------
# Preferir SEMPRE os serviços novos para permissões/roles,
# evitando que o import * do legado sobrescreva funções.
# Isso garante consistência entre templates (inject_user_context)
# e rotas (pages.py), especialmente após autenticação via LDAP.
# ------------------------------------------------------------

from .services.permissions_service import (  # noqa: E402
    get_user_role as _svc_get_user_role,
    get_user_type as _svc_get_user_type,
    tem_grupo as _svc_tem_grupo,
    is_gestor as _svc_is_gestor,
    get_subordinados as _svc_get_subordinados,
)

# sobrescreve os nomes exportados pelo legado (se houver)
get_user_role = _svc_get_user_role  # type: ignore[assignment]
get_user_type = _svc_get_user_type  # type: ignore[assignment]
tem_grupo = _svc_tem_grupo  # type: ignore[assignment]
is_gestor = _svc_is_gestor  # type: ignore[assignment]
get_subordinados = _svc_get_subordinados  # type: ignore[assignment]


# serviços novos para férias/DP/smartsheet sobrescrevem o legado
get_sheet_solicitacoes = svc_get_sheet_solicitacoes  # type: ignore[assignment]
get_sheet_cadastro = svc_get_sheet_cadastro  # type: ignore[assignment]
col_id_by_name = svc_col_id_by_name  # type: ignore[assignment]
invalidate_sheet_cache = svc_invalidate_sheet_cache  # type: ignore[assignment]
serialize_periodo_aquisitivo_alloc = svc_serialize_periodo_aquisitivo_alloc  # type: ignore[assignment]
get_resumo_ferias = svc_get_resumo_ferias  # type: ignore[assignment]
distribuir_solicitacao_por_periodo = svc_distribuir_solicitacao_por_periodo  # type: ignore[assignment]
listar_solicitacoes = svc_listar_solicitacoes  # type: ignore[assignment]
listar_solicitacoes_equipes = svc_listar_solicitacoes_equipes  # type: ignore[assignment]
listar_solicitacoes_todas = svc_listar_solicitacoes_todas  # type: ignore[assignment]
get_ferias_mes = svc_get_ferias_mes  # type: ignore[assignment]

def get_user_grupos(email: str):
    """Retorna lista de grupos compatível com o legado do sistema.

    Usa SEMPRE o get_user_type do permissions_service para evitar divergência.
    """
    ut = get_user_type(email)
    if ut == "ADMIN":
        return ["Administrador"]
    if ut == "DP":
        return ["DP"]
    return ["USER"]
