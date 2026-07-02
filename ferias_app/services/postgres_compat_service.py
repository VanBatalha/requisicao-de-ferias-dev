"""Camada de compatibilidade PostgreSQL -> formato legado do app.

O app nasceu lendo dados do Smartsheet. Esta camada devolve os mesmos formatos
esperados pelas telas/servicos, mas usando as tabelas PostgreSQL.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import func, or_
from ..config import get_settings
from ..logging_config import get_logger
from ..utils import safe_lower
from ..models import Colaborador, ColaboradorComplemento, Solicitacao, PermissaoUsuario, HierarquiaGestao, PeriodoAquisitivo, SaldoPeriodo, SaldoPeriodoNovo
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


def _status_rank_colaborador(colab: Colaborador) -> int:
    """Prioriza cadastros ativos quando o mesmo e-mail aparece em mais de uma matrícula."""
    return 2 if _is_ativo_value(getattr(colab, "status", None)) else 0


def _choose_preferred_colaborador(rows: list[Colaborador]) -> Optional[Colaborador]:
    """Escolhe o cadastro ativo mais provável entre duplicidades por e-mail/local-part."""
    if not rows:
        return None
    rows_sorted = sorted(
        rows,
        key=lambda c: (
            _status_rank_colaborador(c),
            1 if getattr(c, "email", None) else 0,
            int(getattr(c, "id", 0) or 0),
        ),
        reverse=True,
    )
    return rows_sorted[0]


def _active_query_filter(query):
    """Aplica filtro de status ativo em consultas de usuário por nome/e-mail."""
    from sqlalchemy import func
    return query.filter(func.upper(func.coalesce(Colaborador.status, "ATIVO")).in_(["ATIVO", "ACTIVE"]))


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



def get_colaborador_model_by_matricula(matricula: str) -> Optional[Colaborador]:
    session = get_db_session()
    matricula = str(matricula or "").strip().upper()
    if not matricula:
        return None
    return session.query(Colaborador).filter(func.upper(Colaborador.matricula) == matricula).first()


def _identidade_gestor_para_email(valor: str | None) -> str:
    valor = str(valor or "").strip()
    if not valor:
        return ""
    if valor.lower() in {"dp", "gestor"}:
        return valor.lower()
    if "@" in valor:
        return safe_lower(valor)
    colab = get_colaborador_model_by_matricula(valor)
    return safe_lower(colab.email if colab else valor)


def _identidade_gestor_para_matricula(valor: str | None) -> str:
    valor = str(valor or "").strip()
    if not valor:
        return ""
    if valor.lower() in {"dp", "gestor"}:
        return valor.upper()
    if "@" in valor:
        colab = get_colaborador_model(valor)
        return (colab.matricula if colab else valor).upper()
    return valor.upper()

def get_colaborador_model(email: str) -> Optional[Colaborador]:
    """Localiza colaborador por matrícula ou e-mail.

    A matrícula é a chave operacional do app. O e-mail continua aceito apenas
    para compatibilidade com links antigos e login, sempre priorizando cadastro ATIVO.
    """
    session = get_db_session()
    ident = str(email or "").strip()
    if not ident:
        return None

    # Primeiro tenta matrícula explicitamente. Isso evita misturar vínculos quando
    # um e-mail foi reutilizado entre um cadastro inativo e uma matrícula ativa.
    if "@" not in ident:
        mat = ident.upper()
        colab = session.query(Colaborador).filter(func.upper(Colaborador.matricula) == mat).first()
        if colab:
            return colab

    email_norm = safe_lower(ident)
    exact = session.query(Colaborador).filter(func.lower(Colaborador.email) == email_norm).all()
    if exact:
        return _choose_preferred_colaborador(exact)

    local = _email_local(email_norm)
    if not local:
        return None
    try:
        matches = session.query(Colaborador).filter(func.split_part(func.lower(Colaborador.email), '@', 1) == local).all()
    except Exception:
        matches = [c for c in session.query(Colaborador).all() if _email_local(c.email) == local]
    return _choose_preferred_colaborador(matches)


def _role_for_colaborador(colab: Colaborador) -> str:
    try:
        session = get_db_session()
        rows = session.query(PermissaoUsuario).filter(PermissaoUsuario.colaborador_matricula == colab.matricula).all()
        roles = {str(r.role or '').strip().upper() for r in rows}
        if 'ADMIN' in roles or 'ADMINISTRADOR' in roles:
            return 'ADMIN'
        if 'DP' in roles or 'RH' in roles:
            return 'DP'
    except Exception:
        pass
    comp = getattr(colab, 'complemento', None)
    ut = str((getattr(comp, 'user_type', None) if comp else '') or '').strip().upper()
    if ut in {'ADMIN', 'ADMINISTRADOR'}:
        return 'ADMIN'
    if ut in {'DP', 'RH'}:
        return 'DP'
    return 'USER'


def _hierarquia_for_colaborador(colab: Colaborador) -> tuple[str, str]:
    try:
        session = get_db_session()
        h = session.query(HierarquiaGestao).filter(HierarquiaGestao.colaborador_matricula == colab.matricula).first()
        if h:
            gd_email = h.gestor_direto_email or ''
            if not gd_email and h.gestor_direto_matricula:
                gd = session.query(Colaborador).filter(Colaborador.matricula == h.gestor_direto_matricula).first()
                gd_email = gd.email if gd else ''
            gs = h.gestor_superior_email_custom or ''
            if not gs and h.gestor_superior_matricula:
                sup = session.query(Colaborador).filter(Colaborador.matricula == h.gestor_superior_matricula).first()
                gs = sup.email if sup else ''
            if not gs and str(h.gestor_superior_tipo or '').strip().upper() == 'DP':
                gs = 'dp'
            return safe_lower(gd_email), safe_lower(gs)
    except Exception:
        pass
    comp = getattr(colab, 'complemento', None)
    gd = getattr(comp, 'gestor_direto', '') if comp else ''
    gs = getattr(comp, 'gestor_superior', '') if comp else ''
    gd_email = _identidade_gestor_para_email(gd) or safe_lower(getattr(comp, 'gestor_direto_email', '') if comp else '')
    gs_email = _identidade_gestor_para_email(gs) or safe_lower(getattr(comp, 'gestor_superior_email', '') if comp else '')
    return safe_lower(gd_email), safe_lower(gs_email)


def _saldos_por_periodo(colab: Colaborador, saldo_tipo: str = 'REGULAR') -> list[dict]:
    session = get_db_session()
    saldo_tipo = (saldo_tipo or 'REGULAR').upper()
    rows = (
        session.query(SaldoPeriodoNovo)
        .filter(SaldoPeriodoNovo.colaborador_matricula == colab.matricula, SaldoPeriodoNovo.tipo_saldo == saldo_tipo)
        .order_by(SaldoPeriodoNovo.data_inicio.asc(), SaldoPeriodoNovo.periodo_numero.asc())
        .all()
    )
    out = []
    for s in rows:
        direito = int(round(float(s.saldo_inicial or 0)))
        usados = int(round(float(s.saldo_utilizado or 0)))
        reservados = int(round(float(s.saldo_reservado or 0)))
        saldo = int(round(float(s.saldo_disponivel or 0)))
        out.append({
            'id': s.id,
            'periodo_id': s.id,
            'numero': int(s.periodo_numero or 0),
            'inicio': s.data_inicio,
            'fim': s.data_fim,
            'inicio_fmt': _formatar_data_br(s.data_inicio),
            'fim_fmt': _formatar_data_br(s.data_fim),
            'direito': direito,
            'usados': usados,
            'reservados': reservados,
            'saldo': saldo,
            'label': _periodo_label(int(s.periodo_numero or 0), s.data_inicio, s.data_fim),
            'atual': bool(s.is_atual),
            'tipo_saldo': saldo_tipo,
        })
    return out


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
    gestor_direto_matricula = ""
    gestor_superior_matricula = ""
    user_type = None
    ativo_no_app = True
    if comp:
        gestor_direto = comp.gestor_direto_email
        gestor_superior = comp.gestor_superior_email
        gestor_direto_matricula = _identidade_gestor_para_matricula(getattr(comp, 'gestor_direto', '') or '')
        gestor_superior_matricula = _identidade_gestor_para_matricula(getattr(comp, 'gestor_superior', '') or '')
        user_type = comp.user_type
        ativo_no_app = comp.ativo_no_app

    h_gd, h_gs = _hierarquia_for_colaborador(colab)
    gestor_direto = safe_lower(h_gd or gestor_direto or raw.get("GESTOR DIRETO") or raw.get("GESTOR") or "")
    gestor_superior = safe_lower(h_gs or gestor_superior or raw.get("GESTOR SUPERIOR") or "")
    user_type = _role_for_colaborador(colab)

    out.update({
        "id": getattr(colab, "id", None),
        "matricula": getattr(colab, "matricula", "") or "",
        "MATRICULA": getattr(colab, "matricula", "") or "",
        "MATRÍCULA": getattr(colab, "matricula", "") or "",
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
        "gestor_direto": gestor_direto_matricula,
        "gestor_superior": gestor_superior_matricula,
        "GESTOR_DIRETO_MATRICULA": gestor_direto_matricula,
        "GESTOR_SUPERIOR_MATRICULA": gestor_superior_matricula,
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
    query = (
        session.query(Colaborador)
        .outerjoin(ColaboradorComplemento, ColaboradorComplemento.colaborador_id == Colaborador.id)
    )
    if only_ativos is True:
        query = _active_query_filter(query)
    rows = query.order_by(Colaborador.nome_completo).all()
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
    gestor_colab = get_colaborador_model(gestor_email)
    gestor_matricula = (gestor_colab.matricula or "").upper() if gestor_colab else ""
    is_dp_user = get_user_type_postgres(gestor_email) == "DP"
    out: List[Dict[str, Any]] = []
    seen = set()
    for c in listar_colaboradores_legacy(only_ativos=only_ativos):
        colab_email = safe_lower(c.get("EMAIL DA EMPRESA") or c.get("email") or "")
        colab_matricula = str(c.get("MATRICULA") or c.get("MATRÍCULA") or c.get("matricula") or "").strip().upper()
        if not colab_email or emails_equivalentes(colab_email, gestor_email) or colab_email in seen:
            continue
        gestor_direto_email = c.get("GESTOR DIRETO") or c.get("GESTOR") or c.get("gestor_direto_email")
        gestor_superior_email = c.get("GESTOR SUPERIOR") or c.get("gestor_superior_email")
        gestor_direto_matricula = str(c.get("GESTOR_DIRETO_MATRICULA") or c.get("gestor_direto") or "").strip().upper()
        gestor_superior_matricula = str(c.get("GESTOR_SUPERIOR_MATRICULA") or c.get("gestor_superior") or "").strip().upper()
        match = False
        if is_dp_user and safe_lower(gestor_superior_email or "") == "dp":
            match = True
        elif is_dp_user and gestor_superior_matricula == "DP":
            match = True
        elif gestor_matricula and gestor_superior_matricula == gestor_matricula:
            match = True
        elif gestor_superior_email and emails_equivalentes(gestor_superior_email, gestor_email):
            match = True
        elif gestor_matricula and gestor_direto_matricula == gestor_matricula:
            match = True
        elif gestor_direto_email and emails_equivalentes(gestor_direto_email, gestor_email):
            match = True
        if match:
            seen.add(colab_email)
            out.append(c)
    out.sort(key=lambda x: (str(x.get("NOME COMPLETO") or "").casefold(), str(x.get("MATRICULA") or ""), str(x.get("EMAIL DA EMPRESA") or "").casefold()))
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
    reg_periodos = _saldos_por_periodo(colab, 'REGULAR')
    prem_periodos = _saldos_por_periodo(colab, 'PREMIUM')
    def totals(periodos):
        direito = sum(int(p.get('direito') or 0) for p in periodos)
        usados = sum(int(p.get('usados') or 0) for p in periodos)
        reservados = sum(int(p.get('reservados') or 0) for p in periodos)
        saldo = sum(int(p.get('saldo') or 0) for p in periodos)
        return direito, usados, reservados, saldo
    rd, ru, rr, rs = totals(reg_periodos)
    pd, pu, pr, ps = totals(prem_periodos)
    periodo_atual = next((p for p in reg_periodos if p.get('atual')), None)
    try:
        session = get_db_session()
        total = session.query(Solicitacao).filter(Solicitacao.colaborador_matricula == colab.matricula, Solicitacao.is_ajuste.is_(False)).count()
    except Exception:
        total = 0
    return {
        "regular": {
            "direito": rd,
            "usados": ru,
            "reservados": rr,
            "saldo": rs,
            "ajustes": 0,
            "periodos": reg_periodos,
            "periodo_atual": periodo_atual,
        },
        "premium": {
            "direito": pd,
            "usados": pu,
            "reservados": pr,
            "saldo": ps,
            "ajustes": 0,
            "periodos": prem_periodos,
        },
        "total_solicitacoes": int(total),
    }


def _solicitacao_tuple(sol: Solicitacao, include_email: bool = True):
    status = canonical_status(sol.status or "PENDENTE")
    saldo_tipo = (sol.saldo_tipo or sol.tipo_ferias or infer_saldo_tipo(sol.observacoes or "", "")).upper() or "REGULAR"
    email = safe_lower(sol.colaborador_email or "")
    if not email and getattr(sol, 'colaborador', None):
        email = safe_lower(sol.colaborador.email or "")
    dias_val = sol.dias if sol.dias is not None else sol.dias_solicitados
    try:
        dias_val = int(round(float(dias_val or 0)))
    except Exception:
        dias_val = 0
    base = (
        sol.id,
        email,
        _formatar_data_br(sol.data_inicio),
        _formatar_data_br(sol.data_fim),
        dias_val,
        status,
        sol.solicitacao or sol.tipo_solicitacao or "",
        saldo_tipo,
        sol.observacoes or "",
    )
    if include_email:
        return base
    return (base[0], base[2], base[3], base[4], base[5], base[6], base[7], base[8])


def listar_solicitacoes_postgres(email: str):
    session = get_db_session()
    email = safe_lower(email or "")
    colab = get_colaborador_model(email)
    if not colab:
        return []
    try:
        rows = session.query(Solicitacao).filter(Solicitacao.colaborador_matricula == colab.matricula, Solicitacao.is_ajuste.is_(False)).order_by(Solicitacao.data_inicio.desc()).all()
        return [_solicitacao_tuple(s, include_email=False) for s in rows]
    except Exception as exc:
        log.exception("Falha ao listar solicitações no PostgreSQL para %s: %s", email, exc)
        return []


def listar_solicitacoes_equipes_postgres(emails: Sequence[str]):
    allowed = {safe_lower(e) for e in (emails or []) if safe_lower(e)}
    if not allowed:
        return []
    session = get_db_session()
    try:
        locals_allowed = {_email_local(e) for e in allowed if _email_local(e)}
        colaboradores = session.query(Colaborador).all()
        matriculas = [c.matricula for c in colaboradores if safe_lower(c.email) in allowed or _email_local(c.email) in locals_allowed]
        if not matriculas:
            return []
        rows = session.query(Solicitacao).filter(Solicitacao.colaborador_matricula.in_(matriculas), Solicitacao.is_ajuste.is_(False)).order_by(Solicitacao.data_inicio.desc()).all()
        return [_solicitacao_tuple(s, include_email=True) for s in rows]
    except Exception as exc:
        log.exception("Falha ao listar solicitações da equipe no PostgreSQL: %s", exc)
        return []


def listar_solicitacoes_todas_postgres():
    session = get_db_session()
    try:
        rows = session.query(Solicitacao).filter(Solicitacao.is_ajuste.is_(False)).order_by(Solicitacao.data_inicio.desc()).all()
        return [_solicitacao_tuple(s, include_email=True) for s in rows]
    except Exception as exc:
        log.exception("Falha ao listar todas as solicitações no PostgreSQL: %s", exc)
        return []


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
    colab = get_colaborador_model(colaborador_email)
    if not colab:
        return False
    rows = session.query(Solicitacao).filter(
        Solicitacao.colaborador_matricula == colab.matricula,
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

# ---------------------------------------------------------------------------
# V41 - consultas leves para a tela de Solicitações (/ferias)
# ---------------------------------------------------------------------------

def _num_int(value: Any) -> int:
    try:
        return int(round(float(value or 0)))
    except Exception:
        return 0


def listar_colaboradores_opcoes_ativas_postgres() -> List[Dict[str, Any]]:
    """Lista leve de colaboradores ativos para autocomplete.

    Esta função evita ``colaborador_to_legacy`` porque a tela de solicitação só
    precisa de matrícula, nome e e-mail informativo. A conversão legada calcula
    hierarquia/permissões por colaborador e fica cara no banco oficial remoto.
    """
    session = get_db_session()
    query = (
        session.query(
            Colaborador.id,
            Colaborador.matricula,
            Colaborador.email,
            Colaborador.nome_completo,
            Colaborador.status,
            ColaboradorComplemento.ativo_no_app,
        )
        .outerjoin(ColaboradorComplemento, ColaboradorComplemento.colaborador_id == Colaborador.id)
    )
    query = _active_query_filter(query)
    try:
        query = query.filter(or_(ColaboradorComplemento.ativo_no_app.is_(None), ColaboradorComplemento.ativo_no_app.is_(True)))
    except Exception:
        pass
    rows = query.order_by(func.lower(Colaborador.nome_completo).asc(), Colaborador.matricula.asc()).all()
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        mat = str(row.matricula or '').strip().upper()
        if not mat or mat in seen:
            continue
        seen.add(mat)
        nome = str(row.nome_completo or row.email or mat).strip()
        out.append({
            'id': row.id,
            'matricula': mat,
            'MATRICULA': mat,
            'MATRÍCULA': mat,
            'email': safe_lower(row.email or ''),
            'EMAIL DA EMPRESA': safe_lower(row.email or ''),
            'nome': nome,
            'NOME COMPLETO': nome,
            'status': row.status or 'ATIVO',
            'STATUS': row.status or 'ATIVO',
            'ativo_no_app': bool(row.ativo_no_app) if row.ativo_no_app is not None else True,
        })
    return out


def listar_colaboradores_opcoes_ferias_postgres(usuario_email: str, role: str | None = None) -> List[Dict[str, Any]]:
    """Lista leve de colaboradores ATIVOS dentro do escopo da tela /ferias.

    Regra operacional atual:
    - ADMIN vê todos os colaboradores ativos;
    - DP vê colaboradores marcados com gestor_direto/gestor_superior = DP e
      também vínculos diretos à sua matrícula, quando existirem;
    - Gestor vê colaboradores cuja matrícula aparece em gestor_direto ou
      gestor_superior. Quando gestor_superior = GESTOR, o responsável é o
      gestor_direto.

    A função é deliberadamente baseada em matrícula e colunas leves, sem
    conversão legada e sem consultas por colaborador.
    """
    session = get_db_session()
    usuario_email = safe_lower(usuario_email or '')
    role_norm = str(role or '').strip().upper()

    usuario_colab = get_colaborador_model(usuario_email) if usuario_email else None
    usuario_matricula = str(getattr(usuario_colab, 'matricula', '') or '').strip().upper()

    gd = func.upper(func.trim(func.coalesce(ColaboradorComplemento.gestor_direto, '')))
    gs = func.upper(func.trim(func.coalesce(ColaboradorComplemento.gestor_superior, '')))

    query = (
        session.query(
            Colaborador.id,
            Colaborador.matricula,
            Colaborador.email,
            Colaborador.nome_completo,
            Colaborador.status,
            ColaboradorComplemento.ativo_no_app,
            ColaboradorComplemento.gestor_direto,
            ColaboradorComplemento.gestor_superior,
        )
        .outerjoin(ColaboradorComplemento, ColaboradorComplemento.colaborador_id == Colaborador.id)
    )
    query = _active_query_filter(query)
    query = query.filter(or_(ColaboradorComplemento.ativo_no_app.is_(None), ColaboradorComplemento.ativo_no_app.is_(True)))

    if role_norm in {'ADMIN', 'ADMINISTRADOR'}:
        pass
    elif role_norm in {'DP', 'RH'}:
        filtros = [gd == 'DP', gs == 'DP']
        if usuario_matricula:
            filtros.extend([gd == usuario_matricula, gs == usuario_matricula])
        query = query.filter(or_(*filtros))
    else:
        if not usuario_matricula:
            return []
        query = query.filter(or_(gd == usuario_matricula, gs == usuario_matricula))

    rows = query.order_by(func.lower(Colaborador.nome_completo).asc(), Colaborador.matricula.asc()).all()
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        mat = str(row.matricula or '').strip().upper()
        if not mat or mat in seen:
            continue
        seen.add(mat)
        nome = str(row.nome_completo or row.email or mat).strip()
        out.append({
            'id': row.id,
            'matricula': mat,
            'MATRICULA': mat,
            'MATRÍCULA': mat,
            'email': safe_lower(row.email or ''),
            'EMAIL DA EMPRESA': safe_lower(row.email or ''),
            'nome': nome,
            'NOME COMPLETO': nome,
            'status': row.status or 'ATIVO',
            'STATUS': row.status or 'ATIVO',
            'ativo_no_app': bool(row.ativo_no_app) if row.ativo_no_app is not None else True,
            'gestor_direto': str(row.gestor_direto or '').strip().upper(),
            'gestor_superior': str(row.gestor_superior or '').strip().upper(),
        })
    return out


def get_resumo_ferias_por_matricula_postgres(matricula: str) -> Dict[str, Any]:
    """Resumo de férias direto da tabela oficial saldo_periodo.

    A matrícula é a chave operacional. Não resolve colaborador por e-mail.
    """
    session = get_db_session()
    matricula = str(matricula or '').strip().upper()
    empty = {
        'regular': {'direito': 0, 'usados': 0, 'reservados': 0, 'saldo': 0, 'ajustes': 0, 'periodos': [], 'periodo_atual': None},
        'premium': {'direito': 0, 'usados': 0, 'reservados': 0, 'saldo': 0, 'ajustes': 0, 'periodos': [], 'periodo_atual': None},
        'total_solicitacoes': 0,
    }
    if not matricula:
        return empty

    rows = (
        session.query(SaldoPeriodoNovo)
        .filter(func.upper(SaldoPeriodoNovo.colaborador_matricula) == matricula)
        .order_by(SaldoPeriodoNovo.tipo_saldo.asc(), SaldoPeriodoNovo.data_inicio.asc(), SaldoPeriodoNovo.periodo_numero.asc())
        .all()
    )
    grupos: Dict[str, List[Dict[str, Any]]] = {'REGULAR': [], 'PREMIUM': []}
    for s in rows:
        tipo = str(s.tipo_saldo or 'REGULAR').upper()
        if tipo not in grupos:
            grupos[tipo] = []
        grupos[tipo].append({
            'id': s.id,
            'periodo_id': s.id,
            'numero': int(s.periodo_numero or 0),
            'inicio': s.data_inicio,
            'fim': s.data_fim,
            'inicio_fmt': _formatar_data_br(s.data_inicio),
            'fim_fmt': _formatar_data_br(s.data_fim),
            'direito': _num_int(s.saldo_inicial),
            'usados': _num_int(s.saldo_utilizado),
            'reservados': _num_int(s.saldo_reservado),
            'saldo': _num_int(s.saldo_disponivel),
            'label': _periodo_label(int(s.periodo_numero or 0), s.data_inicio, s.data_fim),
            'atual': bool(s.is_atual),
            'tipo_saldo': tipo,
        })

    def totals(periodos: List[Dict[str, Any]]):
        return {
            'direito': sum(_num_int(p.get('direito')) for p in periodos),
            'usados': sum(_num_int(p.get('usados')) for p in periodos),
            'reservados': sum(_num_int(p.get('reservados')) for p in periodos),
            'saldo': sum(_num_int(p.get('saldo')) for p in periodos),
            'ajustes': 0,
            'periodos': periodos,
            'periodo_atual': next((p for p in periodos if p.get('atual')), None),
        }

    try:
        total = session.query(Solicitacao.id).filter(
            func.upper(Solicitacao.colaborador_matricula) == matricula,
            Solicitacao.is_ajuste.is_(False),
        ).count()
    except Exception:
        total = 0

    return {
        'regular': totals(grupos.get('REGULAR', [])),
        'premium': totals(grupos.get('PREMIUM', [])),
        'total_solicitacoes': int(total),
    }


def _solicitacao_tuple_matricula(sol: Solicitacao):
    status = canonical_status(sol.status or 'PENDENTE')
    saldo_tipo = (sol.saldo_tipo or sol.tipo_ferias or infer_saldo_tipo(sol.observacoes or '', '')).upper() or 'REGULAR'
    ident = str(sol.colaborador_matricula or '').strip().upper()
    if not ident:
        ident = safe_lower(sol.colaborador_email or '')
    dias_val = sol.dias if sol.dias is not None else sol.dias_solicitados
    return (
        sol.id,
        ident,
        _formatar_data_br(sol.data_inicio),
        _formatar_data_br(sol.data_fim),
        _num_int(dias_val),
        status,
        sol.solicitacao or sol.tipo_solicitacao or '',
        saldo_tipo,
        sol.observacoes or '',
    )


def listar_solicitacoes_matricula_postgres(matricula: str, limit: int = 250):
    """Histórico da tela /ferias para uma matrícula específica.

    Evita carregar todas as solicitações do banco a cada troca de colaborador.
    """
    session = get_db_session()
    matricula = str(matricula or '').strip().upper()
    if not matricula:
        return []
    query = session.query(Solicitacao).filter(
        func.upper(Solicitacao.colaborador_matricula) == matricula,
        Solicitacao.is_ajuste.is_(False),
    ).order_by(Solicitacao.data_inicio.desc(), Solicitacao.id.desc())
    if limit and limit > 0:
        query = query.limit(int(limit))
    return [_solicitacao_tuple_matricula(row) for row in query.all()]
