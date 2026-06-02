"""Camada de compatibilidade PostgreSQL -> formato legado do app.

O app nasceu lendo dados do Smartsheet. Esta camada devolve os mesmos formatos
esperados pelas telas/servicos, mas usando as tabelas PostgreSQL.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..config import get_settings
from ..logging_config import get_logger
from ..utils import safe_lower
from ..models import Colaborador, ColaboradorComplemento, Solicitacao
from .postgres_service import get_db_session
from .normalization_service import canonical_status, is_ajuste, infer_saldo_tipo, norm_status

log = get_logger(__name__)


def postgres_enabled() -> bool:
    return bool((get_settings().database_url or "").strip())


def _email_local(email: str | None) -> str:
    email = safe_lower(email or "")
    return email.split("@", 1)[0].strip() if "@" in email else email.strip()


def emails_equivalentes(a: str | None, b: str | None) -> bool:
    a = safe_lower(a or "")
    b = safe_lower(b or "")
    if not a or not b:
        return False
    if a == b:
        return True
    return _email_local(a) != "" and _email_local(a) == _email_local(b)


def _is_missing_value(value: Any) -> bool:
    """Identifica valores vazios vindos do PostgreSQL/Excel/pandas."""
    if value is None:
        return True
    try:
        # pandas.NaT/NaN chegam aqui quando dados foram importados do Excel.
        import pandas as pd  # type: ignore
        result = pd.isna(value)
        if isinstance(result, bool):
            return result
    except Exception:
        pass
    return False


def _as_dict(value: Any) -> Dict[str, Any]:
    """Converte JSON/dict do banco em dict legado.

    Em algumas importações do Excel, o campo Colaborador.raw_payload foi salvo como
    a linha inteira do Excel, contendo outro campo chamado ``raw_payload`` com o JSON
    real do Smartsheet. Este normalizador mescla esse JSON interno para que campos
    como USER TYPE, GESTOR DIRETO e GESTOR SUPERIOR continuem disponíveis mesmo em
    bases já importadas antes da correção do importador.
    """
    out: Dict[str, Any] = {}

    if isinstance(value, dict):
        out = dict(value)
    elif isinstance(value, str) and value.strip():
        try:
            data = json.loads(value)
            out = data if isinstance(data, dict) else {}
        except Exception:
            out = {}

    nested = out.get("raw_payload")
    nested_dict: Dict[str, Any] = {}
    if isinstance(nested, dict):
        nested_dict = dict(nested)
    elif isinstance(nested, str) and nested.strip() and nested.strip().lower() != "nan":
        try:
            data = json.loads(nested)
            if isinstance(data, dict):
                nested_dict = data
        except Exception:
            nested_dict = {}

    if nested_dict:
        merged = dict(out)
        # O JSON original do Smartsheet deve ter prioridade, pois contém as colunas
        # legadas com os nomes esperados pela aplicação.
        merged.update(nested_dict)
        out = merged

    return {k: v for k, v in out.items() if not _is_missing_value(v)}


def _is_ativo_value(value: Any) -> bool:
    st = str(value or "").strip().upper()
    if not st:
        return True
    return st in {"ATIVO", "ACTIVE", "1", "SIM", "YES", "TRUE", "OK"}


def is_colaborador_ativo_legacy(colab: Dict[str, Any]) -> bool:
    return _is_ativo_value(colab.get("STATUS") or colab.get("status") or colab.get("ativo_no_app"))


def _parse_date(value: Any) -> Optional[dt.date]:
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(text[:19], fmt).date()
        except Exception:
            pass
    try:
        # Datas vindas do Excel podem ser números seriais.
        serial = float(text)
        return (dt.date(1899, 12, 30) + dt.timedelta(days=int(serial)))
    except Exception:
        return None


def _formatar_data_br(value: Any) -> str:
    d = _parse_date(value)
    return d.strftime("%d/%m/%Y") if d else ""


def _coerce_date(value: Any) -> Optional[dt.date]:
    """Converte datas vindas do PostgreSQL/JSON em dt.date para os templates."""
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    text = str(value).strip()
    if not text or text.lower() in {"none", "null", "nan", "nat"}:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return dt.datetime.strptime(text[:19], fmt).date()
        except Exception:
            pass
    return _parse_date(text)


def _periodo_label(numero: int, inicio: Optional[dt.date], fim: Optional[dt.date], fallback: str = "") -> str:
    if inicio and fim:
        return f"Período {numero} — {inicio.strftime('%d/%m/%Y')} a {fim.strftime('%d/%m/%Y')}"
    return fallback or f"Período {numero}"


def _row_identity_filter(query, email: str):
    email = safe_lower(email or "")
    local = _email_local(email)
    if not email:
        return query.filter(False)
    try:
        from sqlalchemy import func, or_
        return query.filter(or_(Colaborador.email == email, func.split_part(Colaborador.email, '@', 1) == local))
    except Exception:
        return query.filter(Colaborador.email == email)


def get_colaborador_model(email: str) -> Optional[Colaborador]:
    session = get_db_session()
    email = safe_lower(email or "")
    if not email:
        return None
    colab = session.query(Colaborador).filter(Colaborador.email == email).first()
    if colab:
        return colab
    local = _email_local(email)
    if not local:
        return None
    try:
        from sqlalchemy import func
        matches = session.query(Colaborador).filter(func.split_part(Colaborador.email, '@', 1) == local).all()
    except Exception:
        matches = [c for c in session.query(Colaborador).all() if _email_local(c.email) == local]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        active = [c for c in matches if _is_ativo_value(c.status)]
        return active[0] if active else matches[0]
    return None


def colaborador_to_legacy(colab: Colaborador) -> Dict[str, Any]:
    raw = _as_dict(getattr(colab, "raw_payload", None))
    comp = getattr(colab, "complemento", None)
    out: Dict[str, Any] = dict(raw)
    email = safe_lower(getattr(colab, "email", "") or raw.get("EMAIL DA EMPRESA") or raw.get("EMAIL") or "")
    nome = getattr(colab, "nome_completo", None) or raw.get("NOME COMPLETO") or raw.get("NOME") or email
    status = getattr(colab, "status", None) or raw.get("STATUS") or "ATIVO"
    regime = getattr(colab, "regime", None) or raw.get("REGIME DE CONTRATACAO") or raw.get("REGIME DE CONTRATAÇÃO") or raw.get("REGIME") or ""
    adm = getattr(colab, "data_admissao", None) or raw.get("DATA DE ADMISSAO") or raw.get("DATA DE ADMISSÃO")
    dias = getattr(colab, "dias_direito", None)
    if dias is None:
        dias = raw.get("DIAS DE DIREITO") or raw.get("DIAS DIREITO") or 0

    gestor_direto = None
    gestor_superior = None
    user_type = None
    ativo_no_app = True
    if comp:
        gestor_direto = comp.gestor_direto_email
        gestor_superior = comp.gestor_superior_email
        user_type = comp.user_type
        ativo_no_app = comp.ativo_no_app

    gestor_direto = safe_lower(gestor_direto or raw.get("GESTOR DIRETO") or raw.get("GESTOR") or "")
    gestor_superior = safe_lower(gestor_superior or raw.get("GESTOR SUPERIOR") or "")
    user_type = str(user_type or raw.get("USER TYPE") or "USER").strip().upper() or "USER"

    out.update({
        "id": getattr(colab, "id", None),
        "email": email,
        "nome": nome,
        "status": status,
        "setor": getattr(colab, "setor", None) or raw.get("SETOR") or raw.get("DEPARTAMENTO") or "",
        "cargo": getattr(colab, "cargo", None) or raw.get("CARGO") or raw.get("FUNÇÃO") or raw.get("FUNCAO") or "",
        "regime": regime,
        "dias_direito": dias,
        "ativo_no_app": bool(ativo_no_app),
        "user_type": user_type,
        "gestor_direto_email": gestor_direto,
        "gestor_superior_email": gestor_superior,
        "EMAIL DA EMPRESA": email,
        "NOME COMPLETO": nome,
        "STATUS": status,
        "SETOR": getattr(colab, "setor", None) or raw.get("SETOR") or raw.get("DEPARTAMENTO") or "",
        "CARGO": getattr(colab, "cargo", None) or raw.get("CARGO") or raw.get("FUNÇÃO") or raw.get("FUNCAO") or "",
        "REGIME DE CONTRATACAO": regime,
        "REGIME DE CONTRATAÇÃO": regime,
        "DATA DE ADMISSAO": adm.isoformat() if isinstance(adm, dt.date) else adm,
        "DATA DE ADMISSÃO": adm.isoformat() if isinstance(adm, dt.date) else adm,
        "DIAS DE DIREITO": dias,
        "GESTOR DIRETO": gestor_direto,
        "GESTOR": gestor_direto,
        "GESTOR SUPERIOR": gestor_superior,
        "USER TYPE": user_type,
    })
    return out


def listar_colaboradores_legacy(only_ativos: Optional[bool] = None) -> List[Dict[str, Any]]:
    session = get_db_session()
    rows = session.query(Colaborador).outerjoin(ColaboradorComplemento).order_by(Colaborador.nome_completo).all()
    out = [colaborador_to_legacy(c) for c in rows]
    if only_ativos is True:
        out = [c for c in out if is_colaborador_ativo_legacy(c)]
    return out


def get_colaborador_legacy(email: str) -> Optional[Dict[str, Any]]:
    colab = get_colaborador_model(email)
    return colaborador_to_legacy(colab) if colab else None


def listar_emails_colaboradores_postgres(only_ativos: bool = True) -> List[str]:
    out = []
    seen = set()
    for c in listar_colaboradores_legacy(only_ativos=only_ativos):
        email = safe_lower(c.get("EMAIL DA EMPRESA") or c.get("email") or "")
        if email and email not in seen:
            seen.add(email)
            out.append(email)
    return sorted(out)


def get_user_type_postgres(email: str) -> str:
    row = get_colaborador_legacy(email)
    ut = str((row or {}).get("USER TYPE") or (row or {}).get("user_type") or "USER").strip().upper()
    if ut in {"ADMIN", "ADMINISTRADOR"}:
        return "ADMIN"
    if ut in {"DP", "RH"}:
        return "DP"
    return "USER"


def subordinados_do_gestor_postgres(gestor_email: str, only_ativos: bool = True) -> List[Dict[str, Any]]:
    gestor_email = safe_lower(gestor_email or "")
    if not gestor_email:
        return []
    is_dp_user = get_user_type_postgres(gestor_email) == "DP"
    out: List[Dict[str, Any]] = []
    seen = set()
    for c in listar_colaboradores_legacy(only_ativos=only_ativos):
        colab_email = safe_lower(c.get("EMAIL DA EMPRESA") or c.get("email") or "")
        if not colab_email or emails_equivalentes(colab_email, gestor_email) or colab_email in seen:
            continue
        gestor_direto = c.get("GESTOR DIRETO") or c.get("GESTOR") or c.get("gestor_direto_email")
        gestor_superior = c.get("GESTOR SUPERIOR") or c.get("gestor_superior_email")
        match = False
        if is_dp_user and safe_lower(gestor_superior or "") == "dp":
            match = True
        elif gestor_superior and emails_equivalentes(gestor_superior, gestor_email):
            match = True
        elif gestor_direto and emails_equivalentes(gestor_direto, gestor_email):
            match = True
        if match:
            seen.add(colab_email)
            out.append(c)
    out.sort(key=lambda x: (str(x.get("NOME COMPLETO") or "").casefold(), str(x.get("EMAIL DA EMPRESA") or "").casefold()))
    return out


def get_admissao_postgres(email: str) -> Optional[dt.date]:
    row = get_colaborador_legacy(email) or {}
    return _parse_date(row.get("DATA DE ADMISSAO") or row.get("DATA DE ADMISSÃO") or row.get("data_admissao"))


def get_regime_postgres(email: str) -> str:
    row = get_colaborador_legacy(email) or {}
    return str(row.get("REGIME DE CONTRATACAO") or row.get("REGIME DE CONTRATAÇÃO") or row.get("regime") or "").strip()


def get_resumo_ferias_postgres(email: str) -> Dict[str, Any]:
    colab = get_colaborador_model(email)
    if not colab:
        return {
            "regular": {"direito": 0, "usados": 0, "reservados": 0, "saldo": 0, "ajustes": 0, "periodos": [], "periodo_atual": None},
            "premium": {"direito": 0, "usados": 0, "reservados": 0, "saldo": 0, "ajustes": 0},
            "total_solicitacoes": 0,
        }
    comp = colab.complemento
    regular_direito = int((getattr(comp, "saldo_regular_direito", None) if comp else None) or colab.dias_direito or 0)
    regular_usados = int((getattr(comp, "saldo_regular_usado", None) if comp else None) or 0)
    regular_reservados = int((getattr(comp, "saldo_regular_reservado", None) if comp else None) or 0)
    regular_saldo = int((getattr(comp, "saldo_regular_disponivel", None) if comp else None) if comp and comp.saldo_regular_disponivel is not None else (regular_direito - regular_usados - regular_reservados))
    premium_direito = int((getattr(comp, "saldo_premium_direito", None) if comp else None) or 0)
    premium_usados = int((getattr(comp, "saldo_premium_usado", None) if comp else None) or 0)
    premium_reservados = int((getattr(comp, "saldo_premium_reservado", None) if comp else None) or 0)
    premium_saldo = int((getattr(comp, "saldo_premium_disponivel", None) if comp else None) if comp and comp.saldo_premium_disponivel is not None else (premium_direito - premium_usados - premium_reservados))
    periodo_atual = getattr(comp, "periodo_aquisitivo_atual", None) if comp else None
    if isinstance(periodo_atual, str):
        try:
            periodo_atual = json.loads(periodo_atual)
        except Exception:
            periodo_atual = None
    periodos = []
    if isinstance(periodo_atual, dict):
        try:
            numero_periodo = int(periodo_atual.get("numero") or 1)
        except Exception:
            numero_periodo = 1
        inicio_periodo = _coerce_date(periodo_atual.get("inicio"))
        fim_periodo = _coerce_date(periodo_atual.get("fim"))
        label_periodo = _periodo_label(numero_periodo, inicio_periodo, fim_periodo, str(periodo_atual.get("label") or ""))
        periodo_atual = {
            **periodo_atual,
            "numero": numero_periodo,
            "inicio": inicio_periodo,
            "fim": fim_periodo,
            "label": label_periodo,
        }
        periodos.append({
            "numero": numero_periodo,
            "inicio": inicio_periodo,
            "fim": fim_periodo,
            "direito": max(0, regular_direito),
            "usados": max(0, regular_usados),
            "reservados": max(0, regular_reservados),
            "saldo": max(0, regular_saldo),
            "label": label_periodo,
            "atual": True,
        })
    total = int((getattr(comp, "total_solicitacoes", None) if comp else None) or 0)
    if total <= 0:
        try:
            session = get_db_session()
            total = session.query(Solicitacao).filter(Solicitacao.colaborador_email == colab.email, Solicitacao.is_ajuste.is_(False)).count()
        except Exception:
            total = 0
    return {
        "regular": {
            "direito": max(0, regular_direito),
            "usados": max(0, regular_usados),
            "reservados": max(0, regular_reservados),
            "saldo": max(0, regular_saldo),
            "ajustes": 0,
            "periodos": periodos,
            "periodo_atual": periodo_atual,
        },
        "premium": {
            "direito": max(0, premium_direito),
            "usados": max(0, premium_usados),
            "reservados": max(0, premium_reservados),
            "saldo": max(0, premium_saldo),
            "ajustes": 0,
        },
        "total_solicitacoes": int(total),
    }


def _solicitacao_tuple(sol: Solicitacao, include_email: bool = True):
    status = canonical_status(sol.status or "PENDENTE")
    saldo_tipo = (sol.saldo_tipo or infer_saldo_tipo(sol.observacoes or "", "")).upper() or "REGULAR"
    base = (
        sol.id,
        safe_lower(sol.colaborador_email or ""),
        _formatar_data_br(sol.data_inicio),
        _formatar_data_br(sol.data_fim),
        int(sol.dias or 0),
        status,
        sol.solicitacao or "",
        saldo_tipo,
        sol.observacoes or "",
    )
    if include_email:
        return base
    return (base[0], base[2], base[3], base[4], base[5], base[6], base[7], base[8])


def listar_solicitacoes_postgres(email: str):
    session = get_db_session()
    email = safe_lower(email or "")
    rows = session.query(Solicitacao).filter(Solicitacao.colaborador_email == email, Solicitacao.is_ajuste.is_(False)).order_by(Solicitacao.data_inicio.desc()).all()
    return [_solicitacao_tuple(s, include_email=False) for s in rows]


def listar_solicitacoes_equipes_postgres(emails: Sequence[str]):
    allowed = {safe_lower(e) for e in (emails or []) if safe_lower(e)}
    if not allowed:
        return []
    session = get_db_session()
    rows = session.query(Solicitacao).filter(Solicitacao.colaborador_email.in_(allowed), Solicitacao.is_ajuste.is_(False)).order_by(Solicitacao.data_inicio.desc()).all()
    return [_solicitacao_tuple(s, include_email=True) for s in rows]


def listar_solicitacoes_todas_postgres():
    session = get_db_session()
    rows = session.query(Solicitacao).filter(Solicitacao.is_ajuste.is_(False)).order_by(Solicitacao.data_inicio.desc()).all()
    return [_solicitacao_tuple(s, include_email=True) for s in rows]


def get_ferias_mes_postgres(mes, ano):
    session = get_db_session()
    try:
        mes = int(mes)
        ano = int(ano)
    except Exception:
        return []
    primeiro = dt.date(ano, mes, 1)
    ultimo = dt.date(ano + (1 if mes == 12 else 0), 1 if mes == 12 else mes + 1, 1) - dt.timedelta(days=1)
    rows = session.query(Solicitacao).filter(
        Solicitacao.is_ajuste.is_(False),
        Solicitacao.data_inicio <= ultimo,
        Solicitacao.data_fim >= primeiro,
    ).order_by(Solicitacao.data_inicio.asc()).all()
    out = []
    for sol in rows:
        colab = get_colaborador_legacy(sol.colaborador_email) or {}
        out.append({
            "row_id": sol.id,
            "email": safe_lower(sol.colaborador_email or ""),
            "nome_completo": colab.get("NOME COMPLETO") or sol.colaborador_email,
            "cargo": colab.get("CARGO") or "",
            "setor": colab.get("SETOR") or "",
            "data_inicio": _formatar_data_br(sol.data_inicio),
            "data_fim": _formatar_data_br(sol.data_fim),
            "dias": int(sol.dias or 0),
            "status": canonical_status(sol.status or "PENDENTE"),
            "solicitacao": sol.solicitacao or "-",
            "saldo_tipo": (sol.saldo_tipo or "REGULAR").upper(),
        })
    return out


def existe_solicitacao_duplicada(colaborador_email: str, tipo_solicitacao: str, saldo_tipo: str, data_inicio: dt.date, data_fim: dt.date, dias: int) -> bool:
    session = get_db_session()
    statuses_bloqueio = {"PENDENTE", "EM ANÁLISE", "EM ANALISE", "RESERVADO", "RESERVADA", "APROVADA", "APROVADO"}
    rows = session.query(Solicitacao).filter(
        Solicitacao.colaborador_email == safe_lower(colaborador_email),
        Solicitacao.is_ajuste.is_(False),
        Solicitacao.data_inicio == data_inicio,
        Solicitacao.data_fim == data_fim,
        Solicitacao.dias == int(dias or 0),
    ).all()
    alvo_tipo = str(tipo_solicitacao or "").strip().upper()
    alvo_saldo = str(saldo_tipo or "").strip().upper()
    for row in rows:
        if str(row.solicitacao or "").strip().upper() == alvo_tipo and str(row.saldo_tipo or "").strip().upper() == alvo_saldo:
            if canonical_status(row.status or "").upper() in statuses_bloqueio:
                return True
    return False
