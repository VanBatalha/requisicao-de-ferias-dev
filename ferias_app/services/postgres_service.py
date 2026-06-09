"""Serviço de acesso ao PostgreSQL - substitui smartsheet_service.py"""
from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, date
from typing import Any, Dict, List, Optional, Tuple
import json

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker, Session
from flask import g

from ..config import get_settings
from ..logging_config import get_logger
from ..models import (
    Base, Colaborador, ColaboradorComplemento, Solicitacao, AdminConfig, Auditoria, SyncState
)

log = get_logger(__name__)


def _db_schema_name() -> str:
    """Schema PostgreSQL usado pelo app.

    Por padrão usa ferias_app, que é o schema onde a base importada está.
    Mantém validação simples para evitar SQL inválido/injeção via variável de ambiente.
    """
    import re

    schema = (os.getenv("DB_SCHEMA") or "ferias_app").strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
        log.warning("DB_SCHEMA inválido (%r); usando ferias_app", schema)
        schema = "ferias_app"
    return schema


def _quote_ident(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


# Engine e SessionLocal globais
_ENGINE = None
_SessionLocal = None


def init_db():
    """Inicializa a conexão com o banco de dados."""
    global _ENGINE, _SessionLocal
    
    settings = get_settings()
    db_url = settings.database_url
    
    if not db_url:
        raise ValueError("DATABASE_URL não configurada.")
    
    schema = _db_schema_name()
    schema_sql = _quote_ident(schema)

    _ENGINE = create_engine(db_url, echo=False, pool_pre_ping=True)

    @event.listens_for(_ENGINE, "connect")
    def _set_search_path(dbapi_connection, connection_record):  # noqa: ANN001, ARG001
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(f"SET search_path TO {schema_sql}, public")
        finally:
            cursor.close()

    # Garante o schema antes do create_all. A aplicação usa modelos sem schema fixo,
    # então o search_path precisa apontar para ferias_app; caso contrário o SQLAlchemy
    # pode procurar/criar tabelas no public e não enxergar os dados importados.
    with _ENGINE.begin() as conn:
        conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema_sql}"))
        conn.execute(text(f"SET search_path TO {schema_sql}, public"))

    _SessionLocal = sessionmaker(bind=_ENGINE, expire_on_commit=False)
    
    # Cria as tabelas se não existirem dentro do schema configurado
    Base.metadata.create_all(_ENGINE)

    # Pequenas migrações defensivas para bases já criadas antes das últimas versões.
    # create_all() não altera tabelas existentes; por isso garantimos colunas novas
    # usadas pelo painel sem depender de uma ferramenta externa de migração.
    with _ENGINE.begin() as conn:
        conn.execute(text(f"ALTER TABLE {schema_sql}.sync_state ADD COLUMN IF NOT EXISTS extra JSONB"))
        # Bases antigas podem ter sido criadas sem a matrícula do colaborador.
        # Mantemos o ID inteiro interno como chave técnica, e usamos matricula como ID externo/cadastro.
        conn.execute(text(f"ALTER TABLE {schema_sql}.colaboradores ADD COLUMN IF NOT EXISTS matricula VARCHAR(50)"))
        conn.execute(text(f"""
            UPDATE {schema_sql}.colaboradores
               SET matricula = COALESCE(
                   NULLIF(raw_payload->>'__matricula_escolhida__', ''),
                   NULLIF(raw_payload->>'MATRICULA', ''),
                   NULLIF(raw_payload->>'MATRÍCULA', ''),
                   NULLIF(raw_payload->>'MATRICULA DO COLABORADOR', '')
               )
             WHERE (matricula IS NULL OR btrim(matricula) = '')
               AND raw_payload IS NOT NULL
        """))
        conn.execute(text(f"CREATE INDEX IF NOT EXISTS ix_colaboradores_matricula_lower ON {schema_sql}.colaboradores (lower(matricula))"))
        # Bases antigas podem ter sido criadas com dias_direito NOT NULL.
        # Garante valor padrão para importações/sincronizações com linhas incompletas no Smartsheet.
        conn.execute(text(f"UPDATE {schema_sql}.colaboradores SET dias_direito = 0 WHERE dias_direito IS NULL"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.colaboradores ALTER COLUMN dias_direito SET DEFAULT 0"))

    log.info("Banco de dados PostgreSQL inicializado no schema %s", schema)


def get_db_session() -> Session:
    """Obtém a sessão do banco para o request atual."""
    if not hasattr(g, '_db_session') or g._db_session is None:
        if _SessionLocal is None:
            init_db()
        g._db_session = _SessionLocal()
    return g._db_session


@contextmanager
def get_session():
    """Context manager para criar e fechar uma sessão."""
    if _SessionLocal is None:
        init_db()
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ============================================
# FUNÇÕES PARA COMPATIBILIDADE COM SMARTSHEET
# ============================================

def get_colaborador(email: str) -> Optional[Dict[str, Any]]:
    """Retorna dados do colaborador por email."""
    session = get_db_session()
    try:
        colab = session.query(Colaborador).filter(
            Colaborador.email == email.lower()
        ).first()
        
        if not colab:
            return None
        
        result = colab.to_dict()
        
        # Adiciona dados do complemento se existir
        if colab.complemento:
            result.update(colab.complemento.to_dict())
        
        return result
    except Exception as e:
        log.error(f"Erro ao buscar colaborador {email}: {e}")
        return None


def listar_colaboradores(status_filter: Optional[str] = None) -> List[Dict[str, Any]]:
    """Lista todos os colaboradores, opcionalmente filtrados por status."""
    session = get_db_session()
    try:
        query = session.query(Colaborador)
        
        if status_filter:
            query = query.filter(Colaborador.status == status_filter.upper())
        
        colaboradores = query.order_by(Colaborador.nome_completo).all()
        
        return [colab.to_dict() for colab in colaboradores]
    except Exception as e:
        log.error(f"Erro ao listar colaboradores: {e}")
        return []


def get_saldos_colaborador(email: str) -> Dict[str, Any]:
    """Retorna saldos do colaborador."""
    session = get_db_session()
    try:
        colab = session.query(Colaborador).filter(
            Colaborador.email == email.lower()
        ).first()
        
        if not colab or not colab.complemento:
            return {
                'regular': {'direito': 0, 'usado': 0, 'reservado': 0, 'disponivel': 0},
                'premium': {'direito': 0, 'usado': 0, 'reservado': 0, 'disponivel': 0},
            }
        
        return colab.complemento.to_dict()['saldo_regular'] if colab.complemento else {}
    except Exception as e:
        log.error(f"Erro ao obter saldos do colaborador {email}: {e}")
        return {}


def criar_solicitacao(payload: Dict[str, Any]) -> Tuple[bool, str, Optional[int]]:
    """Cria uma nova solicitação no banco."""
    session = get_db_session()
    try:
        colaborador_email = (payload.get('colaborador_email') or '').strip().lower()
        
        # Busca o colaborador
        colab = session.query(Colaborador).filter(
            Colaborador.email == colaborador_email
        ).first()
        
        if not colab:
            return False, f"Colaborador {colaborador_email} não encontrado", None
        
        # Cria a solicitação
        solicitacao = Solicitacao(
            colaborador_id=colab.id,
            colaborador_email=colaborador_email,
            gestor_solicitante_email=(payload.get('gestor_email') or '').strip().lower(),
            criado_por=(payload.get('criado_por') or '').strip().lower(),
            solicitacao=payload.get('solicitacao', ''),
            saldo_tipo=(payload.get('saldo_tipo') or 'REGULAR').upper(),
            data_inicio=payload.get('data_inicio'),
            data_fim=payload.get('data_fim'),
            dias=payload.get('dias', 0),
            status=(payload.get('status') or 'PENDENTE').upper(),
            observacoes=payload.get('observacoes', ''),
            is_ajuste=payload.get('is_ajuste', False),
            metadata_json=payload.get('metadata'),
            raw_payload=payload.get('raw_payload'),
            source_created_at=datetime.utcnow(),
        )
        
        session.add(solicitacao)
        session.commit()
        
        return True, "Solicitação criada com sucesso", solicitacao.id
    except Exception as e:
        log.error(f"Erro ao criar solicitação: {e}")
        session.rollback()
        return False, str(e), None


def atualizar_solicitacao(solicitacao_id: int, payload: Dict[str, Any]) -> Tuple[bool, str]:
    """Atualiza uma solicitação existente."""
    session = get_db_session()
    try:
        solicitacao = session.query(Solicitacao).filter(
            Solicitacao.id == solicitacao_id
        ).first()
        
        if not solicitacao:
            return False, "Solicitação não encontrada"
        
        # Atualiza campos
        if 'status' in payload:
            solicitacao.status = payload['status'].upper()
        if 'observacoes' in payload:
            solicitacao.observacoes = payload['observacoes']
        if 'dias' in payload:
            solicitacao.dias = payload['dias']
        
        solicitacao.updated_at = datetime.utcnow()
        session.commit()
        
        return True, "Solicitação atualizada com sucesso"
    except Exception as e:
        log.error(f"Erro ao atualizar solicitação: {e}")
        session.rollback()
        return False, str(e)


def listar_solicitacoes(
    filtro_email: Optional[str] = None,
    filtro_status: Optional[str] = None,
    filtro_periodo: Optional[Tuple[date, date]] = None,
) -> List[Dict[str, Any]]:
    """Lista solicitações com filtros opcionais."""
    session = get_db_session()
    try:
        query = session.query(Solicitacao)
        
        if filtro_email:
            query = query.filter(Solicitacao.colaborador_email == filtro_email.lower())
        
        if filtro_status:
            query = query.filter(Solicitacao.status == filtro_status.upper())
        
        if filtro_periodo:
            data_inicio, data_fim = filtro_periodo
            query = query.filter(
                Solicitacao.data_inicio >= data_inicio,
                Solicitacao.data_fim <= data_fim,
            )
        
        solicitacoes = query.order_by(Solicitacao.data_inicio.desc()).all()
        
        return [sol.to_dict() for sol in solicitacoes]
    except Exception as e:
        log.error(f"Erro ao listar solicitações: {e}")
        return []


def atualizar_saldos_colaborador(
    email: str,
    saldo_regular_direito: Optional[int] = None,
    saldo_regular_usado: Optional[int] = None,
    saldo_regular_reservado: Optional[int] = None,
    saldo_premium_direito: Optional[int] = None,
    saldo_premium_usado: Optional[int] = None,
    saldo_premium_reservado: Optional[int] = None,
) -> bool:
    """Atualiza os saldos do colaborador."""
    session = get_db_session()
    try:
        colab = session.query(Colaborador).filter(
            Colaborador.email == email.lower()
        ).first()
        
        if not colab:
            return False
        
        if not colab.complemento:
            colab.complemento = ColaboradorComplemento(colaborador_id=colab.id)
        
        # Atualiza saldos (apenas se fornecido)
        if saldo_regular_direito is not None:
            colab.complemento.saldo_regular_direito = saldo_regular_direito
        if saldo_regular_usado is not None:
            colab.complemento.saldo_regular_usado = saldo_regular_usado
        if saldo_regular_reservado is not None:
            colab.complemento.saldo_regular_reservado = saldo_regular_reservado
        if saldo_premium_direito is not None:
            colab.complemento.saldo_premium_direito = saldo_premium_direito
        if saldo_premium_usado is not None:
            colab.complemento.saldo_premium_usado = saldo_premium_usado
        if saldo_premium_reservado is not None:
            colab.complemento.saldo_premium_reservado = saldo_premium_reservado
        
        # Recalcula saldos disponíveis
        colab.complemento.saldo_regular_disponivel = (
            colab.complemento.saldo_regular_direito -
            colab.complemento.saldo_regular_usado -
            colab.complemento.saldo_regular_reservado
        )
        colab.complemento.saldo_premium_disponivel = (
            colab.complemento.saldo_premium_direito -
            colab.complemento.saldo_premium_usado -
            colab.complemento.saldo_premium_reservado
        )
        
        colab.complemento.calculated_at = datetime.utcnow()
        session.commit()
        
        return True
    except Exception as e:
        log.error(f"Erro ao atualizar saldos: {e}")
        session.rollback()
        return False


def registrar_auditoria(
    actor_email: str,
    action: str,
    entity_type: str,
    entity_id: int,
    before_data: Optional[Dict] = None,
    after_data: Optional[Dict] = None,
    context: Optional[Dict] = None,
) -> bool:
    """Registra uma ação na auditoria."""
    session = get_db_session()
    try:
        audit = Auditoria(
            actor_email=actor_email,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            before_data=before_data,
            after_data=after_data,
            context=context,
        )
        session.add(audit)
        session.commit()
        return True
    except Exception as e:
        log.error(f"Erro ao registrar auditoria: {e}")
        session.rollback()
        return False


def get_sync_state(sync_name: str) -> Optional[Dict[str, Any]]:
    """Obtém o estado de uma sincronização."""
    session = get_db_session()
    try:
        sync = session.query(SyncState).filter(
            SyncState.sync_name == sync_name
        ).first()
        
        if not sync:
            return None
        
        return {
            'sync_name': sync.sync_name,
            'last_started_at': sync.last_started_at,
            'last_finished_at': sync.last_finished_at,
            'last_success_at': sync.last_success_at,
            'last_status': sync.last_status,
            'last_error': sync.last_error,
        }
    except Exception as e:
        log.error(f"Erro ao obter sync state: {e}")
        return None


def atualizar_sync_state(
    sync_name: str,
    status: str,
    error: Optional[str] = None,
) -> bool:
    """Atualiza o estado de uma sincronização."""
    session = get_db_session()
    try:
        sync = session.query(SyncState).filter(
            SyncState.sync_name == sync_name
        ).first()
        
        if not sync:
            sync = SyncState(sync_name=sync_name)
            session.add(sync)
        
        sync.last_started_at = datetime.utcnow()
        sync.last_status = status
        if error:
            sync.last_error = error
        else:
            sync.last_success_at = datetime.utcnow()
            sync.last_error = None
        sync.last_finished_at = datetime.utcnow()
        
        session.commit()
        return True
    except Exception as e:
        log.error(f"Erro ao atualizar sync state: {e}")
        session.rollback()
        return False


# Stub functions para compatibilidade com código existente
def get_sheet(access_token: str, sheet_id: int):
    """Stub para compatibilidade - retorna dict simulando sheet do Smartsheet."""
    # Retorna uma estrutura mínima que o código existente espera
    return {
        'id': sheet_id,
        'name': 'Sheet',
        'columns': [],
        'rows': [],
    }


def add_rows(access_token: str, sheet_id: int, rows: list):
    """Stub para compatibilidade com adicionar linhas."""
    return {'ok': True, 'result': []}


def update_rows(access_token: str, sheet_id: int, rows: list):
    """Stub para compatibilidade com atualizar linhas."""
    return {'ok': True}


def columns_map(sheet) -> Dict[str, int]:
    """Stub para compatibilidade com mapeamento de colunas."""
    return {}
