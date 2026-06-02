#!/usr/bin/env python3
"""
Importa dados do arquivo export_ferias_app.xlsx para o PostgreSQL.

Uso:
    python import_data.py <database_url> <excel_file>

Exemplo:
    python import_data.py "postgresql://user:pass@host:5432/ferias_app" export_ferias_app.xlsx
"""

from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

# Importar modelos
sys.path.insert(0, os.path.dirname(__file__))
from ferias_app.models import (  # noqa: E402
    Base,
    Colaborador,
    ColaboradorComplemento,
    Solicitacao,
    SyncState,
)


def db_schema_name() -> str:
    import re
    schema = (os.getenv("DB_SCHEMA") or "ferias_app").strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
        schema = "ferias_app"
    return schema


def quote_ident(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def configure_engine_schema(engine):
    schema = db_schema_name()
    schema_sql = quote_ident(schema)

    @event.listens_for(engine, "connect")
    def _set_search_path(dbapi_connection, connection_record):  # noqa: ANN001, ARG001
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(f"SET search_path TO {schema_sql}, public")
        finally:
            cursor.close()

    with engine.begin() as conn:
        conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema_sql}"))
        conn.execute(text(f"SET search_path TO {schema_sql}, public"))
    return engine


def is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    try:
        result = pd.isna(value)
        if isinstance(result, bool):
            return result
    except Exception:
        pass
    return False


def clean_str(value: Any, *, lower: bool = False, upper: bool = False) -> Optional[str]:
    if is_missing(value):
        return None
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return None
    if lower:
        text = text.lower()
    if upper:
        text = text.upper()
    return text


def parse_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    if is_missing(value):
        return default
    try:
        return int(float(str(value).strip()))
    except Exception:
        return default


def parse_bool(value: Any, default: bool = True) -> bool:
    if is_missing(value):
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "sim", "yes", "y", "s", "ativo"}:
        return True
    if text in {"0", "false", "nao", "não", "no", "n", "inativo"}:
        return False
    return bool(value)


def parse_date(value: Any):
    if is_missing(value):
        return None
    try:
        return pd.to_datetime(value).date()
    except Exception:
        return None


def parse_datetime(value: Any):
    if is_missing(value):
        return None
    try:
        return pd.to_datetime(value).to_pydatetime()
    except Exception:
        return None


def to_jsonable(value: Any) -> Any:
    if is_missing(value):
        return None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    return value


def parse_json_value(value: Any) -> Any:
    if is_missing(value):
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text or text.lower() == "nan":
            return None
        try:
            return json.loads(text)
        except Exception:
            return None
    return value


def raw_payload_from_row(row: pd.Series) -> Dict[str, Any]:
    """Usa o JSON real da coluna raw_payload quando existir.

    Na versão anterior, o importador salvava a linha inteira do Excel dentro de
    raw_payload, deixando o JSON original aninhado. Isso impedia a aplicação de
    ler campos legados como USER TYPE e GESTOR DIRETO em alguns cenários.
    """
    parsed = parse_json_value(row.get("raw_payload"))
    if isinstance(parsed, dict):
        return parsed
    return {str(k): to_jsonable(v) for k, v in row.to_dict().items() if not is_missing(v)}


def import_colaboradores(session, excel_file: str) -> None:
    """Importa/atualiza a aba 'colaboradores'."""
    print("📥 Importando colaboradores...")
    df = pd.read_excel(excel_file, sheet_name="colaboradores")

    for _, row in df.iterrows():
        email = clean_str(row.get("email"), lower=True)
        if not email:
            continue

        colab = session.query(Colaborador).filter_by(email=email).first()
        if not colab:
            kwargs: Dict[str, Any] = {}
            excel_id = parse_int(row.get("id"))
            if excel_id and not session.query(Colaborador).filter_by(id=excel_id).first():
                # Preserva o ID do export quando a base está vazia/nova. Isso mantém
                # as FKs do colaborador_complemento compatíveis com o XLSX original.
                kwargs["id"] = excel_id
            colab = Colaborador(**kwargs)
            session.add(colab)

        colab.email = email
        colab.nome_completo = clean_str(row.get("nome_completo"))
        colab.status = clean_str(row.get("status"), upper=True)
        colab.data_admissao = parse_date(row.get("data_admissao"))
        colab.setor = clean_str(row.get("setor"))
        colab.cargo = clean_str(row.get("cargo"))
        colab.regime = clean_str(row.get("regime"))
        colab.dias_direito = parse_int(row.get("dias_direito"), 0)
        colab.origem_sheet_id = clean_str(row.get("origem_sheet_id"))
        colab.origem_row_id = clean_str(row.get("origem_row_id"))
        colab.raw_payload = raw_payload_from_row(row)

    session.commit()
    count = session.query(Colaborador).count()
    print(f"✅ {count} colaboradores no banco")


