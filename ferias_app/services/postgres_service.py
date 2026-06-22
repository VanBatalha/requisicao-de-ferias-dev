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
    Base, Colaborador, ColaboradorComplemento, Solicitacao, AdminConfig, Auditoria, SyncState, PeriodoAquisitivo, SaldoPeriodo, AuditoriaSaldos, PermissaoUsuario, HierarquiaGestao
)

log = get_logger(__name__)


def _db_schema_name() -> str:
    """Schema PostgreSQL usado pelo app.

    Por padrão usa ferias_app, que é o schema onde a base importada está.
    Mantém validação simples para evitar SQL inválido/injeção via variável de ambiente.
    """
    import re

    schema = (os.getenv("DB_SCHEMA") or "app_ferias").strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
        log.warning("DB_SCHEMA inválido (%r); usando ferias_app", schema)
        schema = "app_ferias"
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
    def _set_search_path_and_timezone(dbapi_connection, connection_record):  # noqa: ANN001, ARG001
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(f"SET search_path TO {schema_sql}, public")
            # Mantem NOW(), CURRENT_TIMESTAMP e exibicao de TIMESTAMPTZ no fuso da empresa.
            app_timezone = getattr(settings, "app_timezone", None) or "America/Fortaleza"
            safe_tz = str(app_timezone).replace("'", "''")
            cursor.execute(f"SET TIME ZONE '{safe_tz}'")
        finally:
            cursor.close()

    # Garante o schema antes do create_all. A aplicação usa modelos sem schema fixo,
    # então o search_path precisa apontar para ferias_app; caso contrário o SQLAlchemy
    # pode procurar/criar tabelas no public e não enxergar os dados importados.
    with _ENGINE.begin() as conn:
        conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema_sql}"))
        conn.execute(text(f"SET search_path TO {schema_sql}, public"))
        safe_tz = str(getattr(settings, "app_timezone", "America/Fortaleza") or "America/Fortaleza").replace("'", "''")
        conn.execute(text(f"SET TIME ZONE '{safe_tz}'"))

    _SessionLocal = sessionmaker(bind=_ENGINE, expire_on_commit=False)
    
    # Cria as tabelas se não existirem dentro do schema configurado
    Base.metadata.create_all(_ENGINE)

    # Pequenas migrações defensivas para bases já criadas antes das últimas versões.
    # create_all() não altera tabelas existentes; por isso garantimos colunas novas
    # usadas pelo painel sem depender de uma ferramenta externa de migração.
    with _ENGINE.begin() as conn:
        conn.execute(text(f"ALTER TABLE {schema_sql}.sync_state ADD COLUMN IF NOT EXISTS extra JSONB"))

        # Compatibilidade com bancos criados manualmente a partir do novo modelo
        # app_ferias. Em algumas bases, a tabela colaboradores já existe sem os
        # campos legados usados por serviços estáveis do app. Esses ALTERs precisam
        # vir ANTES de qualquer UPDATE que leia raw_payload/dias_direito.
        conn.execute(text(f"ALTER TABLE {schema_sql}.colaboradores ADD COLUMN IF NOT EXISTS matricula VARCHAR(50)"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.colaboradores ADD COLUMN IF NOT EXISTS dias_direito INTEGER DEFAULT 0"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.colaboradores ADD COLUMN IF NOT EXISTS origem_sheet_id VARCHAR(50)"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.colaboradores ADD COLUMN IF NOT EXISTS origem_row_id VARCHAR(50)"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.colaboradores ADD COLUMN IF NOT EXISTS raw_payload JSONB DEFAULT '{{}}'::jsonb"))

        # Bases antigas podem ter sido criadas sem a matrícula do colaborador.
        # Mantemos o ID inteiro interno como chave técnica, e usamos matricula como ID externo/cadastro.
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


        # Estrutura nova baseada em matrícula: colunas adicionais usadas pela V12.
        conn.execute(text(f"ALTER TABLE {schema_sql}.colaboradores ADD COLUMN IF NOT EXISTS empresa VARCHAR(100)"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.colaboradores ADD COLUMN IF NOT EXISTS unidade VARCHAR(100)"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.colaboradores ADD COLUMN IF NOT EXISTS telefone VARCHAR(50)"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.colaboradores ALTER COLUMN cargo TYPE TEXT"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.colaboradores ALTER COLUMN setor TYPE TEXT"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.solicitacoes_ferias ADD COLUMN IF NOT EXISTS colaborador_matricula VARCHAR(50)"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.solicitacoes_ferias ADD COLUMN IF NOT EXISTS solicitante_matricula VARCHAR(50)"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.solicitacoes_ferias ADD COLUMN IF NOT EXISTS colaborador_email VARCHAR(255)"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.solicitacoes_ferias ADD COLUMN IF NOT EXISTS gestor_solicitante_email VARCHAR(255)"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.solicitacoes_ferias ADD COLUMN IF NOT EXISTS criado_por VARCHAR(255)"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.solicitacoes_ferias ADD COLUMN IF NOT EXISTS solicitacao VARCHAR(255)"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.solicitacoes_ferias ADD COLUMN IF NOT EXISTS saldo_tipo VARCHAR(50) DEFAULT 'REGULAR'"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.solicitacoes_ferias ADD COLUMN IF NOT EXISTS dias INTEGER"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.solicitacoes_ferias ADD COLUMN IF NOT EXISTS is_ajuste BOOLEAN DEFAULT FALSE"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.solicitacoes_ferias ADD COLUMN IF NOT EXISTS metadata JSONB"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.solicitacoes_ferias ADD COLUMN IF NOT EXISTS raw_payload JSONB"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.periodos_aquisitivos ADD COLUMN IF NOT EXISTS colaborador_matricula VARCHAR(50)"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.permissoes_usuario ADD COLUMN IF NOT EXISTS colaborador_matricula VARCHAR(50)"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.hierarquia_gestao ADD COLUMN IF NOT EXISTS colaborador_matricula VARCHAR(50)"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.hierarquia_gestao ADD COLUMN IF NOT EXISTS gestor_direto_matricula VARCHAR(50)"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.hierarquia_gestao ADD COLUMN IF NOT EXISTS gestor_direto_email VARCHAR(255)"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.hierarquia_gestao ADD COLUMN IF NOT EXISTS gestor_superior_matricula VARCHAR(50)"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.auditoria_saldos ADD COLUMN IF NOT EXISTS usuario_alterou_matricula VARCHAR(50)"))

        # Hotfix V16: bancos criados manualmente/por dumps parciais podem ter as tabelas
        # canônicas, mas não todas as colunas que os modelos SQLAlchemy selecionam.
        # Como o SQLAlchemy seleciona todas as colunas mapeadas, a ausência de apenas
        # uma delas derruba a rota /ferias com 500. Estes ALTERs são idempotentes.
        conn.execute(text(f"ALTER TABLE {schema_sql}.saldos_periodo ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"))

        conn.execute(text(f"ALTER TABLE {schema_sql}.solicitacoes_ferias ADD COLUMN IF NOT EXISTS origem_sheet_id VARCHAR(50)"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.solicitacoes_ferias ADD COLUMN IF NOT EXISTS smartsheet_row_id VARCHAR(50)"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.solicitacoes_ferias ADD COLUMN IF NOT EXISTS source_created_at TIMESTAMP"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.solicitacoes_ferias ADD COLUMN IF NOT EXISTS source_modified_at TIMESTAMP"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.solicitacoes_ferias ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.solicitacoes_ferias ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"))

        conn.execute(text(f"ALTER TABLE {schema_sql}.colaborador_complemento ADD COLUMN IF NOT EXISTS colaborador_matricula VARCHAR(50)"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.colaborador_complemento ADD COLUMN IF NOT EXISTS gestor_superior_email VARCHAR(255)"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.colaborador_complemento ADD COLUMN IF NOT EXISTS flags_internas JSONB"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.colaborador_complemento ADD COLUMN IF NOT EXISTS saldo_regular_direito INTEGER DEFAULT 0"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.colaborador_complemento ADD COLUMN IF NOT EXISTS saldo_regular_usado INTEGER DEFAULT 0"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.colaborador_complemento ADD COLUMN IF NOT EXISTS saldo_regular_reservado INTEGER DEFAULT 0"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.colaborador_complemento ADD COLUMN IF NOT EXISTS saldo_regular_disponivel INTEGER DEFAULT 0"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.colaborador_complemento ADD COLUMN IF NOT EXISTS saldo_premium_direito INTEGER DEFAULT 0"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.colaborador_complemento ADD COLUMN IF NOT EXISTS saldo_premium_usado INTEGER DEFAULT 0"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.colaborador_complemento ADD COLUMN IF NOT EXISTS saldo_premium_reservado INTEGER DEFAULT 0"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.colaborador_complemento ADD COLUMN IF NOT EXISTS saldo_premium_disponivel INTEGER DEFAULT 0"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.colaborador_complemento ADD COLUMN IF NOT EXISTS total_solicitacoes INTEGER DEFAULT 0"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.colaborador_complemento ADD COLUMN IF NOT EXISTS periodo_aquisitivo_atual JSONB"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.colaborador_complemento ADD COLUMN IF NOT EXISTS calculated_at TIMESTAMP"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.colaborador_complemento ADD COLUMN IF NOT EXISTS origem_sheet_id VARCHAR(50)"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.colaborador_complemento ADD COLUMN IF NOT EXISTS origem_row_id VARCHAR(50)"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.colaborador_complemento ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.colaborador_complemento ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"))

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


