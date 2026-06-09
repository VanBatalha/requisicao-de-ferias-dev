"""Sincronização do cadastro Smartsheet -> PostgreSQL.

Baseado no script de migração/sincronização usado pelo projeto, mas adaptado
para rodar dentro do app Flask/Render e ser acionado pelo Painel Admin ou por
um Render Cron Job.
"""
from __future__ import annotations

import calendar
import datetime as dt
import json
import traceback
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional

import smartsheet
from sqlalchemy import func

from ..config import get_settings
from ..logging_config import get_logger
from ..models import Auditoria, Colaborador, ColaboradorComplemento, Solicitacao, SyncState
from .postgres_service import get_session

log = get_logger(__name__)

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


def parse_date(value: Any) -> Optional[dt.date]:
    if value in (None, ""):
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%m/%d/%y", "%m/%d/%Y"):
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
        matricula=str(cell_value(row, c_matricula) or "").strip(),
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
    by_email: dict[str, ColaboradorRecord] = {}
    duplicates: dict[str, list[int]] = {}

    for row in cadastro.rows:
        record = build_colaborador_record(row, cadastro)
        if not record:
            continue
        if record.email not in by_email:
            by_email[record.email] = record
            continue
        duplicates.setdefault(record.email, [by_email[record.email].row_id])
        duplicates[record.email].append(record.row_id)
        by_email[record.email] = choose_better_record(by_email[record.email], record)

    return list(by_email.values()), duplicates


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


def _sync_colaboradores(session, cadastro: SheetMaps, cadastro_sheet_id: int) -> dict:
    records, duplicates = deduplicate_colaboradores(cadastro)
    inserted = updated = complemento_inserted = complemento_updated = skipped = 0

    for record in records:
        if not record.email:
            skipped += 1
            continue

        colab = session.query(Colaborador).filter(func.lower(Colaborador.email) == record.email.lower()).first()
        payload = dict(record.payload)
        if record.matricula:
            payload.setdefault("__matricula_escolhida__", record.matricula)

        is_new = colab is None
        if is_new:
            colab = Colaborador(email=record.email, dias_direito=int(record.dias_direito or 0))
            session.add(colab)
            inserted += 1
        else:
            updated += 1

        # Para colaboradores existentes, não apaga dados corrigidos manualmente quando
        # a célula do Smartsheet vier vazia ou quando a coluna não tiver sido encontrada.
        colab.email = record.email
        colab.nome_completo = clean_optional(coalesce_sheet_value(record.nome, None if is_new else colab.nome_completo))
        colab.status = clean_optional(coalesce_sheet_value(record.status, None if is_new else colab.status))
        colab.data_admissao = coalesce_sheet_value(record.admissao, None if is_new else colab.data_admissao)
        colab.setor = clean_optional(coalesce_sheet_value(record.setor, None if is_new else colab.setor))
        colab.cargo = clean_optional(coalesce_sheet_value(record.cargo, None if is_new else colab.cargo))
        colab.regime = clean_optional(coalesce_sheet_value(record.regime, None if is_new else colab.regime))
        incoming_dias = int(record.dias_direito or 0)
        if is_new or incoming_dias > 0 or colab.dias_direito is None:
            colab.dias_direito = incoming_dias
        else:
            colab.dias_direito = int(colab.dias_direito or 0)
        colab.origem_sheet_id = str(cadastro_sheet_id)
        colab.origem_row_id = str(record.row_id)
        colab.raw_payload = payload
        colab.updated_at = dt.datetime.utcnow()

        # Agora o flush acontece somente depois de preencher dias_direito. Isso evita
        # violar o NOT NULL da coluna em bases antigas do Render.
        session.flush()

        comp = colab.complemento
        if comp:
            complemento_updated += 1
        else:
            comp = ColaboradorComplemento(colaborador_id=colab.id)
            session.add(comp)
            complemento_inserted += 1

        if record.user_type:
            comp.user_type = record.user_type
        elif not comp.user_type:
            comp.user_type = "USER"
        comp.gestor_direto_email = clean_optional(coalesce_sheet_value(record.gestor_direto, comp.gestor_direto_email))
        comp.gestor_superior_email = clean_optional(coalesce_sheet_value(record.gestor_superior, comp.gestor_superior_email))
        comp.ativo_no_app = bool(record.ativo_no_app)
        comp.origem_sheet_id = str(cadastro_sheet_id)
        comp.origem_row_id = str(record.row_id)
        comp.updated_at = dt.datetime.utcnow()

    return {
        "records": len(records),
        "inserted": inserted,
        "updated": updated,
        "complemento_inserted": complemento_inserted,
        "complemento_updated": complemento_updated,
        "skipped": skipped,
        "duplicates": {email: sorted(set(rows)) for email, rows in duplicates.items()},
    }


def _canonical_status(value: Any) -> str:
    raw = normalize_text(value)
    if raw in {"APROVADA", "APROVADO", "APROVADAS"}:
        return "APROVADA"
    if raw in {"PENDENTE", "EM ANALISE", "EM ANÁLISE", "ANALISE", "ANÁLISE", "RESERVA", "RESERVADO"}:
        return "RESERVA"
    if raw in {"CANCELADA", "CANCELADO", "REPROVADA", "REPROVADO"}:
        return raw
    return raw or "PENDENTE"


