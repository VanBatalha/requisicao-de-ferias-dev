"""Sincronização do cadastro principal Smartsheet -> PostgreSQL.

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
import threading
import traceback
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional

import smartsheet
from sqlalchemy import func, or_, select

from ..config import get_settings
from ..logging_config import get_logger
from ..models import Auditoria, Colaborador, ColaboradorComplemento, Solicitacao, SyncState, PermissaoUsuario, HierarquiaGestao, SaldoPeriodoNovo
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
STATUS_INVALIDO_SYNC_SET = {"#NO MATCH", "NO MATCH", "#N/A", "N/A"}


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
    unidade: Any
    empresa: Any
    telefone: Any
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
    """Normaliza a matricula como codigo externo do cadastro."""
    return str(value or "").strip().upper()


def normalize_ref_matricula_ou_marcador(value: Any, allow_dp: bool = True, allow_gestor: bool = True) -> str:
    """Normaliza uma referencia de gestor sem usar e-mail como chave.

    Aceita:
    - MAT00000;
    - numero final da matricula, convertido para MAT com 5 digitos;
    - marcadores DP/GESTOR quando permitidos.

    Qualquer e-mail retorna vazio para impedir relacionamento por e-mail.
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    norm = normalize_text(raw)
    if "@" in raw:
        return ""
    if allow_dp and norm in {"DP", "RH", "DEPARTAMENTO PESSOAL"}:
        return "DP"
    if allow_gestor and norm in {"GESTOR", "GESTORES", "GESTOR DIRETO"}:
        return "GESTOR"
    mat = normalize_matricula(raw)
    if re.fullmatch(r"MAT\d+", mat):
        return mat
    if re.fullmatch(r"\d+", mat):
        return f"MAT{int(mat):05d}"
    return ""


def _active_email_to_matricula_map(records: list[ColaboradorRecord]) -> tuple[dict[str, str], set[str]]:
    """Mapa auxiliar para converter contatos do Smartsheet em matrícula.

    Algumas colunas GESTOR DIRETO/SUPERIOR da planilha vêm como contato/e-mail.
    A aplicação não guarda e-mail como vínculo operacional; na sincronização,
    quando o e-mail identifica de forma inequívoca uma matrícula ATIVA da própria
    planilha, ele é convertido para matrícula. E-mails ambíguos são ignorados.
    """
    by_email: dict[str, str] = {}
    ambiguous: set[str] = set()
    for record in records:
        email = safe_lower(record.email)
        if not email or normalize_text(record.status) not in STATUS_ATIVO_SET:
            continue
        mat = normalize_matricula(record.matricula)
        if not mat:
            continue
        if email in by_email and by_email[email] != mat:
            ambiguous.add(email)
        else:
            by_email[email] = mat
    for email in ambiguous:
        by_email.pop(email, None)
    return by_email, ambiguous


def _resolve_gestor_ref(value: Any, email_to_matricula: dict[str, str] | None = None, allow_dp: bool = True, allow_gestor: bool = True) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if "@" in raw:
        return (email_to_matricula or {}).get(safe_lower(raw), "")
    return normalize_ref_matricula_ou_marcador(raw, allow_dp=allow_dp, allow_gestor=allow_gestor)


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
    """Compatibilidade: devolve somente o ultimo periodo anual concluido."""
    if not admissao:
        return None
    hoje = hoje or dt.date.today()
    completos = completed_aquisitive_periods(admissao, hoje)
    if completos <= 0:
        return None
    inicio = add_months(admissao, (completos - 1) * 12)
    fim = add_months(admissao, completos * 12) - dt.timedelta(days=1)
    return {
        "numero": completos,
        "inicio": inicio.isoformat(),
        "fim": fim.isoformat(),
        "label": f"Período {completos} — {inicio.strftime('%d/%m/%Y')} a {fim.strftime('%d/%m/%Y')}",
    }


