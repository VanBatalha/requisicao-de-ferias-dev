"""Sincronização do cadastro Smartsheet -> PostgreSQL.

Baseado no script de migração/sincronização usado pelo projeto, mas adaptado
para rodar dentro do app Flask/Render e ser acionado pelo Painel Admin ou por
um Render Cron Job.
"""
from __future__ import annotations

import calendar
import datetime as dt
import json
import os
import re
import time
import traceback
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional

import smartsheet
from sqlalchemy import func

from ..config import get_settings
from ..logging_config import get_logger
from ..models import Auditoria, Colaborador, ColaboradorComplemento, Solicitacao, SyncState, PermissaoUsuario, HierarquiaGestao, PeriodoAquisitivo, SaldoPeriodo, SaldoPeriodoNovo
from .postgres_service import get_session, dispose_engine

log = get_logger(__name__)


def _log_progress(stage: str, current: int | None = None, total: int | None = None, started_ts: float | None = None, message: str = "") -> None:
    """Emite progresso visivel no console/Render durante sincronizacoes longas."""
    if current is not None and total:
        pct = (current / total) * 100 if total else 0
        elapsed = time.monotonic() - started_ts if started_ts else 0
        if current and elapsed > 0:
            rate = current / elapsed
            remaining = max(total - current, 0) / rate if rate > 0 else 0
            log.info("SYNC %s: %s/%s (%.1f%%) - estimativa restante %.0fs%s", stage, current, total, pct, remaining, f" - {message}" if message else "")
        else:
            log.info("SYNC %s: %s/%s (%.1f%%)%s", stage, current, total, pct, f" - {message}" if message else "")
    else:
        log.info("SYNC %s%s", stage, f": {message}" if message else "")

STATUS_ATIVO_SET = {"ATIVO", "ACTIVE"}
STATUS_INATIVO_SET = {"INATIVO", "INACTIVE", "DESLIGADO", "DEMITIDO", "RESCINDIDO", "AFASTADO"}


@dataclass
class SheetMaps:
    columns: Dict[str, int]
    rows: list


@dataclass
class ColaboradorRecord:
    row_id: int
    email: str
    nome: Any
    status: Any
    admissao: Optional[dt.date]
    setor: Any
    cargo: Any
    regime: Any
    dias_direito: int
    user_type: str
    gestor_direto: str
    gestor_superior: str
    ativo_no_app: bool
    matricula: str
    payload: Dict[str, Any]


def normalize_text(value: Any) -> str:
    text = str(value or "").strip()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.upper()


def safe_lower(value: Any) -> str:
    return str(value or "").strip().lower()


