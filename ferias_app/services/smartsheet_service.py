from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any, Dict, List, Optional, Tuple

import requests
import smartsheet
from flask import g

from ..config import get_settings
from ..logging_config import get_logger

log = get_logger(__name__)


def _legacy_access_enabled() -> bool:
    return str(os.getenv("SMARTSHEET_LEGACY_ACCESS_ENABLED") or "").strip().lower() in {"1", "true", "yes", "sim"}


def _ensure_legacy_access() -> None:
    if not _legacy_access_enabled():
        raise RuntimeError("Acesso legado ao Smartsheet desativado. Use a sincronização da aba ADMIN.")

@dataclass
class SmartsheetTokens:
    access_token: str
    refresh_token: str | None = None
    expires_in: int | None = None
    token_type: str | None = None

def get_sdk_client(access_token: str) -> smartsheet.Smartsheet:
    _ensure_legacy_access()
    """Cria (ou reutiliza por request) um client do SDK."""
    if not access_token:
        raise ValueError("Access token ausente.")
    cached = getattr(g, "_smartsheet_sdk_client", None)
    if cached and getattr(g, "_smartsheet_sdk_token", None) == access_token:
        return cached

    client = smartsheet.Smartsheet(access_token)
    client.errors_as_exceptions(True)
    g._smartsheet_sdk_client = client
    g._smartsheet_sdk_token = access_token
    return client

def api_get(url: str, access_token: str) -> Dict[str, Any]:
    _ensure_legacy_access()
    r = requests.get(url, headers={"Authorization": f"Bearer {access_token}"}, timeout=30)
    r.raise_for_status()
    return r.json()

def api_post(url: str, access_token: str, data: Dict[str, Any], headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    _ensure_legacy_access()
    h = {}
    if access_token:
        h["Authorization"] = f"Bearer {access_token}"
    if headers:
        h.update(headers)
    r = requests.post(url, headers=h, data=data, timeout=30)
    r.raise_for_status()
    return r.json()

def get_sheet(access_token: str, sheet_id: int):
    client = get_sdk_client(access_token)
    return client.Sheets.get_sheet(sheet_id)

def add_rows(access_token: str, sheet_id: int, rows: list):
    client = get_sdk_client(access_token)
    return client.Sheets.add_rows(sheet_id, rows)

def update_rows(access_token: str, sheet_id: int, rows: list):
    client = get_sdk_client(access_token)
    return client.Sheets.update_rows(sheet_id, rows)

def columns_map(sheet) -> Dict[str, int]:
    """Mapeia título -> columnId (case-insensitive)."""
    m: Dict[str, int] = {}
    for c in sheet.columns:
        title = (c.title or "").strip()
        if title:
            m[title.upper()] = c.id
    return m