def _old_id_to_email(excel_file: str) -> Dict[int, str]:
    df_colabs = pd.read_excel(excel_file, sheet_name="colaboradores")
    out: Dict[int, str] = {}
    for _, row in df_colabs.iterrows():
        old_id = parse_int(row.get("id"))
        email = clean_str(row.get("email"), lower=True)
        if old_id and email:
            out[old_id] = email
    return out


def import_colaborador_complemento(session, excel_file: str) -> None:
    """Importa/atualiza a aba 'colaborador_complemento'.

    A relação é feita por e-mail usando o ID antigo do XLSX apenas como ponte.
    Assim o importador também corrige bases que foram importadas sem preservar
    os IDs originais dos colaboradores.
    """
    print("📥 Importando dados complementares...")
    df = pd.read_excel(excel_file, sheet_name="colaborador_complemento")
    id_email = _old_id_to_email(excel_file)

    for _, row in df.iterrows():
        old_colab_id = parse_int(row.get("colaborador_id"))
        email = id_email.get(old_colab_id or -1)

        colab = None
        if email:
            colab = session.query(Colaborador).filter_by(email=email).first()
        if not colab and old_colab_id:
            colab = session.query(Colaborador).filter_by(id=old_colab_id).first()
        if not colab:
            continue

        compl = session.query(ColaboradorComplemento).filter_by(colaborador_id=colab.id).first()
        if not compl:
            compl = ColaboradorComplemento(colaborador_id=colab.id)
            session.add(compl)

        compl.user_type = clean_str(row.get("user_type"), upper=True) or "USER"
        compl.gestor_direto_email = clean_str(row.get("gestor_direto_email"), lower=True)
        compl.gestor_superior_email = clean_str(row.get("gestor_superior_email"), lower=True)
        compl.ativo_no_app = parse_bool(row.get("ativo_no_app"), True)
        compl.flags_internas = parse_json_value(row.get("flags_internas")) or {}
        compl.saldo_regular_direito = parse_int(row.get("saldo_regular_direito"), 0) or 0
        compl.saldo_regular_usado = parse_int(row.get("saldo_regular_usado"), 0) or 0
        compl.saldo_regular_reservado = parse_int(row.get("saldo_regular_reservado"), 0) or 0
        compl.saldo_regular_disponivel = parse_int(row.get("saldo_regular_disponivel"), 0) or 0
        compl.saldo_premium_direito = parse_int(row.get("saldo_premium_direito"), 0) or 0
        compl.saldo_premium_usado = parse_int(row.get("saldo_premium_usado"), 0) or 0
        compl.saldo_premium_reservado = parse_int(row.get("saldo_premium_reservado"), 0) or 0
        compl.saldo_premium_disponivel = parse_int(row.get("saldo_premium_disponivel"), 0) or 0
        compl.total_solicitacoes = parse_int(row.get("total_solicitacoes"), 0) or 0
        compl.periodo_aquisitivo_atual = parse_json_value(row.get("periodo_aquisitivo_atual")) or {}
        compl.calculated_at = parse_datetime(row.get("calculated_at"))
        compl.origem_sheet_id = clean_str(row.get("origem_sheet_id"))
        compl.origem_row_id = clean_str(row.get("origem_row_id"))

    session.commit()
    count = session.query(ColaboradorComplemento).count()
    print(f"✅ {count} complementos no banco")