def _to_int_days(value) -> int:
    try:
        return int(round(float(value or 0)))
    except Exception:
        return 0


def _norm_status_for_reserva(status: str) -> str:
    st = str(status or "").strip().upper()
    if st in {"APROVADA", "APROVADO"}:
        return "APROVADO"
    if st in {"PENDENTE", "RESERVA", "RESERVADO", "EM ANALISE", "EM ANÁLISE"}:
        return "PENDENTE"
    return st or "PENDENTE"


def _usuario_por_email(session, email: str):
    email = (email or "").strip().lower()
    if not email:
        return None
    return session.query(Colaborador).filter(Colaborador.email == email).first()


def _atualizar_complemento_cache(session, colab: Colaborador):
    comp = colab.complemento
    if not comp:
        comp = ColaboradorComplemento(colaborador_id=colab.id, colaborador_matricula=colab.matricula, user_type="USER", ativo_no_app=True)
        session.add(comp)
        session.flush()
    rows = (
        session.query(SaldoPeriodo)
        .join(PeriodoAquisitivo, SaldoPeriodo.periodo_id == PeriodoAquisitivo.id)
        .filter(PeriodoAquisitivo.colaborador_matricula == colab.matricula)
        .all()
    )
    def sumtipo(tipo, attr):
        return int(round(sum(float(getattr(r, attr) or 0) for r in rows if (r.tipo_saldo or '').upper() == tipo)))
    comp.colaborador_matricula = colab.matricula
    comp.saldo_regular_direito = sumtipo('REGULAR', 'dias_direito')
    comp.saldo_regular_usado = sumtipo('REGULAR', 'dias_usados')
    comp.saldo_regular_reservado = sumtipo('REGULAR', 'dias_reservados')
    comp.saldo_regular_disponivel = max(0, comp.saldo_regular_direito - comp.saldo_regular_usado - comp.saldo_regular_reservado)
    comp.saldo_premium_direito = sumtipo('PREMIUM', 'dias_direito')
    comp.saldo_premium_usado = sumtipo('PREMIUM', 'dias_usados')
    comp.saldo_premium_reservado = sumtipo('PREMIUM', 'dias_reservados')
    comp.saldo_premium_disponivel = max(0, comp.saldo_premium_direito - comp.saldo_premium_usado - comp.saldo_premium_reservado)
    comp.total_solicitacoes = session.query(Solicitacao).filter(Solicitacao.colaborador_matricula == colab.matricula, Solicitacao.is_ajuste.is_(False)).count()
    comp.calculated_at = datetime.utcnow()
    comp.updated_at = datetime.utcnow()
    return comp