def _recalculate_complemento(session) -> dict:
    colaboradores = session.query(Colaborador).order_by(Colaborador.id).all()
    recalculated = 0

    for colab in colaboradores:
        admissao = colab.data_admissao if isinstance(colab.data_admissao, dt.date) else parse_date(colab.data_admissao)
        regular_base = completed_aquisitive_periods(admissao) * 30 if admissao else int(colab.dias_direito or 0)
        premium_base, premium_ini, premium_fim_excl = premium_window(admissao)
        rows = session.query(Solicitacao).filter(func.lower(Solicitacao.colaborador_email) == (colab.email or "").lower()).all()

        regular_usados = regular_reservados = premium_usados = premium_reservados = 0
        ajuste_regular = ajuste_premium = total_solicitacoes = 0

        for sol in rows:
            dias = int(sol.dias or 0)
            saldo_tipo = (sol.saldo_tipo or "REGULAR").upper()
            status = _canonical_status(sol.status)
            solicitacao_norm = normalize_text(sol.solicitacao)
            data_inicio = sol.data_inicio if isinstance(sol.data_inicio, dt.date) else parse_date(sol.data_inicio)

            if bool(sol.is_ajuste):
                if status == "APROVADA":
                    if saldo_tipo == "PREMIUM":
                        ajuste_premium += dias
                    else:
                        ajuste_regular += dias
                continue

            if "LICENCA MATERNIDADE" in solicitacao_norm or "LICENCA PATERNIDADE" in solicitacao_norm:
                continue

            total_solicitacoes += 1
            if saldo_tipo == "PREMIUM":
                if premium_ini and premium_fim_excl and data_inicio and not (premium_ini <= data_inicio < premium_fim_excl):
                    continue
                if status == "APROVADA":
                    premium_usados += dias
                elif status == "RESERVA":
                    premium_reservados += dias
            else:
                if status == "APROVADA":
                    regular_usados += dias
                elif status == "RESERVA":
                    regular_reservados += dias

        regular_direito = max(0, regular_base + ajuste_regular)
        premium_direito = max(0, premium_base + ajuste_premium)
        regular_disponivel = max(0, regular_direito - regular_usados - regular_reservados)
        premium_disponivel = max(0, premium_direito - premium_usados - premium_reservados)
        periodo_atual = current_partial_period(admissao)

        comp = colab.complemento
        if not comp:
            comp = ColaboradorComplemento(colaborador_id=colab.id, user_type="USER", ativo_no_app=True)
            session.add(comp)

        comp.saldo_regular_direito = regular_direito
        comp.saldo_regular_usado = regular_usados
        comp.saldo_regular_reservado = regular_reservados
        comp.saldo_regular_disponivel = regular_disponivel
        comp.saldo_premium_direito = premium_direito
        comp.saldo_premium_usado = premium_usados
        comp.saldo_premium_reservado = premium_reservados
        comp.saldo_premium_disponivel = premium_disponivel
        comp.total_solicitacoes = total_solicitacoes
        comp.periodo_aquisitivo_atual = periodo_atual or {}
        comp.calculated_at = dt.datetime.utcnow()
        comp.updated_at = dt.datetime.utcnow()
        recalculated += 1

    return {"recalculated": recalculated}


def sync_cadastro_from_smartsheet(triggered_by: str = "manual", actor_email: str = "", recalculate: bool = True) -> dict:
    settings = get_settings()
    if not settings.access_token:
        raise ValueError("SMARTSHEET_ACCESS_TOKEN não configurado no Render.")
    sheet_id = int(settings.id_folha_cadastro or 3609445264215940)

    client = smartsheet.Smartsheet(settings.access_token)
    client.errors_as_exceptions(True)

    started = dt.datetime.utcnow()
    with get_session() as session:
        _mark_sync(session, "cadastro", "running", extra={"triggered_by": triggered_by, "actor_email": actor_email, "sheet_id": sheet_id})
        session.commit()
        try:
            cadastro = get_sheet(client, sheet_id)
            result = _sync_colaboradores(session, cadastro, sheet_id)
            recalc_result = _recalculate_complemento(session) if recalculate else {"recalculated": 0}
            finished = dt.datetime.utcnow()
            extra = {
                **result,
                **recalc_result,
                "triggered_by": triggered_by,
                "actor_email": actor_email,
                "sheet_id": sheet_id,
                "started_at": started.isoformat(),
                "finished_at": finished.isoformat(),
            }
            _mark_sync(session, "cadastro", "success", success=True, extra=extra)
            if recalculate:
                _mark_sync(session, "saldos", "success", success=True, extra=recalc_result)
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
            log.info("Sincronização de cadastro concluída: %s", extra)
            return {"ok": True, **extra}
        except Exception as exc:
            err = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            # Depois de erro em flush/commit, a Session fica bloqueada até rollback().
            # Sem isso, a tela mostra apenas o erro genérico de transação já revertida.
            session.rollback()
            try:
                _mark_sync(session, "cadastro", "error", error=err[:4000], success=False, extra={"triggered_by": triggered_by, "actor_email": actor_email, "sheet_id": sheet_id})
                session.commit()
            except Exception:
                session.rollback()
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
