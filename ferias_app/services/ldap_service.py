from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import re
import ssl

from ldap3 import ALL, Connection, Server, Tls
from ldap3.core.exceptions import LDAPException
from ldap3.utils.conv import escape_filter_chars

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


def _build_server() -> Server:
    s = get_settings()
    if not s.ldap_uri:
        raise ValueError("LDAP_URI não configurado.")

    tls = None
    if s.ldap_uri.strip().lower().startswith("ldaps://"):
        # Por padrão valida certificado. Para laboratório, permitir desabilitar.
        tls = Tls(validate=ssl.CERT_REQUIRED if _bool(s.ldap_verify_cert) else ssl.CERT_NONE)

    return Server(s.ldap_uri, get_info=ALL, tls=tls)


def _service_bind() -> Connection:
    """Bind com conta técnica (recomendado) ou bind anônimo se não informado.

    Observação: em muitos ADs o bind anônimo não consegue fazer search,
    então é altamente recomendado configurar LDAP_BIND_DN e LDAP_BIND_PASSWORD.
    """
    s = get_settings()
    server = _build_server()

    if s.ldap_bind_dn:
        return Connection(server, user=s.ldap_bind_dn, password=s.ldap_bind_password, auto_bind=True)

    log.warning("LDAP_BIND_DN não configurado: tentando bind anônimo (pode falhar em AD).")
    return Connection(server, auto_bind=True)


def _candidate_filters(username: str) -> List[str]:
    """Retorna filtros LDAP a tentar, em ordem."""
    s = get_settings()
    u = escape_filter_chars(username)

    filters: List[str] = []

    raw = (getattr(s, "ldap_user_filters", "") or "").strip()
    if raw:
        parts = [p.strip() for p in re.split(r"[;,]+", raw) if p.strip()]
        filters.extend([p.format(username=u) for p in parts])

    single = (s.ldap_user_filter or "").strip() or "(sAMAccountName={username})"
    filters.append(single.format(username=u))

    # se parecer email e não vieram filtros extras, tenta automaticamente
    if "@" in username and not raw:
        filters.insert(0, f"(mail={u})")
        filters.insert(0, f"(userPrincipalName={u})")

    # remove duplicados preservando ordem
    seen = set()
    out: List[str] = []
    for f in filters:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def find_user(username: str) -> Optional[LdapUser]:
    """Localiza o usuário no LDAP e retorna DN + atributos."""
    s = get_settings()
    if not s.ldap_base_dn:
        raise ValueError("LDAP_BASE_DN não configurado.")

    attrs = list({s.ldap_email_attr, s.ldap_name_attr, s.ldap_memberof_attr, "cn", "uid", "sAMAccountName", "userPrincipalName", "mail"})
    filters = _candidate_filters(username)

    conn = _service_bind()
    try:
        entry = None
        for f in filters:
            log.info("LDAP search: base_dn=%s filter=%s", s.ldap_base_dn, f)
            ok = conn.search(search_base=s.ldap_base_dn, search_filter=f, attributes=attrs, size_limit=2)
            if ok and conn.entries:
                entry = conn.entries[0]
                break

        if entry is None:
            return None

        dn = entry.entry_dn

        def _attr(name: str) -> str:
            try:
                v = entry[name].value
                return str(v) if v is not None else ""
            except Exception:
                return ""

        email = _attr(s.ldap_email_attr) or _attr("mail") or ""
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
    2) tenta bind com o DN do usuário (ou formato configurado) e a senha informada
    """
    if not username or not password:
        raise ValueError("Usuário e senha são obrigatórios.")

    user = find_user(username)
    if not user:
        raise ValueError("Usuário não encontrado no LDAP.")

    server = _build_server()
    try:
        bind_user = user.dn
        fmt = (getattr(get_settings(), "ldap_auth_bind_format", "") or "").strip()
        if fmt:
            bind_user = fmt.format(username=username)

        Connection(server, user=bind_user, password=password, auto_bind=True).unbind()
        return user
    except LDAPException as e:
        log.info("Falha de autenticação LDAP para %s: %s", username, e)
        raise ValueError("Credenciais inválidas.")