def premium_window(admissao: Optional[dt.date], hoje: Optional[dt.date] = None) -> tuple[int, Optional[dt.date], Optional[dt.date]]:
    """Compatibilidade com a regra V54: 30 dias em P1 e 15 a cada 30 meses."""
    if not admissao:
        return 0, None, None
    hoje = hoje or dt.date.today()
    primeiro_credito = add_months(admissao, 60) + dt.timedelta(days=1)
    if hoje < primeiro_credito:
        return 0, None, None
    numero = 1
    credito = primeiro_credito
    proximo = add_months(primeiro_credito, 30)
    while hoje >= proximo:
        numero += 1
        credito = proximo
        proximo = add_months(primeiro_credito, numero * 30)
    return (30 if numero == 1 else 15), credito, proximo


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
    """Monta o registro a partir da planilha CADASTRO DE COLABORADORES.

    A fonte oficial atual é a folha 1745799836133252. Permissões (USER TYPE)
    e saldos não são mais lidos do Smartsheet: ficam em permissoes_usuario e
    saldo_periodo, respectivamente. Novo colaborador recebe USER apenas se não
    existir permissão gravada no PostgreSQL.
    """
    c_email = col_id(cadastro.columns, "E-MAIL EMPRESA", "EMAIL EMPRESA", "EMAIL DA EMPRESA", "E-MAIL DA EMPRESA", "EMAIL", "E-MAIL")
    c_nome = col_id(cadastro.columns, "NOME COMPLETO", "NOME SE ATIVO", "NOME", "COLABORADOR", "NOME DO COLABORADOR", "FUNCIONARIO", "FUNCIONÁRIO")
    c_status = col_id(cadastro.columns, "STATUS", "SITUACAO", "SITUAÇÃO")
    c_adm = col_id(cadastro.columns, "ADMISSAO", "ADMISSÃO", "DATA DE ADMISSAO", "DATA DE ADMISSÃO", "DATA ADMISSAO", "DATA ADMISSÃO")
    c_setor = col_id(cadastro.columns, "SETOR", "AREA", "ÁREA", "DEPARTAMENTO", "CENTRO DE CUSTO")
    c_cargo = col_id(cadastro.columns, "CARGO", "FUNCAO", "FUNÇÃO", "FUNCAO/CARGO", "FUNÇÃO/CARGO")
    c_regime = col_id(cadastro.columns, "REGIME", "REGIME DE CONTRATACAO", "REGIME DE CONTRATAÇÃO")
    c_unidade = col_id(cadastro.columns, "UNIDADE")
    c_empresa = col_id(cadastro.columns, "EMPRESA")
    c_telefone = col_id(cadastro.columns, "TELEFONE", "CELULAR")
    c_gestor_direto = col_id(cadastro.columns, "GESTOR DIRETO", "GESTOR")
    c_gestor_superior = col_id(cadastro.columns, "GESTOR SUPERIOR")
    c_matricula = col_id(cadastro.columns, "MATRICULA", "MATRÍCULA", "MATRICULA DO COLABORADOR")

    matricula = normalize_matricula(cell_value(row, c_matricula))
    if not matricula:
        return None

    status = cell_value(row, c_status)
    status_norm = normalize_text(status)
    if status_norm in STATUS_INVALIDO_SYNC_SET:
        return None
    ativo_no_app = status_norm not in STATUS_INATIVO_SET
    payload = row_as_dict(row, cadastro.columns)
    payload["__fonte_cadastro__"] = "CADASTRO_DE_COLABORADORES"
    payload["__fonte_cadastro_sheet_id__"] = "1745799836133252"

    return ColaboradorRecord(
        row_id=row.id,
        email=safe_lower(cell_value(row, c_email)),
        nome=cell_value(row, c_nome),
        status=status,
        admissao=parse_date(cell_value(row, c_adm)),
        setor=cell_value(row, c_setor),
        cargo=cell_value(row, c_cargo),
        regime=cell_value(row, c_regime),
        unidade=cell_value(row, c_unidade),
        empresa=cell_value(row, c_empresa),
        telefone=cell_value(row, c_telefone),
        dias_direito=0,
        user_type="USER",
        gestor_direto=str(cell_value(row, c_gestor_direto) or "").strip(),
        gestor_superior=str(cell_value(row, c_gestor_superior) or "").strip(),
        ativo_no_app=ativo_no_app,
        matricula=matricula,
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
        record.unidade,
        record.empresa,
        record.telefone,
        record.gestor_direto,
        record.gestor_superior,
        record.matricula,
    )
    score = sum(1 for v in fields if v not in (None, "", [], {}, 0))
    score += status_rank(record.status)
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

    for attr in ("nome", "status", "admissao", "setor", "cargo", "regime", "unidade", "empresa", "telefone", "gestor_direto", "gestor_superior", "matricula"):
        if getattr(winner, attr) in (None, "") and getattr(loser, attr) not in (None, ""):
            setattr(winner, attr, getattr(loser, attr))


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
        "unidade": clean_optional(record.unidade),
        "empresa": clean_optional(record.empresa),
        "telefone": clean_optional(record.telefone),
        "origem_sheet_id": sheet_id_str,
        "origem_row_id": row_id_str,
    }
    for attr, value in updates.items():
        # Evita apagar informação boa do banco com célula vazia, exceto status/origem.
        # Saldos/dias de direito não vêm mais do Smartsheet.
        if attr not in {"status", "origem_sheet_id", "origem_row_id"} and value in (None, ""):
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