def _reservar_saldo_periodos(session, colab: Colaborador, saldo_tipo: str, dias: int, solicitacao_id: int | None = None, actor: Colaborador | None = None):
    saldo_tipo = (saldo_tipo or 'REGULAR').upper()
    restante = _to_int_days(dias)
    if restante <= 0:
        return []
    saldos = (
        session.query(SaldoPeriodo)
        .join(PeriodoAquisitivo, SaldoPeriodo.periodo_id == PeriodoAquisitivo.id)
        .filter(PeriodoAquisitivo.colaborador_matricula == colab.matricula, SaldoPeriodo.tipo_saldo == saldo_tipo)
        .order_by(PeriodoAquisitivo.data_inicio.asc(), PeriodoAquisitivo.periodo_numero.asc())
        .all()
    )
    movimentos = []
    for saldo in saldos:
        disponivel = float((saldo.dias_direito or 0) - (saldo.dias_usados or 0) - (saldo.dias_reservados or 0))
        if disponivel <= 0:
            continue
        consumir = min(int(disponivel), restante)
        if consumir <= 0:
            continue
        antes = float(saldo.dias_reservados or 0)
        saldo.dias_reservados = antes + consumir
        saldo.updated_at = datetime.utcnow()
        movimentos.append({"saldo_id": saldo.id, "dias": consumir, "periodo_id": saldo.periodo_id})
        session.add(AuditoriaSaldos(
            saldo_id=saldo.id,
            usuario_alterou_id=actor.id if actor else None,
            usuario_alterou_matricula=actor.matricula if actor else None,
            tipo_movimento="RESERVA_SOLICITACAO",
            dias_anteriores=antes,
            dias_alterados=consumir,
            dias_novos=float(saldo.dias_reservados or 0),
            observacoes=f"Reserva criada pela solicitação {solicitacao_id or ''}".strip(),
        ))
        restante -= consumir
        if restante <= 0:
            break
    if restante > 0:
        raise ValueError(f"Saldo insuficiente. Faltam {restante} dia(s).")
    _atualizar_complemento_cache(session, colab)
    return movimentos



