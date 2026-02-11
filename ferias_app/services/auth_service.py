from __future__ import annotations

import secrets
import urllib.parse
from typing import Any, Dict, Optional

from flask import session

from ..config import get_settings
from ..logging_config import get_logger
from .smartsheet_service import api_get, api_post, SmartsheetTokens

log = get_logger(__name__)

AUTH_URL = "https://app.smartsheet.com/b/authorize"
TOKEN_URL = "https://api.smartsheet.com/2.0/token"
CURRENT_USER_URL = "https://api.smartsheet.com/2.0/users/me"

def build_authorize_url() -> str:
    s = get_settings()
    state = secrets.token_urlsafe(24)
    session["oauth_state"] = state

    params = {
        "response_type": "code",
        "client_id": s.client_id,
        "scope": s.scopes,
        "state": state,
        "redirect_uri": s.redirect_uri,
    }
    return AUTH_URL + "?" + urllib.parse.urlencode(params)

def exchange_code_for_token(code: str) -> SmartsheetTokens:
    s = get_settings()
    data = {
        "grant_type": "authorization_code",
        "client_id": s.client_id,
        "client_secret": s.client_secret,
        "code": code,
        "redirect_uri": s.redirect_uri,
    }
    payload = api_post(TOKEN_URL, access_token="", data=data)  # token endpoint doesn't need bearer
    return SmartsheetTokens(
        access_token=payload.get("access_token", ""),
        refresh_token=payload.get("refresh_token"),
        expires_in=payload.get("expires_in"),
        token_type=payload.get("token_type"),
    )

def fetch_current_user(access_token: str) -> Dict[str, Any]:
    return api_get(CURRENT_USER_URL, access_token)

def login_required_user() -> Optional[Dict[str, Any]]:
    return session.get("user")

def save_session_user(user: Dict[str, Any], tokens: SmartsheetTokens) -> None:
    session["access_token"] = tokens.access_token
    if tokens.refresh_token:
        session["refresh_token"] = tokens.refresh_token
    session["user"] = {
        "email": user.get("email"),
        "name": user.get("name") or user.get("firstName") or user.get("lastName") or user.get("email"),
        "id": user.get("id"),
    }

def get_access_token() -> str:
    # Método A: token fixo (conta de serviço) configurado no ambiente.
    s = get_settings()
    if s.access_token:
        return s.access_token
    # Legado: token por sessão (OAuth Smartsheet)
    return session.get("access_token", "")

def inject_user_context():
    """Contexto global para templates."""
    user = session.get("user")
    if not user:
        return {}
    email = user.get("email") or ""
    # role e grupos são calculados no permissions_service (import lazily para evitar ciclo)
    from .permissions_service import get_user_role, get_user_type
    ut = get_user_type(email)
    role = get_user_role(email)
    grupos = ["Administrador"] if ut == "ADMIN" else (["DP"] if ut == "DP" else ["USER"])

    role_label = {
        "admin": "ADMIN",
        "DP": "DP",
        "gestor": "GESTOR",
        "user": "USUARIO",
    }.get(role, str(role).upper())

    display = user.get("name") or email or "Usuario"
    avatar_seed = (display or email or "U").strip()
    avatar = (avatar_seed[:1] or "U").upper()

    return dict(
        current_user=user,
        user_email=email,
        user_grupos=grupos,
        user_role=role,
        user_role_label=role_label,
        user_display_name=display,
        user_avatar=avatar,
        first=(display.split(" ")[0] if display else (email.split("@")[0] if "@" in email else email)),
        last=(" ".join(display.split(" ")[1:]) if display and " " in display else ""),
    )
