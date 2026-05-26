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

    uri = s.ldap_uri.strip()
    is_ldaps = uri.lower().startswith("ldaps://")
    wants_starttls = (not is_ldaps) and _bool(getattr(s, "ldap_starttls", "false"))

    tls = None
    if is_ldaps or wants_starttls:
        # Para LDAPS ou STARTTLS: configura validação de certificado
        tls = Tls(validate=ssl.CERT_REQUIRED if _bool(s.ldap_verify_cert) else ssl.CERT_NONE)

    # IMPORTANTE: use_ssl deve ser True apenas para ldaps://
    return Server(uri, get_info=ALL, use_ssl=is_ldaps, tls=tls)


def _service_bind() -> Connection:
    """Bind com conta técnica (recomendado) ou bind anônimo se não informado.

    Para ldap:// (389), alguns servidores exigem STARTTLS. Ative com LDAP_STARTTLS=true.
    """
    s = get_settings()
    server = _build_server()

    try:
        if s.ldap_bind_dn:
            conn = Connection(server, user=s.ldap_bind_dn, password=s.ldap_bind_password, auto_bind=False)
            conn.open()
            if _bool(getattr(s, "ldap_starttls", "false")) and (not s.ldap_uri.strip().lower().startswith("ldaps://")):
                conn.start_tls()
            conn.bind()
            if not conn.bound:
                raise LDAPException("Bind de serviço não foi aceito (credenciais ou permissão).")
            log.info("LDAP service bind OK (user=%s)", s.ldap_bind_dn)
            return conn

        log.warning("LDAP_BIND_DN não configurado: tentando bind anônimo (pode falhar dependendo do servidor).")
        conn = Connection(server, auto_bind=False)
        conn.open()
        if _bool(getattr(s, "ldap_starttls", "false")) and (not s.ldap_uri.strip().lower().startswith("ldaps://")):
            conn.start_tls()
        conn.bind()
        return conn
    except Exception as e:
        log.exception("Falha ao conectar/bind no LDAP: %s", e)
        raise


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
            log.info('LDAP search result: ok=%s entries=%s', ok, len(conn.entries) if hasattr(conn,'entries') else 'n/a')
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

        email = _attr(s.ldap_email_attr) or _attr("mail") or _attr("userPrincipalName") or ""
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

        conn_u = Connection(server, user=bind_user, password=password, auto_bind=False)
        conn_u.open()
        if _bool(getattr(get_settings(), 'ldap_starttls', 'false')) and (not get_settings().ldap_uri.strip().lower().startswith('ldaps://')):
            conn_u.start_tls()
        conn_u.bind()
        if not conn_u.bound:
            raise LDAPException('Bind do usuário não foi aceito')
        conn_u.unbind()
        return user
    except LDAPException as e:
        log.info("Falha de autenticação LDAP para %s: %s", username, e)
        raise ValueError("Credenciais inválidas.")