def _periodo_destino_ajuste(session, colab: Colaborador):
    periodo = (
        session.query(PeriodoAquisitivo)
        .filter(PeriodoAquisitivo.colaborador_matricula == colab.matricula)
        .order_by(PeriodoAquisitivo.is_atual.desc(), PeriodoAquisitivo.data_inicio.desc(), PeriodoAquisitivo.periodo_numero.desc())
        .first()
    )
    if periodo:
        return periodo
    hoje = date.today()
    periodo = PeriodoAquisitivo(
        colaborador_id=colab.id,
        colaborador_matricula=colab.matricula,
        periodo_numero=1,
        data_inicio=colab.data_admissao or hoje,
        data_fim=hoje + dt.timedelta(days=364),
        is_atual=True,
    )
    session.add(periodo)
    session.flush()
    return periodo


def _aplicar_ajuste_saldo(session, colab: Colaborador, saldo_tipo: str, dias: int, solicitacao_id: int | None, actor: Colaborador | None = None):
    saldo_tipo = (saldo_tipo or 'REGULAR').upper()
    dias = _to_int_days(dias)
    if dias == 0:
        return
    if dias > 0:
        periodo = _periodo_destino_ajuste(session, colab)
        saldo = session.query(SaldoPeriodo).filter(SaldoPeriodo.periodo_id == periodo.id, SaldoPeriodo.tipo_saldo == saldo_tipo).first()
        if not saldo:
            saldo = SaldoPeriodo(periodo_id=periodo.id, tipo_saldo=saldo_tipo, dias_direito=0, dias_reservados=0, dias_usados=0)
            session.add(saldo)
            session.flush()
        antes = float(saldo.dias_direito or 0)
        saldo.dias_direito = antes + dias
        saldo.updated_at = datetime.utcnow()
        session.add(AuditoriaSaldos(
            saldo_id=saldo.id,
            usuario_alterou_id=actor.id if actor else None,
            usuario_alterou_matricula=actor.matricula if actor else None,
            tipo_movimento='AJUSTE_SALDO',
            dias_anteriores=antes,
            dias_alterados=dias,
            dias_novos=float(saldo.dias_direito or 0),
            observacoes=f'Ajuste positivo pela solicitação {solicitacao_id or ""}'.strip(),
        ))
    else:
        restante = abs(dias)
        saldos = (
            session.query(SaldoPeriodo)
            .join(PeriodoAquisitivo, SaldoPeriodo.periodo_id == PeriodoAquisitivo.id)
            .filter(PeriodoAquisitivo.colaborador_matricula == colab.matricula, SaldoPeriodo.tipo_saldo == saldo_tipo)
            .order_by(PeriodoAquisitivo.data_inicio.desc(), PeriodoAquisitivo.periodo_numero.desc())
            .all()
        )
        for saldo in saldos:
            disponivel = float((saldo.dias_direito or 0) - (saldo.dias_usados or 0) - (saldo.dias_reservados or 0))
            if disponivel <= 0:
                continue
            retirar = min(restante, int(disponivel))
            antes = float(saldo.dias_direito or 0)
            saldo.dias_direito = max(0, antes - retirar)
            saldo.updated_at = datetime.utcnow()
            session.add(AuditoriaSaldos(
                saldo_id=saldo.id,
                usuario_alterou_id=actor.id if actor else None,
                usuario_alterou_matricula=actor.matricula if actor else None,
                tipo_movimento='AJUSTE_SALDO',
                dias_anteriores=antes,
                dias_alterados=-retirar,
                dias_novos=float(saldo.dias_direito or 0),
                observacoes=f'Ajuste negativo pela solicitação {solicitacao_id or ""}'.strip(),
            ))
            restante -= retirar
            if restante <= 0:
                break
        if restante > 0:
            raise ValueError(f'Ajuste negativo maior que o saldo disponível. Faltam {restante} dia(s).')
    _atualizar_complemento_cache(session, colab)

