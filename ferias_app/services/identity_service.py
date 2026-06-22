from __future__ import annotations

"""Helpers de identidade usados para comparar usuários entre LDAP e Smartsheet.

A aplicação recebe o usuário autenticado pelo LDAP, mas a relação gestor ->
colaborador fica no Smartsheet. Pequenas diferenças de formato, domínio ou
campo LDAP não podem quebrar a autorização de um gestor. Por isso, todo ponto
que compara e-mails deve passar por este módulo.
"""

import re
import unicodedata

_EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.IGNORECASE)


def _strip_accents(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    return "".join(ch for ch in value if not unicodedata.combining(ch))


def normalize_identity(value: object) -> str:
    """Normaliza uma identidade livre para comparação.

    Aceita valores como:
    - email@empresa.com
    - Nome <email@empresa.com>
    - mailto:email@empresa.com
    - sAMAccountName / username
    """
    if value is None:
        return ""

    text = str(value).strip().lower()
    if not text:
        return ""

    # Células do Smartsheet às vezes vêm como "Nome <email@dominio>".
    match = _EMAIL_RE.search(text)
    if match:
        return match.group(0).strip().lower()

    text = text.replace("mailto:", "").strip()
    text = text.strip("<>;,")
    text = re.split(r"[;,\s]+", text, maxsplit=1)[0].strip()
    return text.lower()


def normalize_email_identity(value: object) -> str:
    """Alias semântico para campos onde esperamos e-mail/usuário."""
    return normalize_identity(value)


def email_local_part(value: object) -> str:
    """Retorna uma chave estável para comparar LDAP x Smartsheet.

    Se houver domínio, usa a parte antes do @. Se vier apenas usuário, usa o
    próprio valor normalizado. A chave sem acentos evita diferenças ocasionais
    em identificadores digitados manualmente.
    """
    norm = normalize_email_identity(value)
    if not norm:
        return ""
    if "@" in norm:
        norm = norm.split("@", 1)[0]
    norm = _strip_accents(norm)
    norm = re.sub(r"[^a-z0-9._%+\-]", "", norm)
    return norm


def emails_equivalentes(a: object, b: object) -> bool:
    """Compara e-mails/usuários de forma tolerante, mas previsível.

    Primeiro tenta match exato. Depois compara a parte local/username, o que
    cobre a troca Smartsheet OAuth -> LDAP quando o domínio do e-mail muda.
    """
    na = normalize_email_identity(a)
    nb = normalize_email_identity(b)
    if not na or not nb:
        return False
    if na == nb:
        return True

    la = email_local_part(na)
    lb = email_local_part(nb)
    return bool(la and lb and la == lb)


def any_email_equivalente(value: object, candidates: list[object] | tuple[object, ...] | set[object]) -> bool:
    return any(emails_equivalentes(value, candidate) for candidate in (candidates or []))
