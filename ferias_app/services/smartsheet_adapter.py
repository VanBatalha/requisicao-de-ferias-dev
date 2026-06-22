from __future__ import annotations

"""Adaptadores de acesso ao Smartsheet e helpers de planilha.

Este módulo concentra wrappers públicos para helpers de baixo nível ainda
originados do legado, reduzindo a necessidade de blueprints importarem nomes
privados diretamente.
"""

from ..services.core_support import (
    _col_id_by_name,
    _get_sheet_cadastro,
    _get_sheet_solicitacoes,
    _invalidate_sheet_cache,
)


def get_sheet_solicitacoes(client=None, *, force_refresh: bool = False):
    return _get_sheet_solicitacoes(client, force_refresh=force_refresh)



def get_sheet_cadastro(client):
    return _get_sheet_cadastro(client)



def col_id_by_name(sheet, *candidate_names: str):
    return _col_id_by_name(sheet, *candidate_names)



def invalidate_sheet_cache(sheet_id=None):
    _invalidate_sheet_cache(sheet_id)