def criar_solicitacao(payload: Dict[str, Any]) -> Tuple[bool, str, Optional[int]]:
    """Cria uma nova solicitação no banco novo, gravando matrícula como ID de negócio."""
    session = get_db_session()
    try:
        colaborador_email = (payload.get('colaborador_email') or '').strip().lower()
        gestor_email = (payload.get('gestor_email') or payload.get('criado_por') or '').strip().lower()
        colab = _usuario_por_email(session, colaborador_email)
        if not colab:
            return False, f"Colaborador {colaborador_email} não encontrado", None
        solicitante = _usuario_por_email(session, gestor_email)
        saldo_tipo = (payload.get('saldo_tipo') or 'REGULAR').upper()
        tipo_sol = payload.get('solicitacao', '') or payload.get('tipo_solicitacao', '') or 'GOZO'
        dias = _to_int_days(payload.get('dias', 0))
        status = _norm_status_for_reserva(payload.get('status') or 'PENDENTE')
        is_aj = bool(payload.get('is_ajuste', False)) or str(tipo_sol).strip().upper() == 'AJUSTE'

        solicitacao = Solicitacao(
            colaborador_id=colab.id,
            colaborador_matricula=colab.matricula,
            solicitante_id=solicitante.id if solicitante else None,
            solicitante_matricula=solicitante.matricula if solicitante else None,
            colaborador_email=colaborador_email,
            gestor_solicitante_email=gestor_email,
            criado_por=gestor_email,
            tipo_solicitacao=tipo_sol,
            tipo_ferias=saldo_tipo,
            solicitacao=tipo_sol,
            saldo_tipo=saldo_tipo,
            data_inicio=payload.get('data_inicio'),
            data_fim=payload.get('data_fim'),
            dias_solicitados=dias,
            dias=dias,
            status=status,
            observacoes=payload.get('observacoes', ''),
            is_ajuste=is_aj,
            metadata_json=payload.get('metadata'),
            raw_payload=payload.get('raw_payload'),
            source_created_at=datetime.utcnow(),
        )
        session.add(solicitacao)
        session.flush()

        # Ajustes aprovados alteram o saldo real por período.
        if is_aj and status in {'APROVADO', 'APROVADA'} and dias != 0 and saldo_tipo in {'REGULAR', 'PREMIUM'}:
            _aplicar_ajuste_saldo(session, colab, saldo_tipo, dias, solicitacao.id, solicitante)
        # Solicitação comum pendente reserva saldo por período imediatamente.
        elif not is_aj and status in {'PENDENTE', 'RESERVA', 'RESERVADO'} and dias > 0 and saldo_tipo in {'REGULAR', 'PREMIUM'}:
            _reservar_saldo_periodos(session, colab, saldo_tipo, dias, solicitacao.id, solicitante)
        else:
            _atualizar_complemento_cache(session, colab)

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