def clean_optional(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def coalesce_sheet_value(new_value: Any, current_value: Any = None) -> Any:
    """Evita que células vazias do Smartsheet apaguem dados já corrigidos no PostgreSQL."""
    if new_value in (None, "", [], {}):
        return current_value
    if isinstance(new_value, str) and not new_value.strip():
        return current_value
    return new_value


def normalize_user_type_value(value: Any) -> str:
    raw = normalize_text(value)
    if not raw:
        return ""
    aliases = {
        "ADMINISTRADOR": "ADMIN",
        "ADMINISTRADORES": "ADMIN",
        "ADM": "ADMIN",
        "RH": "DP",
        "DEPARTAMENTO PESSOAL": "DP",
    }
    raw = aliases.get(raw, raw)
    return raw if raw in {"USER", "DP", "ADMIN"} else "USER"


def normalize_matricula(value: Any) -> str:
    """Normaliza a matrícula como código externo do cadastro.

    Não converte para número porque valores como MAT00061 perderiam zeros/prefixo.
    """
    return str(value or "").strip().upper()


def extract_id_from_matricula(matricula: Any) -> Optional[int]:
    """Extrai o número da matrícula para preencher colaboradores.id.

    Ex.: MAT00832 -> 832. Mantemos a matrícula textual como identificador
    principal de negócio, mas o banco atual ainda usa id inteiro em FKs legadas.
    """
    text = normalize_matricula(matricula)
    if not text:
        return None
    match = re.search(r"\d+", text)
    return int(match.group()) if match else None


def _email_local(email: Any) -> str:
    email = safe_lower(email or "")
    return email.split("@", 1)[0].strip() if "@" in email else email.strip()


def _single_or_none(rows: list):
    """Retorna o único item quando a busca é inequívoca; evita vincular matrícula duplicada."""
    return rows[0] if len(rows) == 1 else None


def parse_date(value: Any) -> Optional[dt.date]:
    """Converte valores do Smartsheet/Excel/texto em date.

    Campos como DATA DE ADMISSÃO às vezes trazem histórico em múltiplas linhas,
    por exemplo:
        01/03/16\nSOCIEDADE\n27/10/2020
    Para cálculo de férias, usamos a data mais antiga encontrada no texto.
    """
    if value in (None, ""):
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, (int, float)):
        try:
            # Serial Excel/Smartsheet quando veio de planilha .xlsx/export.
            if 1 <= float(value) <= 80000:
                return (dt.datetime(1899, 12, 30) + dt.timedelta(days=float(value))).date()
        except Exception:
            pass

    text = str(value).strip()
    if not text:
        return None

    found_dates: list[dt.date] = []

    def _parse_br_date(day: str, month: str, year: str) -> Optional[dt.date]:
        try:
            y = int(year)
            if y < 100:
                y = 2000 + y if y <= 69 else 1900 + y
            return dt.date(y, int(month), int(day))
        except Exception:
            return None

    # Datas brasileiras em qualquer ponto do texto, inclusive linhas históricas.
    for match in re.finditer(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b", text):
        parsed = _parse_br_date(match.group(1), match.group(2), match.group(3))
        if parsed:
            found_dates.append(parsed)

    # Datas ISO em qualquer ponto do texto.
    for match in re.finditer(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", text):
        try:
            found_dates.append(dt.date(int(match.group(1)), int(match.group(2)), int(match.group(3))))
        except Exception:
            pass

    if found_dates:
        return min(found_dates)

    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%m/%d/%y", "%m/%d/%Y"):
        try:
            return dt.datetime.strptime(text[:19], fmt).date()
        except ValueError:
            continue
    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except Exception:
        return None

def parse_int(value: Any, default: int = 0) -> int:
    if value in (None, ""):
        return default
    try:
        text = str(value).strip()
        if text.count(",") == 1 and text.count(".") == 0:
            text = text.replace(",", ".")
        return int(round(float(text)))
    except Exception:
        return default


def add_months(date_value: dt.date, months: int) -> dt.date:
    year = date_value.year + (date_value.month - 1 + months) // 12
    month = (date_value.month - 1 + months) % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    day = min(date_value.day, last_day)
    return dt.date(year, month, day)


def completed_aquisitive_periods(admissao: Optional[dt.date], hoje: Optional[dt.date] = None) -> int:
    if not admissao:
        return 0
    hoje = hoje or dt.date.today()
    if hoje < admissao:
        return 0
    count = 0
    while add_months(admissao, (count + 1) * 12) <= hoje:
        count += 1
    return count


def current_partial_period(admissao: Optional[dt.date], hoje: Optional[dt.date] = None) -> Optional[dict]:
    if not admissao:
        return None
    hoje = hoje or dt.date.today()
    completos = completed_aquisitive_periods(admissao, hoje)
    inicio = add_months(admissao, completos * 12)
    fim = add_months(admissao, (completos + 1) * 12) - dt.timedelta(days=1)
    if hoje < inicio:
        return None
    return {
        "numero": completos + 1,
        "inicio": inicio.isoformat(),
        "fim": fim.isoformat(),
        "label": f"Período {completos + 1} — {inicio.strftime('%d/%m/%Y')} a {fim.strftime('%d/%m/%Y')}",
    }


def premium_window(admissao: Optional[dt.date], hoje: Optional[dt.date] = None) -> tuple[int, Optional[dt.date], Optional[dt.date]]:
    if not admissao:
        return 0, None, None
    hoje = hoje or dt.date.today()
    years = hoje.year - admissao.year
    if (hoje.month, hoje.day) < (admissao.month, admissao.day):
        years -= 1
    if years < 5:
        return 0, None, None
    anos_da_conquista = (years // 5) * 5
    if anos_da_conquista < 5:
        return 0, None, None
    inicio = dt.date(admissao.year + anos_da_conquista, admissao.month, admissao.day)
    fim_exclusivo = dt.date(inicio.year + 2, inicio.month, inicio.day)
    if hoje >= fim_exclusivo:
        return 0, None, None
    return 30, inicio, fim_exclusivo


def col_id(columns: Dict[str, int], *names: str) -> Optional[int]:
    for name in names:
        cid = columns.get(normalize_text(name))
        if cid:
            return cid
    return None


def row_as_dict(row: Any, columns: Dict[str, int]) -> Dict[str, Any]:
    reverse = {cid: name for name, cid in columns.items()}
    out: Dict[str, Any] = {}
    for cell in getattr(row, "cells", []) or []:
        key = reverse.get(cell.column_id, str(cell.column_id))
        out[key] = getattr(cell, "display_value", None) or getattr(cell, "value", None)
    return out


def cell_value(row: Any, cid: Optional[int]) -> Any:
    if not cid:
        return None
    for cell in getattr(row, "cells", []) or []:
        if cell.column_id == cid:
            return getattr(cell, "display_value", None) or getattr(cell, "value", None)
    return None


def get_sheet(client: smartsheet.Smartsheet, sheet_id: int) -> SheetMaps:
    sheet = client.Sheets.get_sheet(sheet_id)
    columns = {normalize_text(col.title): col.id for col in sheet.columns}
    return SheetMaps(columns=columns, rows=list(sheet.rows or []))


def build_colaborador_record(row: Any, cadastro: SheetMaps) -> Optional[ColaboradorRecord]:
    c_email = col_id(cadastro.columns, "EMAIL DA EMPRESA", "EMAIL", "E-MAIL")
    c_nome = col_id(cadastro.columns, "NOME COMPLETO", "NOME", "COLABORADOR", "NOME DO COLABORADOR", "FUNCIONARIO", "FUNCIONÁRIO")
    c_status = col_id(cadastro.columns, "STATUS", "SITUACAO", "SITUAÇÃO")
    c_adm = col_id(cadastro.columns, "ADMISSAO", "ADMISSÃO", "DATA DE ADMISSAO", "DATA DE ADMISSÃO", "DATA ADMISSAO", "DATA ADMISSÃO")
    c_setor = col_id(cadastro.columns, "SETOR", "AREA", "ÁREA", "DEPARTAMENTO", "CENTRO DE CUSTO")
    c_cargo = col_id(cadastro.columns, "CARGO", "FUNCAO", "FUNÇÃO", "FUNCAO/CARGO", "FUNÇÃO/CARGO")
    c_regime = col_id(cadastro.columns, "REGIME", "REGIME DE CONTRATACAO", "REGIME DE CONTRATAÇÃO")
    c_dias_direito = col_id(cadastro.columns, "DIAS DE DIREITO", "DIAS DIREITO", "DIREITO", "SALDO DIREITO")
    c_user_type = col_id(cadastro.columns, "USER TYPE", "USER_TYPE", "USERTYPE", "TIPO USUARIO", "TIPO DE USUARIO")
    c_gestor_direto = col_id(cadastro.columns, "GESTOR DIRETO", "GESTOR")
    c_gestor_superior = col_id(cadastro.columns, "GESTOR SUPERIOR")
    c_matricula = col_id(cadastro.columns, "MATRICULA", "MATRÍCULA", "MATRICULA DO COLABORADOR")

    email = safe_lower(cell_value(row, c_email))
    if not email:
        return None

    status = cell_value(row, c_status)
    ativo_no_app = normalize_text(status) not in STATUS_INATIVO_SET
    payload = row_as_dict(row, cadastro.columns)
    user_type = normalize_user_type_value(cell_value(row, c_user_type))

    return ColaboradorRecord(
        row_id=row.id,
        email=email,
        nome=cell_value(row, c_nome),
        status=status,
        admissao=parse_date(cell_value(row, c_adm)),
        setor=cell_value(row, c_setor),
        cargo=cell_value(row, c_cargo),
        regime=cell_value(row, c_regime),
        dias_direito=parse_int(cell_value(row, c_dias_direito), 0),
        user_type=user_type,
        gestor_direto=safe_lower(cell_value(row, c_gestor_direto)),
        gestor_superior=safe_lower(cell_value(row, c_gestor_superior)),
        ativo_no_app=ativo_no_app,
        matricula=normalize_matricula(cell_value(row, c_matricula)),
        payload=payload,
    )


def status_rank(status: Any) -> int:
    norm = normalize_text(status)
    if norm in STATUS_ATIVO_SET:
        return 100
    if norm in STATUS_INATIVO_SET:
        return 0
    return 50


def filled_score(record: ColaboradorRecord) -> int:
    fields: Iterable[Any] = (
        record.nome,
        record.status,
        record.admissao,
        record.setor,
        record.cargo,
        record.regime,
        record.user_type,
        record.gestor_direto,
        record.gestor_superior,
        record.matricula,
    )
    score = sum(1 for v in fields if v not in (None, "", [], {}, 0))
    score += status_rank(record.status)
    if record.dias_direito > 0:
        score += 2
    return score


def choose_better_record(current: ColaboradorRecord, candidate: ColaboradorRecord) -> ColaboradorRecord:
    current_score = filled_score(current)
    candidate_score = filled_score(candidate)
    if candidate_score > current_score:
        winner = candidate
        loser = current
    else:
        winner = current
        loser = candidate

    for attr in ("nome", "status", "admissao", "setor", "cargo", "regime", "user_type", "gestor_direto", "gestor_superior", "matricula"):
        if getattr(winner, attr) in (None, "") and getattr(loser, attr) not in (None, ""):
            setattr(winner, attr, getattr(loser, attr))

    if winner.dias_direito <= 0 and loser.dias_direito > 0:
        winner.dias_direito = loser.dias_direito

    duplicate_rows = winner.payload.get("__duplicate_row_ids__", [])
    if loser.row_id not in duplicate_rows:
        duplicate_rows.append(loser.row_id)
    winner.payload["__duplicate_row_ids__"] = sorted(set(duplicate_rows))
    return winner


def deduplicate_colaboradores(cadastro: SheetMaps) -> tuple[list[ColaboradorRecord], dict[str, list[int]]]:
    """Remove duplicidades da própria planilha usando a matrícula como chave principal.

    A matrícula é tratada como o ID externo oficial do cadastro. Quando a linha
    não tem matrícula, usamos o e-mail apenas como chave auxiliar para não
    processar a mesma pessoa duas vezes na mesma execução.
    """
    by_key: dict[str, ColaboradorRecord] = {}
    duplicates: dict[str, list[int]] = {}

    for row in cadastro.rows:
        record = build_colaborador_record(row, cadastro)
        if not record:
            continue

        key = f"matricula:{record.matricula}" if record.matricula else f"email:{record.email}"
        if key not in by_key:
            by_key[key] = record
            continue

        duplicates.setdefault(key, [by_key[key].row_id])
        duplicates[key].append(record.row_id)
        by_key[key] = choose_better_record(by_key[key], record)

    return list(by_key.values()), duplicates


def _mark_sync(session, sync_name: str, status: str, error: Optional[str] = None, success: bool = False, extra: Optional[dict] = None):
    now = dt.datetime.utcnow()
    row = session.query(SyncState).filter(SyncState.sync_name == sync_name).first()
    if not row:
        row = SyncState(sync_name=sync_name)
        session.add(row)
    if status == "running":
        row.last_started_at = now
    row.last_finished_at = now
    row.last_status = status
    row.last_error = error
    row.extra = extra or row.extra
    if success:
        row.last_success_at = now
    row.updated_at = now


def _resolve_colaborador_by_email(session, email: Any, only_active: bool = False) -> Optional[Colaborador]:
    """Resolve colaborador por e-mail, priorizando cadastro ATIVO.

    Quando existe duplicidade de e-mail por mudança de contrato/modalidade, o app
    deve usar o cadastro ativo para login, permissões e hierarquia. O registro
    inativo permanece no banco apenas para histórico.
    """
    email_norm = safe_lower(email or "")
    if not email_norm:
        return None

    def is_active(c: Colaborador) -> bool:
        return normalize_text(getattr(c, "status", None)) in STATUS_ATIVO_SET

    def choose(rows: list[Colaborador]) -> Optional[Colaborador]:
        if only_active:
            rows = [c for c in rows if is_active(c)]
        if not rows:
            return None
        rows.sort(key=lambda c: (1 if is_active(c) else 0, int(getattr(c, "id", 0) or 0)), reverse=True)
        return rows[0]

    exact = session.query(Colaborador).filter(func.lower(Colaborador.email) == email_norm).all()
    chosen = choose(exact)
    if chosen:
        return chosen

    local = _email_local(email_norm)
    if not local:
        return None
    try:
        rows = session.query(Colaborador).filter(func.split_part(func.lower(Colaborador.email), "@", 1) == local).all()
    except Exception:
        rows = [c for c in session.query(Colaborador).all() if _email_local(c.email) == local]
    return choose(rows)


def _get_colaborador_by_matricula(session, matricula: Any) -> Optional[Colaborador]:
    matricula_norm = normalize_matricula(matricula)
    if not matricula_norm:
        return None
    return session.query(Colaborador).filter(func.upper(Colaborador.matricula) == matricula_norm).first()


def _max_matricula_numero_pg(session) -> int:
    """Retorna a maior matricula apenas para estatistica do relatório de sync.

    Essa consulta não é necessária para a carga funcionar. Em execução local usando
    a External Database URL do Render, varrer a tabela logo no início podia dar a
    impressão de travamento ou segurar a conexão por tempo demais. Por padrão,
    pulamos essa estatística e retornamos 0. Para habilitar novamente, defina
    SYNC_CALC_MAX_MATRICULA=true.
    """
    enabled = str(os.getenv("SYNC_CALC_MAX_MATRICULA", "false") or "").strip().lower()
    if enabled not in {"1", "true", "sim", "yes", "y"}:
        _log_progress("postgres", message="pulando leitura de maior matricula inicial (estatistica opcional)")
        return 0

    try:
        _log_progress("postgres", message="calculando maior matricula inicial")
        max_num = 0
        # yield_per evita carregar tudo de uma vez em bases maiores.
        for (matricula,) in session.query(Colaborador.matricula).yield_per(100):
            try:
                n = extract_id_from_matricula(matricula)
                if n is not None and n > max_num:
                    max_num = n
            except Exception:
                continue
        return max_num
    except Exception as exc:
        log.warning("Nao foi possivel calcular maior matricula inicial; seguindo sync sem essa estatistica: %s", exc)
        try:
            session.rollback()
        except Exception:
            pass
        dispose_engine()
        return 0


def _values_equal(a: Any, b: Any) -> bool:
    if isinstance(a, dt.date) or isinstance(b, dt.date):
        return parse_date(a) == parse_date(b)
    return str(a or "").strip() == str(b or "").strip()


def _update_colaborador_from_record(colab: Colaborador, record: ColaboradorRecord, sheet_id_str: str, row_id_str: str) -> bool:
    """Atualiza campos cadastrais que continuam tendo origem no Smartsheet.

    A matrícula identifica o registro. Saldos, solicitações e ajustes feitos no
    app não são alterados por esta rotina.
    """
    changed = False
    updates = {
        "email": record.email,
        "nome_completo": clean_optional(record.nome) or colab.nome_completo,
        "status": clean_optional(record.status) or colab.status or "ATIVO",
        "data_admissao": record.admissao,
        "setor": clean_optional(record.setor),
        "cargo": clean_optional(record.cargo),
        "regime": clean_optional(record.regime),
        "dias_direito": int(record.dias_direito or 0),
        "origem_sheet_id": sheet_id_str,
        "origem_row_id": row_id_str,
    }
    for attr, value in updates.items():
        # Evita apagar informação boa do banco com célula vazia, exceto status/dias.
        if attr not in {"status", "dias_direito", "origem_sheet_id", "origem_row_id"} and value in (None, ""):
            continue
        current = getattr(colab, attr, None)
        if not _values_equal(current, value):
            setattr(colab, attr, value)
            changed = True

    payload = dict(record.payload or {})
    payload.setdefault("__matricula_escolhida__", colab.matricula)
    if colab.raw_payload != payload:
        colab.raw_payload = payload
        changed = True

    if changed:
        colab.updated_at = dt.datetime.utcnow()
    return changed


def _upsert_complemento_acesso(session, colab: Colaborador, record: ColaboradorRecord, sheet_id_str: str, row_id_str: str) -> bool:
    """Atualiza o cache de acesso/hierarquia sem mexer em saldos."""
    comp = session.query(ColaboradorComplemento).filter(ColaboradorComplemento.colaborador_id == colab.id).first()
    created = False
    if not comp:
        comp = ColaboradorComplemento(colaborador_id=colab.id, colaborador_matricula=colab.matricula)
        session.add(comp)
        created = True

    comp.colaborador_matricula = colab.matricula
    comp.user_type = record.user_type or "USER"

    # Campos legados por e-mail permanecem preenchidos para compatibilidade,
    # mas a relação operacional nova usa matrícula/texto especial.
    gestor_direto_email = safe_lower(record.gestor_direto)
    gestor_direto = _resolve_colaborador_by_email(session, gestor_direto_email, only_active=True) if gestor_direto_email else None
    superior_raw = str(record.gestor_superior or "").strip()
    superior_norm = normalize_text(superior_raw)

    comp.gestor_direto_email = clean_optional(record.gestor_direto)
    comp.gestor_direto = gestor_direto.matricula if gestor_direto else None
    comp.gestor_superior_email = clean_optional(record.gestor_superior)

    if superior_norm in {"DP", "RH", "DEPARTAMENTO PESSOAL"}:
        comp.gestor_superior = "DP"
    elif superior_norm in {"GESTOR", "GESTORES", "GESTOR DIRETO"}:
        comp.gestor_superior = "GESTOR"
    elif superior_raw:
        sup_email = safe_lower(superior_raw)
        sup = _resolve_colaborador_by_email(session, sup_email, only_active=True) if "@" in sup_email else None
        comp.gestor_superior = sup.matricula if sup else superior_raw
    else:
        comp.gestor_superior = None

    comp.ativo_no_app = bool(record.ativo_no_app)
    comp.origem_sheet_id = sheet_id_str
    comp.origem_row_id = row_id_str
    comp.updated_at = dt.datetime.utcnow()
    return created


def _sync_permissoes_usuario(session, colab: Colaborador, user_type: str) -> str:
    """Sincroniza a tabela app_ferias.permissoes_usuario pela matrícula.

    Mantemos um registro de role para todos os colaboradores processados, inclusive
    USER, para facilitar auditoria no pgAdmin. ADMIN/DP continuam sendo as únicas
    roles elevadas usadas pelo app.
    """
    role = normalize_user_type_value(user_type) or "USER"
    if role == "ADMINISTRADOR":
        role = "ADMIN"

    # Remove roles antigas daquele colaborador para refletir o USER TYPE atual do Smartsheet.
    session.query(PermissaoUsuario).filter(
        (PermissaoUsuario.colaborador_matricula == colab.matricula) | (PermissaoUsuario.colaborador_id == colab.id)
    ).delete(synchronize_session=False)

    session.add(PermissaoUsuario(
        colaborador_id=colab.id,
        colaborador_matricula=colab.matricula,
        role=role,
    ))
    return role


def _sync_hierarquia_gestao(session, colab: Colaborador, record: ColaboradorRecord) -> bool:
    """Sincroniza app_ferias.hierarquia_gestao a partir da planilha de cadastro."""
    h = session.query(HierarquiaGestao).filter(
        (HierarquiaGestao.colaborador_matricula == colab.matricula) | (HierarquiaGestao.colaborador_id == colab.id)
    ).first()
    created = False
    if not h:
        h = HierarquiaGestao(colaborador_id=colab.id, colaborador_matricula=colab.matricula)
        session.add(h)
        created = True

    gestor_direto_email = safe_lower(record.gestor_direto)
    gestor_direto = _resolve_colaborador_by_email(session, gestor_direto_email, only_active=True) if gestor_direto_email else None

    superior_raw = str(record.gestor_superior or "").strip()
    superior_norm = normalize_text(superior_raw)
    gestor_superior_tipo = "GESTOR"
    gestor_superior_email_custom = None
    gestor_superior = None

    if superior_norm in {"DP", "RH", "DEPARTAMENTO PESSOAL"}:
        gestor_superior_tipo = "DP"
    elif "@" in superior_raw:
        gestor_superior_tipo = "EMAIL_CUSTOM"
        gestor_superior_email_custom = safe_lower(superior_raw)
        gestor_superior = _resolve_colaborador_by_email(session, gestor_superior_email_custom, only_active=True)
    elif superior_norm in {"GESTOR", "GESTORES", "GESTOR DIRETO"}:
        gestor_superior_tipo = "GESTOR"
    elif superior_raw:
        # Valor não reconhecido: preserva como texto customizado para auditoria.
        gestor_superior_tipo = "CUSTOM"
        gestor_superior_email_custom = superior_raw

    h.colaborador_id = colab.id
    h.colaborador_matricula = colab.matricula
    h.gestor_direto_id = gestor_direto.id if gestor_direto else None
    h.gestor_direto_matricula = gestor_direto.matricula if gestor_direto else None
    h.gestor_direto_email = gestor_direto_email or None
    h.gestor_superior_tipo = gestor_superior_tipo
    h.gestor_superior_id = gestor_superior.id if gestor_superior else None
    h.gestor_superior_matricula = gestor_superior.matricula if gestor_superior else None
    h.gestor_superior_email_custom = gestor_superior_email_custom
    return created


def _sync_colaboradores(session, cadastro: SheetMaps, cadastro_sheet_id: int, precomputed: Optional[tuple[list[ColaboradorRecord], dict]] = None) -> dict:
    """Sincroniza cadastro, permissões e hierarquia a partir do Smartsheet.

    Regras atuais:
    - Matrícula é a chave principal de negócio.
    - Dados cadastrais principais já existentes no PostgreSQL são preservados.
    - Matrículas novas são incluídas com os dados iniciais do Smartsheet.
    - Permissões, hierarquia e cache de acesso são sincronizados a cada execução,
      pois essas tabelas estavam vazias na nova base e são essenciais para menus,
      admins, gestores e DP.
    """
    # V22: em execuções locais usando a External Database URL do Render, a
    # deduplicação da planilha pode levar tempo. Se ela ocorrer depois que
    # uma conexão PostgreSQL já foi aberta, a conexão SSL pode ficar ociosa
    # e ser derrubada antes da primeira consulta real. Por isso aceitamos
    # registros já pré-processados antes de abrir a sessão/transação.
    if precomputed is not None:
        records, duplicates = precomputed
    else:
        records, duplicates = deduplicate_colaboradores(cadastro)
    inserted = linked = existing_skipped = existing_updated = skipped = conflicts = sem_matricula = 0
    new_above_last_matricula = existing_changed = existing_unchanged = 0
    max_matricula_pg_inicio = _max_matricula_numero_pg(session)
    complemento_inserted = complemento_updated = 0
    permissoes_synced = hierarquia_synced = hierarquia_inserted = 0
    matched_by = {
        "matricula_existente": 0,
        "origem_existente": 0,
        "email_existente": 0,  # legado: não usado para vínculo cadastral desde a V23
        "vinculado_por_email_ou_origem": 0,
        "novo": 0,
        "sem_matricula": 0,
        "id_conflitante": 0,
    }
    conflict_details: list[dict] = []
    processed_for_access: list[tuple[Colaborador, ColaboradorRecord, str]] = []

    sheet_id_str = str(cadastro_sheet_id)

    total_records = len(records)
    started_progress = time.monotonic()
    cadastro_batch_size = 25
    _log_progress("cadastro", 0, total_records, started_progress, "iniciando inclusao/atualizacao em lotes")

    for idx, record in enumerate(records, start=1):
        if idx == 1 or idx % cadastro_batch_size == 0 or idx == total_records:
            if idx > 1 and idx % cadastro_batch_size == 0:
                try:
                    session.commit()
                    _log_progress("cadastro", idx - 1, total_records, started_progress, "lote de cadastro gravado")
                except Exception:
                    session.rollback()
                    raise
            _log_progress("cadastro", idx, total_records, started_progress, "processando colaboradores")
        row_id_str = str(record.row_id)
        record_matricula = normalize_matricula(record.matricula)

        if not record_matricula:
            sem_matricula += 1
            skipped += 1
            matched_by["sem_matricula"] += 1
            conflict_details.append({
                "email": record.email,
                "origem_row_id": row_id_str,
                "acao": "ignorado_sem_matricula",
                "motivo": "A sincronização exige a coluna MATRÍCULA preenchida.",
            })
            continue

        by_matricula = _get_colaborador_by_matricula(session, record_matricula)
        if by_matricula:
            existing_skipped += 1
            matched_by["matricula_existente"] += 1
            if _update_colaborador_from_record(by_matricula, record, sheet_id_str, row_id_str):
                existing_updated += 1
                existing_changed += 1
            else:
                existing_unchanged += 1
            processed_for_access.append((by_matricula, record, row_id_str))
            continue

        by_origem = session.query(Colaborador).filter(
            Colaborador.origem_sheet_id == sheet_id_str,
            Colaborador.origem_row_id == row_id_str,
        ).first()
        # V23: a matrícula é a chave de negócio. Não usamos mais e-mail como
        # critério de vínculo de cadastro, porque existem recontratações/trocas
        # de modalidade em que o mesmo e-mail aparece em duas matrículas
        # diferentes (uma inativa e outra ativa). A busca operacional do app
        # já prioriza status ATIVO, então o histórico pode coexistir.
        legacy_colab = by_origem
        if legacy_colab:
            if legacy_colab.matricula and legacy_colab.matricula.upper() != record_matricula:
                conflicts += 1
                matched_by["origem_existente"] += 1
                conflict_details.append({
                    "email": record.email,
                    "matricula_smartsheet": record_matricula,
                    "matricula_postgres": legacy_colab.matricula,
                    "origem_row_id": row_id_str,
                    "colaborador_id": legacy_colab.id,
                    "acao": "preservado_sem_alterar",
                    "motivo": "Mesma linha de origem aponta para outra matrícula. Registro preservado para evitar sobrescrita indevida.",
                })
                processed_for_access.append((legacy_colab, record, row_id_str))
                continue

            legacy_colab.matricula = record_matricula
            _update_colaborador_from_record(legacy_colab, record, sheet_id_str, row_id_str)
            linked += 1
            matched_by["vinculado_por_email_ou_origem"] += 1
            processed_for_access.append((legacy_colab, record, row_id_str))
            continue

        id_num = extract_id_from_matricula(record_matricula)
        if id_num is None:
            conflicts += 1
            matched_by["id_conflitante"] += 1
            conflict_details.append({
                "email": record.email,
                "matricula_smartsheet": record_matricula,
                "origem_row_id": row_id_str,
                "acao": "ignorado_id_invalido",
                "motivo": "Não foi possível extrair número da matrícula para preencher colaboradores.id.",
            })
            continue
        by_id = session.query(Colaborador).filter(Colaborador.id == id_num).first()
        if by_id and normalize_matricula(by_id.matricula) != record_matricula:
            conflicts += 1
            matched_by["id_conflitante"] += 1
            conflict_details.append({
                "email": record.email,
                "matricula_smartsheet": record_matricula,
                "id_extraido": id_num,
                "matricula_postgres_no_mesmo_id": by_id.matricula,
                "origem_row_id": row_id_str,
                "acao": "ignorado_id_conflitante",
                "motivo": "O número extraído da matrícula já pertence a outro colaborador.",
            })
            continue

        payload = dict(record.payload)
        payload.setdefault("__matricula_escolhida__", record_matricula)
        nome = clean_optional(record.nome) or f"COLABORADOR SEM NOME ({record_matricula})"

        colab = Colaborador(
            id=id_num,
            email=record.email,
            matricula=record_matricula,
            nome_completo=nome,
            status=clean_optional(record.status) or "ATIVO",
            data_admissao=record.admissao,
            setor=clean_optional(record.setor),
            cargo=clean_optional(record.cargo),
            regime=clean_optional(record.regime),
            dias_direito=int(record.dias_direito or 0),
            origem_sheet_id=sheet_id_str,
            origem_row_id=row_id_str,
            raw_payload=payload,
        )
        session.add(colab)
        session.flush()
        inserted += 1
        id_num_inserted = extract_id_from_matricula(record_matricula) or 0
        if id_num_inserted > max_matricula_pg_inicio:
            new_above_last_matricula += 1
        matched_by["novo"] += 1
        processed_for_access.append((colab, record, row_id_str))

    # Commit intermediario antes da etapa de acessos.
    # Na External Database URL do Render, transacoes muito longas derrubam SSL.
    # Gravamos o cadastro em lote e iniciamos a etapa seguinte com transacoes curtas.
    try:
        session.commit()
        _log_progress("postgres", message="cadastro gravado; iniciando permissoes/hierarquia em lotes")
    except Exception:
        session.rollback()
        raise

    # Segunda passada: agora todos os colaboradores existentes/novos ja estao visiveis
    # no banco. As permissoes/hierarquia sao gravadas em pequenos lotes, com commit
    # a cada 25 registros. Se a conexao externa cair no meio, fazemos rollback,
    # descartamos o pool e seguimos para o proximo registro, evitando efeito cascata
    # de PendingRollbackError ate o fim da sincronizacao.
    total_access = len(processed_for_access)
    access_progress = time.monotonic()
    access_batch_size = 25
    _log_progress("acessos", 0, total_access, access_progress, "permissoes e hierarquia")
    for access_idx, (colab, record, row_id_str) in enumerate(processed_for_access, start=1):
        if access_idx == 1 or access_idx % access_batch_size == 0 or access_idx == total_access:
            _log_progress("acessos", access_idx, total_access, access_progress, "permissoes e hierarquia")
        try:
            # Recarrega o colaborador na sessao atual; depois de commits/rollbacks
            # intermediarios, evita usar um objeto ORM com estado antigo.
            fresh_colab = session.get(Colaborador, getattr(colab, "id", None))
            if not fresh_colab:
                conflicts += 1
                conflict_details.append({
                    "email": record.email,
                    "matricula": getattr(colab, "matricula", None),
                    "acao": "falha_acesso_hierarquia",
                    "motivo": "Colaborador nao encontrado ao sincronizar acesso/hierarquia.",
                })
                continue

            with session.begin_nested():
                created_comp = _upsert_complemento_acesso(session, fresh_colab, record, sheet_id_str, row_id_str)
                if created_comp:
                    complemento_inserted += 1
                else:
                    complemento_updated += 1
                _sync_permissoes_usuario(session, fresh_colab, record.user_type or "USER")
                permissoes_synced += 1
                created_h = _sync_hierarquia_gestao(session, fresh_colab, record)
                hierarquia_synced += 1
                if created_h:
                    hierarquia_inserted += 1
        except Exception as exc:
            conflicts += 1
            try:
                session.rollback()
            except Exception:
                pass
            log.exception("Falha ao sincronizar acesso/hierarquia para %s (%s)", record.email, getattr(colab, "matricula", None))
            conflict_details.append({
                "email": record.email,
                "matricula": getattr(colab, "matricula", None),
                "acao": "falha_acesso_hierarquia",
                "motivo": str(exc)[:500],
            })
            continue

        if access_idx % access_batch_size == 0 or access_idx == total_access:
            try:
                session.commit()
                _log_progress("acessos", access_idx, total_access, access_progress, "lote gravado")
            except Exception as exc:
                conflicts += 1
                try:
                    session.rollback()
                except Exception:
                    pass
                log.exception("Falha ao gravar lote de permissoes/hierarquia ate o item %s", access_idx)
                conflict_details.append({
                    "email": record.email,
                    "matricula": getattr(colab, "matricula", None),
                    "acao": "falha_commit_lote_acesso",
                    "motivo": str(exc)[:500],
                })
                continue

    return {
        "mode": "sync_by_matricula_saldo_periodo_v31",
        "records": len(records),
        "inserted": inserted,
        "updated": existing_updated,
        "linked": linked,
        "existing_skipped": existing_skipped,
        "existing_changed": existing_changed,
        "existing_unchanged": existing_unchanged,
        "max_matricula_pg_inicio": max_matricula_pg_inicio,
        "new_above_last_matricula": new_above_last_matricula,
        "complemento_inserted": complemento_inserted,
        "complemento_updated": complemento_updated,
        "permissoes_synced": permissoes_synced,
        "hierarquia_synced": hierarquia_synced,
        "hierarquia_inserted": hierarquia_inserted,
        "skipped": skipped,
        "sem_matricula": sem_matricula,
        "conflicts": conflicts,
        "matched_by": matched_by,
        "conflict_details": conflict_details[:50],
        "duplicates": {key: sorted(set(rows)) for key, rows in duplicates.items()},
    }


def _reference_date() -> dt.date:
    """Data base para cálculo de saldos/períodos.

    Por padrão usa a data atual do ambiente. Para reproduzir uma fotografia
    específica, defina SYNC_REFERENCE_DATE=YYYY-MM-DD ou DD/MM/YYYY.
    """
    ref = parse_date(os.getenv("SYNC_REFERENCE_DATE"))
    return ref or dt.date.today()


def _iter_periodos_aquisitivos(admissao: Optional[dt.date], ref_date: Optional[dt.date] = None):
    if not admissao:
        return
    ref_date = ref_date or _reference_date()
    if ref_date < admissao:
        return
    numero = 1
    inicio = admissao
    while inicio <= ref_date:
        fim = add_months(admissao, numero * 12) - dt.timedelta(days=1)
        yield numero, inicio, fim, inicio <= ref_date <= fim
        numero += 1
        inicio = add_months(admissao, (numero - 1) * 12)


def _find_periodo_for_date(periodos: list[PeriodoAquisitivo], data_ref: Optional[dt.date]) -> Optional[PeriodoAquisitivo]:
    if not periodos:
        return None
    if data_ref:
        for periodo in periodos:
            if periodo.data_inicio <= data_ref <= periodo.data_fim:
                return periodo
    atuais = [p for p in periodos if p.is_atual]
    if atuais:
        return atuais[0]
    return periodos[-1]


def _get_or_create_saldo_periodo(session, periodo: PeriodoAquisitivo, tipo_saldo: str) -> SaldoPeriodo:
    tipo = (tipo_saldo or "REGULAR").upper()
    saldo = session.query(SaldoPeriodo).filter(
        SaldoPeriodo.periodo_id == periodo.id,
        SaldoPeriodo.tipo_saldo == tipo,
    ).first()
    if not saldo:
        saldo = SaldoPeriodo(periodo_id=periodo.id, tipo_saldo=tipo)
        session.add(saldo)
        session.flush()
    return saldo


def _get_or_create_saldo_periodo_novo(session, colab: Colaborador, periodo: PeriodoAquisitivo, tipo_saldo: str) -> SaldoPeriodoNovo:
    tipo = (tipo_saldo or "REGULAR").upper()
    saldo = session.query(SaldoPeriodoNovo).filter(
        SaldoPeriodoNovo.colaborador_matricula == colab.matricula,
        SaldoPeriodoNovo.periodo_numero == int(periodo.periodo_numero or 0),
        SaldoPeriodoNovo.tipo_saldo == tipo,
    ).first()
    if not saldo:
        saldo = SaldoPeriodoNovo(
            colaborador_id=colab.id,
            colaborador_matricula=colab.matricula,
            periodo_numero=int(periodo.periodo_numero or 0),
            data_inicio=periodo.data_inicio,
            data_fim=periodo.data_fim,
            is_atual=bool(periodo.is_atual),
            tipo_saldo=tipo,
            saldo_inicial=0,
            saldo_utilizado=0,
            saldo_reservado=0,
            saldo_disponivel=0,
            ultima_alteracao=dt.datetime.utcnow(),
            created_at=dt.datetime.utcnow(),
            updated_at=dt.datetime.utcnow(),
        )
        session.add(saldo)
        session.flush()
    else:
        saldo.colaborador_id = colab.id
        saldo.colaborador_matricula = colab.matricula
        saldo.data_inicio = periodo.data_inicio
        saldo.data_fim = periodo.data_fim
        saldo.is_atual = bool(periodo.is_atual)
        saldo.updated_at = dt.datetime.utcnow()
    return saldo


def _format_periodo_aquisitivo_origem(alloc: list[dict]) -> str:
    partes: list[str] = []
    for item in alloc or []:
        numero = int(item.get("periodo_numero") or item.get("numero") or 0)
        dias = float(item.get("dias") or 0)
        if numero <= 0 or dias <= 0:
            continue
        dias_txt = str(int(dias)) if dias.is_integer() else str(round(dias, 2)).rstrip("0").rstrip(".")
        partes.append(f"P{numero}:{dias_txt}")
    return " | ".join(partes)


def _parse_periodo_aquisitivo_origem(value: Any) -> list[dict]:
    """Lê textos como 'P4:10 | P5:10' e devolve alocações."""
    text = str(value or "").strip()
    if not text:
        return []
    found = re.findall(r"P\s*(\d+)\s*[:=]\s*([+-]?\d+(?:[\.,]\d+)?)", text, flags=re.IGNORECASE)
    out: list[dict] = []
    for numero, dias in found:
        try:
            out.append({"periodo_numero": int(numero), "dias": float(str(dias).replace(",", "."))})
        except Exception:
            continue
    return out


def _allocate_from_saldo_periodo(saldos: list[SaldoPeriodoNovo], dias: float, field: str) -> list[dict]:
    """Consome/reserva saldo do período mais antigo para o mais novo.

    V31: se a solicitação ultrapassar o saldo disponível, o restante fica no
    último período elegível, permitindo saldo negativo de forma controlada.
    """
    restante = abs(float(dias or 0))
    alloc: list[dict] = []
    if restante <= 0:
        return alloc

    elegiveis = [s for s in saldos if float(s.saldo_inicial or 0) != 0 or bool(s.is_atual)] or list(saldos)
    ultimo_elegivel = elegiveis[-1] if elegiveis else (saldos[-1] if saldos else None)

    for saldo in elegiveis:
        disponivel = float(saldo.saldo_disponivel or 0)
        if disponivel <= 0:
            continue
        consumir = min(disponivel, restante)
        if consumir <= 0:
            continue
        if field == "saldo_reservado":
            saldo.saldo_reservado = float(saldo.saldo_reservado or 0) + consumir
        else:
            saldo.saldo_utilizado = float(saldo.saldo_utilizado or 0) + consumir
        saldo.saldo_disponivel = float(saldo.saldo_disponivel or 0) - consumir
        saldo.ultima_alteracao = dt.datetime.utcnow()
        saldo.updated_at = dt.datetime.utcnow()
        alloc.append({"periodo_numero": int(saldo.periodo_numero or 0), "dias": consumir})
        restante -= consumir
        if restante <= 0.0001:
            break

    if restante > 0.0001 and ultimo_elegivel is not None:
        saldo = ultimo_elegivel
        if field == "saldo_reservado":
            saldo.saldo_reservado = float(saldo.saldo_reservado or 0) + restante
        else:
            saldo.saldo_utilizado = float(saldo.saldo_utilizado or 0) + restante
        saldo.saldo_disponivel = float(saldo.saldo_disponivel or 0) - restante
        saldo.ultima_alteracao = dt.datetime.utcnow()
        saldo.updated_at = dt.datetime.utcnow()
        alloc.append({"periodo_numero": int(saldo.periodo_numero or 0), "dias": restante})

    return alloc


def _apply_explicit_alloc_saldo_periodo(saldos_by_num: dict[int, SaldoPeriodoNovo], alloc: list[dict], field: str) -> list[dict]:
    aplicado: list[dict] = []
    for item in alloc or []:
        numero = int(item.get("periodo_numero") or item.get("numero") or 0)
        dias = abs(float(item.get("dias") or 0))
        saldo = saldos_by_num.get(numero)
        if not saldo or dias <= 0:
            continue
        if field == "saldo_reservado":
            saldo.saldo_reservado = float(saldo.saldo_reservado or 0) + dias
        else:
            saldo.saldo_utilizado = float(saldo.saldo_utilizado or 0) + dias
        saldo.saldo_disponivel = float(saldo.saldo_disponivel or 0) - dias
        saldo.ultima_alteracao = dt.datetime.utcnow()
        saldo.updated_at = dt.datetime.utcnow()
        aplicado.append({"periodo_numero": numero, "dias": dias})
    return aplicado


def _apply_adjustment_saldo_periodo(saldos_by_num: dict[int, SaldoPeriodoNovo], periodos: list[PeriodoAquisitivo], data_inicio: Optional[dt.date], dias: float, explicit_alloc: list[dict] | None = None) -> list[dict]:
    """Aplica AJUSTE como correção de saldo, positiva ou negativa.

    O sinal vem da própria solicitação: dias positivos creditam saldo;
    dias negativos debitam saldo. A coluna periodo_aquisitivo_origem guarda
    apenas a distribuição por período, no padrão P4:10 | P5:10.
    """
    delta_total = float(dias or 0)
    if abs(delta_total) <= 0.0001:
        return []

    sinal = 1 if delta_total >= 0 else -1
    alloc = explicit_alloc or []
    aplicado: list[dict] = []

    if alloc:
        for item in alloc:
            numero = int(item.get("periodo_numero") or item.get("numero") or 0)
            magnitude = abs(float(item.get("dias") or 0))
            saldo = saldos_by_num.get(numero)
            if not saldo or magnitude <= 0:
                continue
            delta = sinal * magnitude
            saldo.saldo_inicial = float(saldo.saldo_inicial or 0) + delta
            saldo.saldo_disponivel = float(saldo.saldo_disponivel or 0) + delta
            saldo.ultima_alteracao = dt.datetime.utcnow()
            saldo.updated_at = dt.datetime.utcnow()
            aplicado.append({"periodo_numero": numero, "dias": magnitude})
        return aplicado

    periodo = _find_periodo_for_date(periodos, data_inicio) or (periodos[-1] if periodos else None)
    if not periodo:
        return []
    saldo = saldos_by_num.get(int(periodo.periodo_numero or 0))
    if not saldo:
        return []
    magnitude = abs(delta_total)
    saldo.saldo_inicial = float(saldo.saldo_inicial or 0) + delta_total
    saldo.saldo_disponivel = float(saldo.saldo_disponivel or 0) + delta_total
    saldo.ultima_alteracao = dt.datetime.utcnow()
    saldo.updated_at = dt.datetime.utcnow()
    return [{"periodo_numero": int(periodo.periodo_numero or 0), "dias": magnitude}]


# Alias mantido por compatibilidade com versões anteriores do serviço.
def _credit_adjustment_saldo_periodo(saldos_by_num: dict[int, SaldoPeriodoNovo], periodos: list[PeriodoAquisitivo], data_inicio: Optional[dt.date], dias: float, explicit_alloc: list[dict] | None = None) -> list[dict]:
    return _apply_adjustment_saldo_periodo(saldos_by_num, periodos, data_inicio, dias, explicit_alloc)

def _ensure_periodos_colaborador(session, colab: Colaborador, ref_date: dt.date) -> list[PeriodoAquisitivo]:
    admissao = colab.data_admissao if isinstance(colab.data_admissao, dt.date) else parse_date(colab.data_admissao)
    if not admissao:
        return []

    existentes = {
        int(p.periodo_numero): p
        for p in session.query(PeriodoAquisitivo).filter(
            (PeriodoAquisitivo.colaborador_matricula == colab.matricula) | (PeriodoAquisitivo.colaborador_id == colab.id)
        ).all()
        if p.periodo_numero is not None
    }

    periodos: list[PeriodoAquisitivo] = []
    for numero, inicio, fim, is_atual in _iter_periodos_aquisitivos(admissao, ref_date) or []:
        periodo = existentes.get(numero)
        if not periodo:
            periodo = PeriodoAquisitivo(
                colaborador_id=colab.id,
                colaborador_matricula=colab.matricula,
                periodo_numero=numero,
                data_inicio=inicio,
                data_fim=fim,
                is_atual=is_atual,
            )
            session.add(periodo)
            session.flush()
            existentes[numero] = periodo
        else:
            periodo.colaborador_id = colab.id
            periodo.colaborador_matricula = colab.matricula
            periodo.data_inicio = inicio
            periodo.data_fim = fim
            periodo.is_atual = is_atual
        periodos.append(periodo)

    return sorted(periodos, key=lambda p: p.periodo_numero or 0)

def _canonical_status(value: Any) -> str:
    raw = normalize_text(value)
    # O banco novo usa enum com valores masculinos em algumas instalações
    # (APROVADO/CANCELADO/REPROVADO). A planilha antiga usa APROVADA/CANCELADA.
    # Mantemos compatibilidade normalizando para os valores mais aceitos pelo enum.
    if raw in {"APROVADA", "APROVADO", "APROVADAS", "APROVADOS"}:
        return "APROVADO"
    if raw in {"PENDENTE", "EM ANALISE", "EM ANÁLISE", "ANALISE", "ANÁLISE", "RESERVA", "RESERVADO"}:
        return "PENDENTE"
    if raw in {"CANCELADA", "CANCELADO", "CANCELADAS", "CANCELADOS"}:
        return "CANCELADO"
    if raw in {"REPROVADA", "REPROVADO", "REPROVADAS", "REPROVADOS"}:
        return "REPROVADO"
    return raw or "PENDENTE"



def _infer_saldo_tipo(explicit_tipo: Any, observacoes: Any = None, solicitacao: Any = None) -> str:
    tipo = normalize_text(explicit_tipo)
    obs = normalize_text(observacoes)
    sol = normalize_text(solicitacao)
    if "PREMIUM" in tipo or "CERTARIANA" in tipo or "PREMIUM" in obs or "CERTARIANA" in obs or "CERTARIANA" in sol:
        return "PREMIUM"
    return "REGULAR"


def _is_ajuste_solicitacao(value: Any) -> bool:
    return "AJUSTE" in normalize_text(value)


def _tipo_solicitacao_canonico(value: Any) -> str:
    raw = normalize_text(value)
    if "AJUSTE" in raw:
        return "AJUSTE"
    if "VENDA" in raw or "ABONO" in raw:
        return "VENDA"
    if "MATERNIDADE" in raw:
        return "LICENCA_MATERNIDADE"
    if "PATERNIDADE" in raw:
        return "LICENCA_PATERNIDADE"
    if raw:
        return raw[:50]
    return "GOZO"


def _sync_solicitacoes(session, solicitacoes: SheetMaps, solicitacoes_sheet_id: int) -> dict:
    """Sincroniza a folha histórica de solicitações do Smartsheet em lotes."""
    c_colab = col_id(solicitacoes.columns, "COLABORADOR", "EMAIL", "EMAIL DO COLABORADOR", "EMAIL DA EMPRESA")
    c_gestor = col_id(solicitacoes.columns, "GESTOR SOLICITANTE")
    c_criado_por = col_id(solicitacoes.columns, "CRIADO_POR", "CRIADO POR", "CRIADO")
    c_solic = col_id(solicitacoes.columns, "SOLICITACAO", "SOLICITAÇÃO")
    c_periodo = col_id(solicitacoes.columns, "PERIODO_AQUISITIVO", "PERIODO AQUISITIVO", "PERÍODO AQUISITIVO")
    c_tipo = col_id(solicitacoes.columns, "SALDO TIPO", "TIPO SALDO", "TIPO_DE_SALDO", "TIPO DE SALDO")
    c_inicio = col_id(solicitacoes.columns, "DATA INICIO", "DATA INÍCIO", "DATA INICIAL", "INICIO", "INÍCIO")
    c_fim = col_id(solicitacoes.columns, "DATA FIM", "DATA FINAL", "FIM")
    c_dias = col_id(solicitacoes.columns, "DIAS", "DIAS (GOZO)")
    c_status = col_id(solicitacoes.columns, "STATUS")
    c_obs = col_id(solicitacoes.columns, "OBSERVACOES", "OBSERVAÇÕES", "OBSERVACAO", "OBSERVAÇÃO")

    sheet_id_str = str(solicitacoes_sheet_id)
    inserted = updated = skipped = sem_colaborador = 0
    batch_size = int(os.getenv("SYNC_SOLICITACOES_BATCH_SIZE", "25") or "25")
    total = len(solicitacoes.rows)
    started_progress = time.monotonic()
    _log_progress("solicitacoes", 0, total, started_progress, "importando historico")

    for idx, row in enumerate(solicitacoes.rows, start=1):
        if idx == 1 or idx % batch_size == 0 or idx == total:
            _log_progress("solicitacoes", idx, total, started_progress, "importando historico")

        row_id_str = str(getattr(row, "id", "") or "")
        colaborador_email = safe_lower(cell_value(row, c_colab))
        if not colaborador_email:
            skipped += 1
            continue
        gestor_email = safe_lower(cell_value(row, c_gestor))
        criado_por = safe_lower(cell_value(row, c_criado_por))
        solicitacao_txt = str(cell_value(row, c_solic) or "").strip()
        explicit_tipo = cell_value(row, c_tipo)
        observacoes = cell_value(row, c_obs)
        saldo_tipo = _infer_saldo_tipo(explicit_tipo, observacoes, solicitacao_txt)
        data_inicio = parse_date(cell_value(row, c_inicio))
        data_fim = parse_date(cell_value(row, c_fim)) or data_inicio
        dias = parse_int(cell_value(row, c_dias), 0)
        if not dias and data_inicio and data_fim:
            dias = (data_fim - data_inicio).days + 1
        status = _canonical_status(cell_value(row, c_status))
        payload = row_as_dict(row, solicitacoes.columns)
        periodo_raw = cell_value(row, c_periodo)

        if not data_inicio:
            skipped += 1
            continue

        with session.no_autoflush:
            colab = _resolve_colaborador_by_email(session, colaborador_email, only_active=True)
            solicitante = _resolve_colaborador_by_email(session, gestor_email or criado_por, only_active=True)
        if not colab:
            sem_colaborador += 1

        existing = session.query(Solicitacao).filter(
            Solicitacao.origem_sheet_id == sheet_id_str,
            Solicitacao.smartsheet_row_id == row_id_str,
        ).first()
        if not existing:
            existing = Solicitacao(
                origem_sheet_id=sheet_id_str,
                smartsheet_row_id=row_id_str,
                created_at=dt.datetime.utcnow(),
            )
            session.add(existing)
            inserted += 1
        else:
            updated += 1

        existing.colaborador_id = colab.id if colab else None
        existing.colaborador_matricula = colab.matricula if colab else None
        existing.solicitante_id = solicitante.id if solicitante else None
        existing.solicitante_matricula = solicitante.matricula if solicitante else None
        existing.colaborador_email = colaborador_email
        existing.gestor_solicitante_email = gestor_email or None
        existing.criado_por = criado_por or None
        existing.solicitacao = solicitacao_txt or _tipo_solicitacao_canonico(solicitacao_txt)
        existing.tipo_solicitacao = _tipo_solicitacao_canonico(solicitacao_txt)
        existing.tipo_ferias = saldo_tipo
        existing.saldo_tipo = saldo_tipo
        existing.data_inicio = data_inicio
        existing.data_fim = data_fim
        existing.dias = int(dias or 0)
        existing.dias_solicitados = float(dias or 0)
        existing.status = status
        existing.observacoes = str(observacoes or "").strip() or None
        existing.is_ajuste = _is_ajuste_solicitacao(solicitacao_txt)
        existing.metadata_json = {"explicit_tipo": explicit_tipo, "periodo_aquisitivo": periodo_raw}
        existing.periodo_aquisitivo_origem = str(periodo_raw or "").strip() or None
        existing.raw_payload = payload
        existing.source_created_at = getattr(row, "created_at", None)
        existing.source_modified_at = getattr(row, "modified_at", None)
        existing.updated_at = dt.datetime.utcnow()

        if idx % batch_size == 0 or idx == total:
            try:
                session.commit()
                _log_progress("solicitacoes", idx, total, started_progress, "lote gravado")
            except Exception:
                session.rollback()
                raise

    return {
        "solicitacoes_records": total,
        "solicitacoes_inserted": inserted,
        "solicitacoes_updated": updated,
        "solicitacoes_skipped": skipped,
        "solicitacoes_sem_colaborador": sem_colaborador,
    }

def _recalculate_complemento(session) -> dict:
    """Recalcula saldo_periodo e preenche periodo_aquisitivo_origem.

    V29: solicitacoes_ferias é a fonte oficial dos eventos. A tabela
    saldo_periodo é a fonte oficial do saldo vivo por matrícula/período/tipo.
    auditoria_saldos não é alimentada por esta rotina para evitar duplicidade.
    """
    ref_date = _reference_date()
    colaboradores = session.query(Colaborador).order_by(Colaborador.id).all()
    solicitacoes = session.query(Solicitacao).order_by(Solicitacao.data_inicio.asc(), Solicitacao.id.asc()).all()

    by_matricula: dict[str, list[Solicitacao]] = {}
    by_email: dict[str, list[Solicitacao]] = {}
    for sol in solicitacoes:
        if sol.colaborador_matricula:
            by_matricula.setdefault(normalize_matricula(sol.colaborador_matricula), []).append(sol)
        if sol.colaborador_email:
            by_email.setdefault(safe_lower(sol.colaborador_email), []).append(sol)

    recalculated = periodos_synced = saldo_periodo_synced = origem_preenchida = 0
    started_progress = time.monotonic()
    total = len(colaboradores)
    batch_size = int(os.getenv("SYNC_RECALC_BATCH_SIZE", "25") or "25")
    _log_progress("saldos", 0, total, started_progress, f"recalculando saldo_periodo ate {ref_date.isoformat()}")

    for idx, colab in enumerate(colaboradores, start=1):
        if idx == 1 or idx % batch_size == 0 or idx == total:
            _log_progress("saldos", idx, total, started_progress, "saldo_periodo e origem das solicitacoes")

        admissao = colab.data_admissao if isinstance(colab.data_admissao, dt.date) else parse_date(colab.data_admissao)
        periodos = _ensure_periodos_colaborador(session, colab, ref_date)
        periodos_synced += len(periodos)

        completed_count = sum(1 for p in periodos if p.data_fim <= ref_date)
        regular_base_total = completed_count * 30 if admissao else int(colab.dias_direito or 0)
        premium_base_total, premium_ini, premium_fim_excl = premium_window(admissao, ref_date)

        colab.dias_direito = int(regular_base_total or 0)
        colab.updated_at = dt.datetime.utcnow()

        saldos_regular: list[SaldoPeriodoNovo] = []
        saldos_premium: list[SaldoPeriodoNovo] = []
        saldos_regular_by_num: dict[int, SaldoPeriodoNovo] = {}
        saldos_premium_by_num: dict[int, SaldoPeriodoNovo] = {}

        for periodo in periodos:
            numero = int(periodo.periodo_numero or 0)
            regular = _get_or_create_saldo_periodo_novo(session, colab, periodo, "REGULAR")
            regular.saldo_inicial = 30 if periodo.data_fim <= ref_date else 0
            regular.saldo_utilizado = 0
            regular.saldo_reservado = 0
            regular.saldo_disponivel = float(regular.saldo_inicial or 0)
            regular.ultima_alteracao = dt.datetime.utcnow()
            regular.updated_at = dt.datetime.utcnow()
            saldos_regular.append(regular)
            saldos_regular_by_num[numero] = regular
            saldo_periodo_synced += 1

            premium = _get_or_create_saldo_periodo_novo(session, colab, periodo, "PREMIUM")
            premium.saldo_inicial = 0
            if premium_base_total and premium_ini and periodo.data_inicio <= premium_ini <= periodo.data_fim:
                premium.saldo_inicial = premium_base_total
            premium.saldo_utilizado = 0
            premium.saldo_reservado = 0
            premium.saldo_disponivel = float(premium.saldo_inicial or 0)
            premium.ultima_alteracao = dt.datetime.utcnow()
            premium.updated_at = dt.datetime.utcnow()
            saldos_premium.append(premium)
            saldos_premium_by_num[numero] = premium
            saldo_periodo_synced += 1

        rows = list(by_matricula.get(normalize_matricula(colab.matricula), []))
        if not rows and colab.email:
            rows = list(by_email.get(safe_lower(colab.email), []))

        # Ajustes primeiro: eles representam créditos/correções de base.
        ajustes = [r for r in rows if bool(r.is_ajuste)]
        movimentos = [r for r in rows if not bool(r.is_ajuste)]
        rows_ordenadas = sorted(ajustes, key=lambda x: (x.data_inicio or dt.date.min, x.id or 0)) + sorted(movimentos, key=lambda x: (x.data_inicio or dt.date.min, x.id or 0))

        for sol in rows_ordenadas:
            dias = float(sol.dias if sol.dias is not None else (sol.dias_solicitados or 0))
            if abs(dias) <= 0.0001:
                continue
            saldo_tipo = (sol.saldo_tipo or sol.tipo_ferias or "REGULAR").upper()
            status = _canonical_status(sol.status)
            solicitacao_norm = normalize_text(sol.solicitacao or sol.tipo_solicitacao)
            data_inicio = sol.data_inicio if isinstance(sol.data_inicio, dt.date) else parse_date(sol.data_inicio)

            if "LICENCA MATERNIDADE" in solicitacao_norm or "LICENCA PATERNIDADE" in solicitacao_norm:
                continue

            # Origem pré-existente do Smartsheet/metadata tem prioridade para preservar histórico.
            origem_atual = (getattr(sol, "periodo_aquisitivo_origem", None) or "").strip()
            metadata = sol.metadata_json if isinstance(sol.metadata_json, dict) else {}
            origem_meta = str(metadata.get("periodo_aquisitivo") or "").strip()
            explicit_alloc = _parse_periodo_aquisitivo_origem(origem_atual or origem_meta)

            saldos = saldos_premium if saldo_tipo == "PREMIUM" else saldos_regular
            saldos_by_num = saldos_premium_by_num if saldo_tipo == "PREMIUM" else saldos_regular_by_num
            alloc: list[dict] = []

            if bool(sol.is_ajuste):
                if status in {"APROVADA", "APROVADO"}:
                    alloc = _apply_adjustment_saldo_periodo(saldos_by_num, periodos, data_inicio, dias, explicit_alloc)
                else:
                    alloc = explicit_alloc
            else:
                dias = abs(dias)
                if saldo_tipo == "PREMIUM" and premium_ini and premium_fim_excl and data_inicio and not (premium_ini <= data_inicio < premium_fim_excl):
                    # Fora da janela premium: preserva origem se existir, mas não impacta saldo premium.
                    alloc = explicit_alloc
                elif status in {"APROVADA", "APROVADO"}:
                    alloc = _apply_explicit_alloc_saldo_periodo(saldos_by_num, explicit_alloc, "saldo_utilizado") if explicit_alloc else _allocate_from_saldo_periodo(saldos, dias, "saldo_utilizado")
                elif status in {"RESERVA", "RESERVADO", "PENDENTE"}:
                    alloc = _apply_explicit_alloc_saldo_periodo(saldos_by_num, explicit_alloc, "saldo_reservado") if explicit_alloc else _allocate_from_saldo_periodo(saldos, dias, "saldo_reservado")
                else:
                    alloc = explicit_alloc

            origem_formatada = _format_periodo_aquisitivo_origem(alloc)
            if origem_formatada and origem_formatada != origem_atual:
                sol.periodo_aquisitivo_origem = origem_formatada
                sol.updated_at = dt.datetime.utcnow()
                origem_preenchida += 1
            elif not origem_atual and origem_meta:
                sol.periodo_aquisitivo_origem = origem_meta
                sol.updated_at = dt.datetime.utcnow()
                origem_preenchida += 1

        comp = colab.complemento
        if not comp:
            comp = ColaboradorComplemento(colaborador_id=colab.id, colaborador_matricula=colab.matricula, user_type="USER", ativo_no_app=True)
            session.add(comp)
        comp.colaborador_matricula = colab.matricula
        # V43: saldos consolidados e total de solicitações não são mais gravados
        # em colaborador_complemento. A fonte oficial é saldo_periodo.
        comp.calculated_at = dt.datetime.utcnow()
        comp.updated_at = dt.datetime.utcnow()
        recalculated += 1

        if idx % batch_size == 0 or idx == total:
            try:
                session.commit()
                _log_progress("saldos", idx, total, started_progress, "lote gravado")
            except Exception:
                session.rollback()
                raise

    return {
        "recalculated": recalculated,
        "periodos_synced": periodos_synced,
        "saldo_periodo_synced": saldo_periodo_synced,
        "periodo_aquisitivo_origem_preenchido": origem_preenchida,
        "saldo_reference_date": ref_date.isoformat(),
    }


def recalcular_saldo_periodo_from_db() -> dict:
    """Recalcula somente saldo_periodo e periodo_aquisitivo_origem usando o BD atual."""
    with get_session() as session:
        return _recalculate_complemento(session)


def sync_cadastro_from_smartsheet(triggered_by: str = "manual", actor_email: str = "", recalculate: bool = False, include_solicitacoes: bool = False) -> dict:
    settings = get_settings()
    if not settings.access_token:
        raise ValueError("SMARTSHEET_ACCESS_TOKEN não configurado no Render.")
    sheet_id = int(settings.id_folha_cadastro or 3609445264215940)

    client = smartsheet.Smartsheet(settings.access_token)
    client.errors_as_exceptions(True)

    started = dt.datetime.utcnow()
    _log_progress("inicio", message=f"baixando cadastro Smartsheet {sheet_id}")

    # Baixa as planilhas antes de abrir transação com PostgreSQL.
    # Isso evita conexão/transaction idle por vários minutos enquanto o Smartsheet responde.
    cadastro = get_sheet(client, sheet_id)
    _log_progress("smartsheet", message=f"cadastro baixado: {len(cadastro.rows)} linha(s)")

    solicitacoes_sheet_id = int(settings.id_folha_solicitacoes or 0)
    solicitacoes_sheet = None
    if include_solicitacoes and solicitacoes_sheet_id:
        _log_progress("smartsheet", message=f"baixando solicitacoes {solicitacoes_sheet_id}")
        solicitacoes_sheet = get_sheet(client, solicitacoes_sheet_id)
        _log_progress("smartsheet", message=f"solicitacoes baixadas: {len(solicitacoes_sheet.rows)} linha(s)")

    # Deduplica/processa a planilha antes de abrir uma conexão/transação
    # com o PostgreSQL. Na execução local pela External Database URL do Render,
    # esse processamento pode deixar a conexão SSL ociosa por tempo suficiente
    # para o servidor fechá-la, gerando "SSL connection has been closed unexpectedly".
    _log_progress("preprocessamento", message="deduplicando cadastro por matricula")
    cadastro_precomputed = deduplicate_colaboradores(cadastro)
    _log_progress("preprocessamento", message=f"{len(cadastro_precomputed[0])} registro(s) apos deduplicacao")

    # Marca execução iniciada usando uma sessão curta e fecha tudo em seguida.
    try:
        with get_session() as session:
            _mark_sync(session, "cadastro", "running", extra={
                "triggered_by": triggered_by,
                "actor_email": actor_email,
                "sheet_id": sheet_id,
                "progress_message": "Iniciando sincronização",
                "progress_percent": 0,
            })
            session.commit()
    finally:
        dispose_engine()

    result = {}
    solicitacoes_result = {}
    recalc_result = {"recalculated": 0}
    extra = {}

    try:
        # Transação principal: grava dados. O status final é marcado depois,
        # em sessão nova, para uma falha no sync_state não desfazer a carga.
        _log_progress("postgres", message="abrindo transacao principal")
        with get_session() as session:
            result = _sync_colaboradores(session, cadastro, sheet_id, precomputed=cadastro_precomputed)
            session.flush()

            if include_solicitacoes and solicitacoes_sheet is not None and solicitacoes_sheet_id:
                _log_progress("solicitacoes", message="sincronizando folha historica")
                solicitacoes_result = _sync_solicitacoes(session, solicitacoes_sheet, solicitacoes_sheet_id)

            if recalculate:
                _log_progress("saldos", message="recalculando complementos")
                recalc_result = _recalculate_complemento(session)

            finished = dt.datetime.utcnow()
            extra = {
                **result,
                **solicitacoes_result,
                **recalc_result,
                "triggered_by": triggered_by,
                "actor_email": actor_email,
                "sheet_id": sheet_id,
                "started_at": started.isoformat(),
                "finished_at": finished.isoformat(),
            }
            try:
                session.add(Auditoria(
                    actor_email=safe_lower(actor_email or triggered_by),
                    action="SYNC_CADASTRO_SMARTSHEET",
                    entity_type="sync_state",
                    entity_id=0,
                    before_data=None,
                    after_data=extra,
                    context={"triggered_by": triggered_by, "sheet_id": sheet_id},
                ))
            except Exception:
                pass
            session.commit()

        dispose_engine()

        # Sessão curta apenas para status final. Se falhar, os dados já foram gravados.
        try:
            with get_session() as status_session:
                _mark_sync(status_session, "cadastro", "success", success=True, extra={
                    **extra,
                    "progress_message": "Sincronização concluída",
                    "progress_percent": 100,
                })
                if include_solicitacoes and solicitacoes_result:
                    _mark_sync(status_session, "solicitacoes", "success", success=True, extra={"sheet_id": solicitacoes_sheet_id, **solicitacoes_result})
                if recalculate:
                    _mark_sync(status_session, "saldos", "success", success=True, extra=recalc_result)
                status_session.commit()
        except Exception:
            log.exception("Dados sincronizados, mas houve falha ao gravar sync_state final.")

        log.info("Sincronização de cadastro concluída: %s", extra)
        _log_progress("fim", message="sincronizacao concluida")
        return {"ok": True, **extra}

    except Exception as exc:
        err = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        try:
            dispose_engine()
            with get_session() as status_session:
                _mark_sync(status_session, "cadastro", "error", error=err[:4000], success=False, extra={
                    "triggered_by": triggered_by,
                    "actor_email": actor_email,
                    "sheet_id": sheet_id,
                    "include_solicitacoes": include_solicitacoes,
                    "progress_message": "Erro na sincronização",
                    "progress_percent": None,
                })
                status_session.commit()
        except Exception:
            log.exception("Falha ao registrar erro de sincronização no sync_state.")
        log.exception("Falha na sincronização de cadastro")
        raise

def get_sync_states() -> dict:
    with get_session() as session:
        rows = session.query(SyncState).order_by(SyncState.sync_name.asc()).all()
        out = []
        for row in rows:
            out.append({
                "sync_name": row.sync_name,
                "last_started_at": row.last_started_at.isoformat() if row.last_started_at else None,
                "last_finished_at": row.last_finished_at.isoformat() if row.last_finished_at else None,
                "last_success_at": row.last_success_at.isoformat() if row.last_success_at else None,
                "last_status": row.last_status,
                "last_error": row.last_error,
                "extra": row.extra or {},
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            })
        return {"ok": True, "states": out}
