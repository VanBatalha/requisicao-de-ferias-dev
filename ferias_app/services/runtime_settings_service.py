from __future__ import annotations

import datetime as dt
import json
import os

from ..utils import safe_lower
from .permissions_service import get_user_type, is_gestor

RUNTIME_SETTINGS_PATH = os.getenv(
    "RUNTIME_SETTINGS_PATH",
    "/tmp/requisicao_ferias_runtime_settings.json",
)

DEFAULT_RUNTIME_SETTINGS = {
    "same_month": {
        "enabled": True,
        "until": "2026-02-11",
        "scope": {
            "all": True,
            "gestores": False,
            "groups": [],
            "users": [],
        },
    }
}


def load_runtime_settings() -> dict:
    data = {}
    try:
        if os.path.exists(RUNTIME_SETTINGS_PATH):
            with open(RUNTIME_SETTINGS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
    except Exception:
        data = {}

    out = json.loads(json.dumps(DEFAULT_RUNTIME_SETTINGS))
    try:
        for key, value in (data or {}).items():
            if isinstance(value, dict) and isinstance(out.get(key), dict):
                out[key].update(value)
            else:
                out[key] = value
    except Exception:
        pass
    return out


def save_runtime_settings(payload: dict) -> None:
    with open(RUNTIME_SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(payload or {}, f, ensure_ascii=False, indent=2)


def parse_iso_date(value: str) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.datetime.strptime(str(value).strip()[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _user_groups_for_scope(email: str) -> set[str]:
    user_type = get_user_type(email)
    if user_type == "ADMIN":
        return {"Administrador"}
    if user_type == "DP":
        return {"DP"}
    return {"USER"}


def same_month_override_allowed(requester_email: str) -> bool:
    requester_email = safe_lower(requester_email)
    if not requester_email:
        return False

    user_type = get_user_type(requester_email)
    if user_type in {"ADMIN", "DP"}:
        return True

    cfg = load_runtime_settings().get("same_month", {}) or {}
    if not bool(cfg.get("enabled", False)):
        return False

    until = parse_iso_date(cfg.get("until") or "")
    if until and dt.date.today() > until:
        return False

    scope = cfg.get("scope") or {}
    if bool(scope.get("all", False)):
        return True
    if bool(scope.get("gestores", False)) and is_gestor(requester_email):
        return True

    allowed_users = {safe_lower(u) for u in (scope.get("users") or []) if safe_lower(u)}
    if requester_email in allowed_users:
        return True

    allowed_groups = {str(g).strip() for g in (scope.get("groups") or []) if str(g).strip()}
    if allowed_groups and _user_groups_for_scope(requester_email).intersection(allowed_groups):
        return True

    return False
