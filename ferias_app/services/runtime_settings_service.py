from __future__ import annotations

import datetime as dt
import json
import os

RUNTIME_SETTINGS_PATH = os.getenv(
    "RUNTIME_SETTINGS_PATH",
    "/tmp/requisicao_ferias_runtime_settings.json",
)

DEFAULT_RUNTIME_SETTINGS = {
    "same_month": {
        "enabled": False,
        "until": "",
        "cutoff_day": 21,
        "scope": {
            "all": False,
            "gestores": False,
            "groups": [],
            "users": [],
        },
    }
}


def _deep_merge_dict(base: dict, extra: dict) -> dict:
    out = json.loads(json.dumps(base or {}))
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge_dict(out.get(key) or {}, value)
        else:
            out[key] = value
    return out


def load_runtime_settings() -> dict:
    data = {}
    try:
        if os.path.exists(RUNTIME_SETTINGS_PATH):
            with open(RUNTIME_SETTINGS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
    except Exception:
        data = {}

    return _deep_merge_dict(DEFAULT_RUNTIME_SETTINGS, data or {})


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


def same_month_override_allowed(requester_email: str) -> bool:
    # Compatibilidade com o nome antigo.
    from ..rules import request_window_override_allowed

    return request_window_override_allowed(requester_email)