def _upsert_complemento_acesso(session, colab: Colaborador, record: ColaboradorRecord, sheet_id_str: str, row_id_str: str, email_to_matricula: Optional[dict[str, str]] = None) -> bool:
    """Atualiza o cache de acesso/hierarquia sem mexer em saldos."""
    comp = session.query(ColaboradorComplemento).filter(ColaboradorComplemento.colaborador_id == colab.id).first()
    created = False
    if not comp:
        comp = ColaboradorComplemento(colaborador_id=colab.id, colaborador_matricula=colab.matricula)
        session.add(comp)
        created = True

    comp.colaborador_matricula = colab.matricula
    existing_role = _get_existing_permission_role(session, colab)
    if created:
        comp.user_type = existing_role or "USER"
    elif existing_role:
        comp.user_type = existing_role

    # Relacoes de gestor sao gravadas por matricula/marcador. Se o Smartsheet trouxer contato/e-mail, ele e convertido para matricula ativa antes de salvar.
    gd_ref = _resolve_gestor_ref(record.gestor_direto, email_to_matricula, allow_dp=False, allow_gestor=False)
    gs_ref = _resolve_gestor_ref(record.gestor_superior, email_to_matricula, allow_dp=True, allow_gestor=True)
    gestor_direto = _get_colaborador_by_matricula(session, gd_ref) if gd_ref else None
    gestor_superior = _get_colaborador_by_matricula(session, gs_ref) if gs_ref and gs_ref not in {"DP", "GESTOR"} else None

    comp.gestor_direto = gestor_direto.matricula if gestor_direto else None
    comp.gestor_direto_email = safe_lower(gestor_direto.email) if gestor_direto and gestor_direto.email else None
    comp.gestor_superior = gs_ref or None
    comp.gestor_superior_email = safe_lower(gestor_superior.email) if gestor_superior and gestor_superior.email else None

    comp.ativo_no_app = bool(record.ativo_no_app)
    comp.origem_sheet_id = sheet_id_str
    comp.origem_row_id = row_id_str
    comp.updated_at = dt.datetime.utcnow()
    return created


def _get_existing_permission_role(session, colab: Colaborador) -> str:
    rows = session.query(PermissaoUsuario).filter(
        (PermissaoUsuario.colaborador_matricula == colab.matricula) | (PermissaoUsuario.colaborador_id == colab.id)
    ).all()
    roles = {str(r.role or "").strip().upper() for r in rows}
    if "ADMINISTRADOR" in roles or "ADMIN" in roles:
        return "ADMIN"
    if "DP" in roles or "RH" in roles:
        return "DP"
    if "USER" in roles:
        return "USER"
    return ""


def _ensure_permissao_usuario_default(session, colab: Colaborador) -> str:
    """Garante permissão mínima sem sobrescrever permissões existentes.

    A coluna USER TYPE deixou de ser sincronizada do Smartsheet. A tabela
    permissoes_usuario é a fonte operacional. Novos colaboradores recebem USER
    apenas quando ainda não possuem nenhuma permissão cadastrada no PostgreSQL.
    """
    existing = _get_existing_permission_role(session, colab)
    if existing:
        return existing
    session.add(PermissaoUsuario(
        colaborador_id=colab.id,
        colaborador_matricula=colab.matricula,
        role="USER",
    ))
    return "USER"