def import_solicitacoes(session, excel_file: str) -> None:
    """Importa solicitações ainda inexistentes da aba 'solicitacoes'."""
    print("📥 Importando solicitações...")
    df = pd.read_excel(excel_file, sheet_name="solicitacoes")

    for _, row in df.iterrows():
        email = clean_str(row.get("colaborador_email"), lower=True)
        if not email:
            continue

        data_inicio = parse_date(row.get("data_inicio"))
        data_fim = parse_date(row.get("data_fim"))
        if not data_inicio or not data_fim:
            continue

        smartsheet_row_id = clean_str(row.get("smartsheet_row_id"))
        if smartsheet_row_id:
            existing = session.query(Solicitacao).filter_by(smartsheet_row_id=smartsheet_row_id).first()
            if existing:
                continue

        colab = session.query(Colaborador).filter_by(email=email).first()

        sol = Solicitacao(
            origem_sheet_id=clean_str(row.get("origem_sheet_id")),
            smartsheet_row_id=smartsheet_row_id,
            colaborador_id=colab.id if colab else None,
            colaborador_email=email,
            gestor_solicitante_email=clean_str(row.get("gestor_solicitante_email"), lower=True),
            criado_por=clean_str(row.get("criado_por"), lower=True),
            solicitacao=clean_str(row.get("solicitacao")),
            saldo_tipo=clean_str(row.get("saldo_tipo"), upper=True) or "REGULAR",
            data_inicio=data_inicio,
            data_fim=data_fim,
            dias=parse_int(row.get("dias"), 0) or 0,
            status=clean_str(row.get("status"), upper=True) or "PENDENTE",
            observacoes=clean_str(row.get("observacoes")),
            is_ajuste=parse_bool(row.get("is_ajuste"), False),
            metadata_json=parse_json_value(row.get("metadata")),
            raw_payload=parse_json_value(row.get("raw_payload")),
            source_created_at=parse_datetime(row.get("source_created_at")),
            source_modified_at=parse_datetime(row.get("source_modified_at")),
        )
        session.add(sol)

    session.commit()
    count = session.query(Solicitacao).count()
    print(f"✅ {count} solicitações no banco")


def import_sync_state(session, excel_file: str) -> None:
    """Importa/atualiza a aba 'sync_state'."""
    print("📥 Importando estado de sincronizações...")
    df = pd.read_excel(excel_file, sheet_name="sync_state")

    for _, row in df.iterrows():
        sync_name = clean_str(row.get("sync_name"))
        if not sync_name:
            continue

        sync = session.query(SyncState).filter_by(sync_name=sync_name).first()
        if not sync:
            sync = SyncState(sync_name=sync_name)
            session.add(sync)

        sync.last_started_at = parse_datetime(row.get("last_started_at"))
        sync.last_finished_at = parse_datetime(row.get("last_finished_at"))
        sync.last_success_at = parse_datetime(row.get("last_success_at"))
        sync.last_status = clean_str(row.get("last_status"))
        sync.last_error = clean_str(row.get("last_error"))
        sync.extra = parse_json_value(row.get("extra"))

    session.commit()
    count = session.query(SyncState).count()
    print(f"✅ {count} sync states no banco")


def main() -> None:
    if len(sys.argv) < 3:
        print("Uso: python import_data.py <database_url> <excel_file>")
        print("Exemplo: python import_data.py 'postgresql://user:pass@localhost:5432/ferias_app' export_ferias_app.xlsx")
        sys.exit(1)

    database_url = sys.argv[1]
    excel_file = sys.argv[2]

    if not Path(excel_file).exists():
        print(f"❌ Arquivo não encontrado: {excel_file}")
        sys.exit(1)

    print(f"🔄 Conectando ao banco: {database_url.split('@')[1] if '@' in database_url else 'local'}")

    engine = configure_engine_schema(create_engine(database_url, echo=False))
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()

    try:
        import_colaboradores(session, excel_file)
        import_colaborador_complemento(session, excel_file)
        import_solicitacoes(session, excel_file)
        import_sync_state(session, excel_file)

        print("\n✅ Importação concluída com sucesso!")
        colabs = session.query(Colaborador).count()
        complementos = session.query(ColaboradorComplemento).count()
        sols = session.query(Solicitacao).count()
        print(f"   📊 Total: {colabs} colaboradores, {complementos} complementos, {sols} solicitações")

    except Exception as e:
        session.rollback()
        print(f"\n❌ Erro durante importação: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        session.close()


if __name__ == "__main__":
    main()
