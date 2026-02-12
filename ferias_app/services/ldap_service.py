from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import ssl

from ldap3 import Connection, Server, Tls, ALL
from ldap3.core.exceptions import LDAPException

from ..config import get_settings
from ..logging_config import get_logger

log = get_logger(__name__)


@dataclass
class LdapUser:
    dn: str
    username: str
    email: str
    name: str
    groups: List[str]
    raw: Dict[str, Any]


def _bool(v: str) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "y", "on")


def _build_server():
    s = get_settings()
    if not s.ldap_uri:
        raise ValueError("LDAP_URI não configurado.")

    tls = None
    if s.ldap_uri.strip().lower().startswith("ldaps://"):
        # Por padrão valida certificado. Para laboratório, permitir desabilitar.
        if _bool(s.ldap_verify_cert):
            tls = Tls(validate=ssl.CERT_REQUIRED)
        else:
            tls = Tls(validate=ssl.CERT_NONE)

    return Server(s.ldap_uri, get_info=ALL, tls=tls)


def _service_bind() -> Connection:
    """Bind com conta técnica (recomendado) ou bind anônimo se não informado."""
    s = get_settings()
    server = _build_server()

    if s.ldap_bind_dn:
        conn = Connection(server, user=s.ldap_bind_dn, password=s.ldap_bind_password, auto_bind=True)
        return conn

    # fallback: anonymous bind
    conn = Connection(server, auto_bind=True)
    return conn


def find_user(username: str) -> Optional[LdapUser]:
    """Localiza o usuário no LDAP e retorna DN + atributos."""
    s = get_settings()
    if not s.ldap_base_dn:
        raise ValueError("LDAP_BASE_DN não configurado.")

    user_filter = (s.ldap_user_filter or "").format(username=username)
    attrs = list({s.ldap_email_attr, s.ldap_name_attr, s.ldap_memberof_attr, "cn", "uid", "sAMAccountName"})

    conn = _service_bind()
    try:
        ok = conn.search(search_base=s.ldap_base_dn, search_filter=user_filter, attributes=attrs, size_limit=2)
        if not ok or not conn.entries:
            return None

        entry = conn.entries[0]
        dn = entry.entry_dn

        def _attr(name: str) -> str:
            try:
                v = entry[name].value
                return str(v) if v is not None else ""
            except Exception:
                return ""

        email = _attr(s.ldap_email_attr) or ""
        name = _attr(s.ldap_name_attr) or _attr("cn") or username

        groups: List[str] = []
        try:
            mv = entry[s.ldap_memberof_attr].values  # type: ignore[attr-defined]
            for g in list(mv or []):
                groups.append(str(g))
        except Exception:
            groups = []

        raw = entry.entry_attributes_as_dict

        return LdapUser(dn=dn, username=username, email=email, name=name, groups=groups, raw=raw)
    finally:
        try:
            conn.unbind()
        except Exception:
            pass


def authenticate(username: str, password: str) -> LdapUser:
    """Valida usuário/senha:

    1) busca DN com conta técnica
    2) tenta bind com o DN do usuário e a senha informada
    """
    if not username or not password:
        raise ValueError("Usuário e senha são obrigatórios.")

    user = find_user(username)
    if not user:
        raise ValueError("Usuário não encontrado no LDAP.")

    server = _build_server()
    try:
        # bind como o usuário valida a senha
        Connection(server, user=user.dn, password=password, auto_bind=True).unbind()
        return user
    except LDAPException as e:
        log.info("Falha de autenticação LDAP para %s: %s", username, e)
        raise ValueError("Credenciais inválidas.")