def _sync_hierarquia_gestao(session, colab: Colaborador, record: ColaboradorRecord, email_to_matricula: Optional[dict[str, str]] = None) -> bool:
    """Sincroniza app_ferias.hierarquia_gestao a partir da planilha de cadastro."""
    h = session.query(HierarquiaGestao).filter(
        (HierarquiaGestao.colaborador_matricula == colab.matricula) | (HierarquiaGestao.colaborador_id == colab.id)
    ).first()
    created = False
    if not h:
        h = HierarquiaGestao(colaborador_id=colab.id, colaborador_matricula=colab.matricula)
        session.add(h)
        created = True

    gd_ref = _resolve_gestor_ref(record.gestor_direto, email_to_matricula, allow_dp=False, allow_gestor=False)
    gs_ref = _resolve_gestor_ref(record.gestor_superior, email_to_matricula, allow_dp=True, allow_gestor=True)
    gestor_direto = _get_colaborador_by_matricula(session, gd_ref) if gd_ref else None
    gestor_superior = _get_colaborador_by_matricula(session, gs_ref) if gs_ref and gs_ref not in {"DP", "GESTOR"} else None

    h.colaborador_id = colab.id
    h.colaborador_matricula = colab.matricula
    h.gestor_direto_id = gestor_direto.id if gestor_direto else None
    h.gestor_direto_matricula = gestor_direto.matricula if gestor_direto else None
    h.gestor_direto_email = safe_lower(gestor_direto.email) if gestor_direto and gestor_direto.email else None
    h.gestor_superior_id = gestor_superior.id if gestor_superior else None
    h.gestor_superior_matricula = (gestor_superior.matricula if gestor_superior else (gs_ref or None))
    h.gestor_superior_email = safe_lower(gestor_superior.email) if gestor_superior and gestor_superior.email else None
    return created


