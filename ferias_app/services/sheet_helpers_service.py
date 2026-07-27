from __future__ import annotations

import os
import time

import requests
from flask import g, session
import smartsheet

from ..config import get_settings

_SHEET_CACHE = {}
_SHEET_CACHE_TTL_SECONDS = int(os.getenv("SHEET_CACHE_TTL_SECONDS", "20"))


def _legacy_smartsheet_enabled() -> bool:
    """Bloqueia acessos antigos; a sincronização ADMIN usa serviço próprio."""
    return str(os.getenv("SMARTSHEET_LEGACY_ACCESS_ENABLED") or "").strip().lower() in {"1", "true", "yes", "sim"}


def invalidate_sheet_cache(sheet_id=None):
    try:
        if sheet_id is None:
            _SHEET_CACHE.clear()
        else:
            _SHEET_CACHE.pop(sheet_id, None)
        try:
            if sheet_id in (None, get_settings().id_folha_solicitacoes):
                if hasattr(g, "_sheet_solicitacoes"):
                    delattr(g, "_sheet_solicitacoes")
        except Exception:
            pass
    except Exception:
        pass


def get_smartsheet_client(force_user_token: bool = False):
    if not _legacy_smartsheet_enabled():
        return None
    service_token = (
        os.getenv("SMARTSHEET_SERVICE_TOKEN")
        or os.getenv("SMARTSHEET_API_TOKEN")
        or os.getenv("SMARTSHEET_ACCESS_TOKEN")
    )
    if service_token and not force_user_token:
        return smartsheet.Smartsheet(service_token)
    access_token = session.get("access_token")
    if access_token:
        return smartsheet.Smartsheet(access_token)
    return None


def get_smartsheet_token() -> str | None:
    if not _legacy_smartsheet_enabled():
        return None
    service_token = (
        os.getenv("SMARTSHEET_SERVICE_TOKEN")
        or os.getenv("SMARTSHEET_API_TOKEN")
        or os.getenv("SMARTSHEET_ACCESS_TOKEN")
    )
    if service_token:
        return service_token
    return session.get("access_token")


def add_rows_rest(sheet_id: int, rows_to_add: list, *, timeout: int = 25) -> list[int]:
    if not _legacy_smartsheet_enabled():
        raise RuntimeError("Acesso legado ao Smartsheet desativado. Use a sincronização da aba ADMIN.")
    token = get_smartsheet_token()
    if not token:
        raise RuntimeError("Token Smartsheet ausente.")

    url = f"https://api.smartsheet.com/2.0/sheets/{int(sheet_id)}/rows"
    payload_rows = []
    for row in rows_to_add or []:
        cells = []
        for cell in getattr(row, "cells", []) or []:
            cid = getattr(cell, "column_id", None)
            if not cid:
                continue
            out = {"columnId": int(cid), "value": getattr(cell, "value", None)}
            if hasattr(cell, "strict"):
                out["strict"] = bool(getattr(cell, "strict"))
            cells.append(out)
        payload_rows.append({"toBottom": True, "cells": cells})

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    for attempt in (1, 2):
        try:
            resp = requests.post(url, headers=headers, json=payload_rows, timeout=timeout)
            if resp.status_code >= 400:
                raise RuntimeError(f"Smartsheet HTTP {resp.status_code}: {resp.text[:500]}")
            data = resp.json() if resp.text else {}
            inserted = []
            for result in (data.get("result") or []):
                rid = result.get("id")
                if rid:
                    inserted.append(int(rid))
            return inserted
        except Exception:
            if attempt == 1:
                time.sleep(0.8)
                continue
            raise


def get_col_map(sheet):
    try:
        if sheet is None or not hasattr(sheet, "columns"):
            return {}
        return {col.title: col.id for col in sheet.columns}
    except Exception:
        return {}


def ensure_primary_cell(sheet, row, value):
    if not sheet or not getattr(sheet, "columns", None) or row is None:
        return
    primary_col = next((col for col in sheet.columns if getattr(col, "primary", False)), None)
    if not primary_col:
        return
    already_has = any(getattr(c, "column_id", None) == primary_col.id for c in (getattr(row, "cells", []) or []))
    if already_has:
        return
    row.cells = list(getattr(row, "cells", []) or [])
    row.cells.append(smartsheet.models.Cell({"column_id": primary_col.id, "value": value}))