def _sync_colaboradores(session, cadastro: SheetMaps, cadastro_sheet_id: int, precomputed: Optional[tuple[list[ColaboradorRecord], dict]] = None) -> dict:
    """Sincroniza cadastro, permissões e hierarquia a partir do Smartsheet.

    Regras atuais:
    - Matrícula é a chave principal de negócio.
    - Dados cadastrais principais já existentes no PostgreSQL são preservados.
    - Matrículas novas são incluídas com os dados iniciais do Smartsheet.
    - Hierarquia e cache de acesso são sincronizados a cada execução.
    - Permissões existentes no PostgreSQL são preservadas; matrícula nova sem
      permissão recebe USER.
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
    email_to_matricula, gestor_email_ambiguo = _active_email_to_matricula_map(records)
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
            unidade=clean_optional(record.unidade),
            empresa=clean_optional(record.empresa),
            telefone=clean_optional(record.telefone),
            dias_direito=0,
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
                created_comp = _upsert_complemento_acesso(session, fresh_colab, record, sheet_id_str, row_id_str, email_to_matricula=email_to_matricula)
                if created_comp:
                    complemento_inserted += 1
                else:
                    complemento_updated += 1
                _ensure_permissao_usuario_default(session, fresh_colab)
                permissoes_synced += 1
                created_h = _sync_hierarquia_gestao(session, fresh_colab, record, email_to_matricula=email_to_matricula)
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

    # V58: a sincronização cadastral não apaga saldo_periodo quando alguém passa
    # a INATIVO. O histórico permanece; a rotina de ciclos consulta somente ATIVOS,
    # portanto nenhuma nova linha será criada enquanto o cadastro estiver inativo.
    saldos_inativos_removidos = 0  # mantido na resposta por compatibilidade
    session.commit()

    return {
        "mode": "sync_by_matricula_cadastro_colaboradores_v58",
        "saldos_inativos_removidos": int(saldos_inativos_removidos or 0),
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
        "permissoes_default_checked": permissoes_synced,
        "hierarquia_synced": hierarquia_synced,
        "hierarquia_inserted": hierarquia_inserted,
        "skipped": skipped,
        "sem_matricula": sem_matricula,
        "conflicts": conflicts,
        "matched_by": matched_by,
        "conflict_details": conflict_details[:50],
        "duplicates": {key: sorted(set(rows)) for key, rows in duplicates.items()},
        "gestor_emails_ambiguos_ignorados": sorted(gestor_email_ambiguo),
        "gestor_email_refs_mapeadas": len(email_to_matricula),
    }




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
    """Compatibilidade de interface; nenhum saldo é recalculado nesta transação."""
    return {
        "recalculated": 0,
        "saldo_periodo_synced": 0,
        "message": "A normalização V58 ocorre depois da sincronização, somente em saldo_periodo.",
    }

def recalcular_saldo_periodo_from_db() -> dict:
    """Executa somente a verificação de ciclos adquiridos no PostgreSQL."""
    from .period_accrual_service import ensure_due_periods
    return ensure_due_periods(
        actor_email="manual-db-recalc",
        force=True,
        wait_for_lock=True,
    )

def sync_cadastro_from_smartsheet(triggered_by: str = "manual", actor_email: str = "", recalculate: bool = False, include_solicitacoes: bool = False) -> dict:
    settings = get_settings()
    if not settings.access_token:
        raise ValueError("SMARTSHEET_ACCESS_TOKEN não configurado no Render.")
    # Fonte oficial de dados cadastrais: CADASTRO DE COLABORADORES
    # (Smartsheet 1745799836133252). A antiga CONTROLE_DP deixou de ser
    # origem de cadastro/permissões/saldos.
    sheet_id = int(getattr(settings, "id_folha_cadastro_principal", 0) or 1745799836133252)

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

        # Novos colaboradores ativos recebem imediatamente somente os ciclos já
        # concluídos. Quem ficar inativo preserva o histórico e deixa de receber novos ciclos.
        try:
            from .period_accrual_service import ensure_due_periods
            period_result = ensure_due_periods(
                actor_email=safe_lower(actor_email or triggered_by or "smartsheet-sync"),
                force=True,
                wait_for_lock=True,
            )
            extra["periodos_v58"] = period_result
        except Exception as period_exc:
            log.exception("Cadastro sincronizado, mas falhou a normalização V58 de saldo_periodo")
            extra["periodos_v58_error"] = str(period_exc)[:1000]

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


# Wrapper de concorrencia: evita duas sincronizacoes simultaneas no mesmo worker
# e permite que o Painel ADMIN dispare a rotina em background sem estourar timeout HTTP.
_SYNC_CADASTRO_LOCK = threading.Lock()
_sync_cadastro_from_smartsheet_impl = sync_cadastro_from_smartsheet


def is_sync_cadastro_running() -> bool:
    return _SYNC_CADASTRO_LOCK.locked()


def sync_cadastro_from_smartsheet(triggered_by: str = "manual", actor_email: str = "", recalculate: bool = False, include_solicitacoes: bool = False) -> dict:
    if not _SYNC_CADASTRO_LOCK.acquire(blocking=False):
        raise RuntimeError("Já existe uma sincronização de cadastro em execução. Aguarde a conclusão antes de iniciar outra.")
    try:
        return _sync_cadastro_from_smartsheet_impl(
            triggered_by=triggered_by,
            actor_email=actor_email,
            recalculate=recalculate,
            include_solicitacoes=include_solicitacoes,
        )
    finally:
        _SYNC_CADASTRO_LOCK.release()


def start_sync_cadastro_background(triggered_by: str = "manual", actor_email: str = "", recalculate: bool = False, include_solicitacoes: bool = False) -> dict:
    """Dispara a sincronização em thread separada e retorna imediatamente.

    A rota HTTP do Render/Gunicorn não deve esperar a sincronização completa,
    pois a etapa de permissões/hierarquia pode levar vários minutos.
    """
    if not _SYNC_CADASTRO_LOCK.acquire(blocking=False):
        return {
            "ok": True,
            "started": False,
            "running": True,
            "message": "Já existe uma sincronização de cadastro em execução. Acompanhe pelo status.",
        }

    try:
        with get_session() as session:
            _mark_sync(session, "cadastro", "running", extra={
                "triggered_by": triggered_by,
                "actor_email": actor_email,
                "progress_message": "Sincronização iniciada em background",
                "progress_percent": 0,
                "include_solicitacoes": include_solicitacoes,
                "recalculate": recalculate,
            })
            session.commit()
    except Exception:
        log.exception("Nao foi possivel registrar inicio da sincronizacao em background")

    def worker() -> None:
        try:
            _sync_cadastro_from_smartsheet_impl(
                triggered_by=triggered_by,
                actor_email=actor_email,
                recalculate=recalculate,
                include_solicitacoes=include_solicitacoes,
            )
        except Exception:
            log.exception("Falha na sincronização de cadastro em background")
        finally:
            _SYNC_CADASTRO_LOCK.release()

    threading.Thread(target=worker, name="admin-sync-cadastro", daemon=True).start()
    return {
        "ok": True,
        "started": True,
        "running": True,
        "message": "Sincronização iniciada em background. Acompanhe o andamento pelo status.",
        "include_solicitacoes": include_solicitacoes,
        "recalculate": recalculate,
    }

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
