"""Serviço de acesso ao PostgreSQL - substitui smartsheet_service.py"""
from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, date
from typing import Any, Dict, List, Optional, Tuple
import json

from sqlalchemy import create_engine, event, text, func
from sqlalchemy.orm import sessionmaker, Session
from flask import g

from ..config import get_settings
from ..logging_config import get_logger
from ..models import (
    Base, Colaborador, ColaboradorComplemento, Solicitacao, AdminConfig, Auditoria, SyncState, PeriodoAquisitivo, SaldoPeriodo, SaldoPeriodoNovo, AuditoriaSaldos, PermissaoUsuario, HierarquiaGestao
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


def init_db(run_migrations: bool = False):
    """Inicializa a conexão com o banco de dados.

    Por padrão, o Web Service NÃO executa DDL/migrações no startup.
    Scripts manuais de sincronização/recalculo chamam init_db(run_migrations=True).
    """
    global _ENGINE, _SessionLocal
    
    settings = get_settings()
    db_url = settings.database_url
    
    if not db_url:
        raise ValueError(
            "Banco de dados não configurado. Defina DB_TARGET=oficial com PG_HOST/PG_PORT/PG_DB/PG_USER/PG_PASSWORD, "
            "ou DB_TARGET=teste_url com TEST_DATABASE_URL, ou DB_TARGET=database_url com DATABASE_URL."
        )
    
    schema = _db_schema_name()
    schema_sql = _quote_ident(schema)

    # Conexão externa do Render pode derrubar sessões SSL que ficam paradas
    # enquanto o Smartsheet responde. pool_pre_ping ajuda, pool_recycle força
    # renovação periódica e keepalives reduzem quedas em sincronizações longas.
    connect_args = {
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 5,
    }
    _ENGINE = create_engine(
        db_url,
        echo=False,
        pool_pre_ping=True,
        pool_recycle=60,
        pool_timeout=30,
        pool_reset_on_return="rollback",
        connect_args=connect_args,
    )

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

    _SessionLocal = sessionmaker(bind=_ENGINE, expire_on_commit=False)

    # V40: o Web Service nunca deve executar DDL/migrações no startup.
    # Isso evita travar o deploy antes de abrir a porta do Render.
    # Apenas scripts manuais devem chamar init_db(run_migrations=True).
    if not run_migrations:
        log.info(
            "Banco de dados PostgreSQL inicializado no schema %s (DB_TARGET=%s, DDL inicial desativado no Web Service)",
            schema,
            getattr(settings, "db_target", "auto"),
        )
        return

    # Garante o schema antes do create_all. A aplicação usa modelos sem schema fixo,
    # então o search_path precisa apontar para app_ferias; caso contrário o SQLAlchemy
    # pode procurar/criar tabelas no public e não enxergar os dados importados.
    with _ENGINE.begin() as conn:
        conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema_sql}"))
        conn.execute(text(f"SET search_path TO {schema_sql}, public"))
        safe_tz = str(getattr(settings, "app_timezone", "America/Fortaleza") or "America/Fortaleza").replace("'", "''")
        conn.execute(text(f"SET TIME ZONE '{safe_tz}'"))

    # Cria as tabelas se não existirem dentro do schema configurado
    Base.metadata.create_all(_ENGINE)

    # Pequenas migrações defensivas para bases já criadas antes das últimas versões.
    # create_all() não altera tabelas existentes; por isso garantimos colunas novas
    # usadas pelo painel sem depender de uma ferramenta externa de migração.
    with _ENGINE.begin() as conn:
        # Se a base antiga tiver status_solicitacao_enum, garante valores usados pela sincronização.
        # Em bases novas a coluna é texto; nesse caso o bloco não faz nada.
        conn.execute(text(f"""
        DO $$
        DECLARE
            enum_reg regtype;
            enum_value text;
            enum_values text[] := ARRAY['PENDENTE','APROVADO','APROVADA','CANCELADO','CANCELADA','REPROVADO','REPROVADA','RESERVA','RESERVADO'];
        BEGIN
            SELECT to_regtype('{schema}.status_solicitacao_enum') INTO enum_reg;
            IF enum_reg IS NOT NULL THEN
                FOREACH enum_value IN ARRAY enum_values LOOP
                    IF NOT EXISTS (
                        SELECT 1
                          FROM pg_enum e
                         WHERE e.enumtypid = enum_reg
                           AND e.enumlabel = enum_value
                    ) THEN
                        EXECUTE format('ALTER TYPE %s ADD VALUE %L', enum_reg::text, enum_value);
                    END IF;
                END LOOP;
            END IF;
        END $$;
        """))

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
        conn.execute(text(f"ALTER TABLE {schema_sql}.solicitacoes_ferias ADD COLUMN IF NOT EXISTS periodo_aquisitivo_origem TEXT"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.solicitacoes_ferias ADD COLUMN IF NOT EXISTS raw_payload JSONB"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.periodos_aquisitivos ADD COLUMN IF NOT EXISTS colaborador_matricula VARCHAR(50)"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.permissoes_usuario ADD COLUMN IF NOT EXISTS colaborador_matricula VARCHAR(50)"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.hierarquia_gestao ADD COLUMN IF NOT EXISTS colaborador_matricula VARCHAR(50)"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.hierarquia_gestao ADD COLUMN IF NOT EXISTS gestor_direto_matricula VARCHAR(50)"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.hierarquia_gestao ADD COLUMN IF NOT EXISTS gestor_direto_email VARCHAR(255)"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.hierarquia_gestao ADD COLUMN IF NOT EXISTS gestor_superior_matricula VARCHAR(50)"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.hierarquia_gestao ADD COLUMN IF NOT EXISTS gestor_superior_email VARCHAR(255)"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.auditoria_saldos ADD COLUMN IF NOT EXISTS usuario_alterou_matricula VARCHAR(50)"))

        # Hotfix V16: bancos criados manualmente/por dumps parciais podem ter as tabelas
        # canônicas, mas não todas as colunas que os modelos SQLAlchemy selecionam.
        # Como o SQLAlchemy seleciona todas as colunas mapeadas, a ausência de apenas
        # uma delas derruba a rota /ferias com 500. Estes ALTERs são idempotentes.
        conn.execute(text(f"ALTER TABLE {schema_sql}.saldos_periodo ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"))
        conn.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {schema_sql}.saldo_periodo (
            id SERIAL PRIMARY KEY,
            colaborador_id INTEGER NOT NULL REFERENCES {schema_sql}.colaboradores(id),
            colaborador_matricula VARCHAR(50) NOT NULL REFERENCES {schema_sql}.colaboradores(matricula),
            periodo_numero INTEGER NOT NULL,
            data_inicio DATE NOT NULL,
            data_fim DATE NOT NULL,
            is_atual BOOLEAN DEFAULT FALSE,
            tipo_saldo VARCHAR(20) NOT NULL DEFAULT 'REGULAR',
            saldo_inicial NUMERIC(6,2) DEFAULT 0,
            saldo_utilizado NUMERIC(6,2) DEFAULT 0,
            saldo_reservado NUMERIC(6,2) DEFAULT 0,
            saldo_disponivel NUMERIC(6,2) DEFAULT 0,
            ultima_alteracao TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_saldo_periodo_matricula_periodo_tipo UNIQUE (colaborador_matricula, periodo_numero, tipo_saldo)
        )
        """))
        conn.execute(text(f"CREATE INDEX IF NOT EXISTS ix_saldo_periodo_matricula_tipo ON {schema_sql}.saldo_periodo (colaborador_matricula, tipo_saldo)"))
        conn.execute(text(f"CREATE INDEX IF NOT EXISTS ix_saldo_periodo_colaborador ON {schema_sql}.saldo_periodo (colaborador_id)"))

        conn.execute(text(f"ALTER TABLE {schema_sql}.solicitacoes_ferias ADD COLUMN IF NOT EXISTS origem_sheet_id VARCHAR(50)"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.solicitacoes_ferias ADD COLUMN IF NOT EXISTS smartsheet_row_id VARCHAR(50)"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.solicitacoes_ferias ADD COLUMN IF NOT EXISTS source_created_at TIMESTAMP"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.solicitacoes_ferias ADD COLUMN IF NOT EXISTS source_modified_at TIMESTAMP"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.solicitacoes_ferias ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.solicitacoes_ferias ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"))

        conn.execute(text(f"ALTER TABLE {schema_sql}.colaborador_complemento ADD COLUMN IF NOT EXISTS colaborador_matricula VARCHAR(50)"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.colaborador_complemento ADD COLUMN IF NOT EXISTS gestor_superior_email VARCHAR(255)"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.colaborador_complemento ADD COLUMN IF NOT EXISTS gestor_direto VARCHAR(50)"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.colaborador_complemento ADD COLUMN IF NOT EXISTS gestor_superior VARCHAR(50)"))
        conn.execute(text(f"CREATE INDEX IF NOT EXISTS ix_colaborador_complemento_gestor_direto ON {schema_sql}.colaborador_complemento (gestor_direto)"))
        conn.execute(text(f"CREATE INDEX IF NOT EXISTS ix_colaborador_complemento_gestor_superior ON {schema_sql}.colaborador_complemento (gestor_superior)"))
        conn.execute(text(f"""
            UPDATE {schema_sql}.colaborador_complemento cc
               SET gestor_direto = COALESCE(NULLIF(cc.gestor_direto, ''), h.gestor_direto_matricula)
              FROM {schema_sql}.hierarquia_gestao h
             WHERE h.colaborador_matricula = cc.colaborador_matricula
               AND h.gestor_direto_matricula IS NOT NULL
               AND (cc.gestor_direto IS NULL OR btrim(cc.gestor_direto) = '')
        """))
        conn.execute(text(f"""
            UPDATE {schema_sql}.colaborador_complemento cc
               SET gestor_direto = g.matricula
              FROM {schema_sql}.colaboradores g
             WHERE lower(g.email) = lower(cc.gestor_direto_email)
               AND upper(coalesce(g.status, 'ATIVO')) IN ('ATIVO','ACTIVE')
               AND (cc.gestor_direto IS NULL OR btrim(cc.gestor_direto) = '')
        """))
        conn.execute(text(f"""
            UPDATE {schema_sql}.colaborador_complemento cc
               SET gestor_superior = COALESCE(NULLIF(cc.gestor_superior, ''), h.gestor_superior_matricula)
              FROM {schema_sql}.hierarquia_gestao h
             WHERE h.colaborador_matricula = cc.colaborador_matricula
               AND h.gestor_superior_matricula IS NOT NULL
               AND (cc.gestor_superior IS NULL OR btrim(cc.gestor_superior) = '')
        """))
        conn.execute(text(f"""
            UPDATE {schema_sql}.colaborador_complemento
               SET gestor_superior = upper(gestor_superior_email)
             WHERE lower(coalesce(gestor_superior_email, '')) IN ('dp', 'gestor')
               AND (gestor_superior IS NULL OR btrim(gestor_superior) = '')
        """))
        conn.execute(text(f"""
            UPDATE {schema_sql}.colaborador_complemento cc
               SET gestor_superior = g.matricula
              FROM {schema_sql}.colaboradores g
             WHERE lower(g.email) = lower(cc.gestor_superior_email)
               AND upper(coalesce(g.status, 'ATIVO')) IN ('ATIVO','ACTIVE')
               AND (cc.gestor_superior IS NULL OR btrim(cc.gestor_superior) = '')
        """))
        conn.execute(text(f"ALTER TABLE {schema_sql}.colaborador_complemento ADD COLUMN IF NOT EXISTS flags_internas JSONB"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.colaborador_complemento ADD COLUMN IF NOT EXISTS calculated_at TIMESTAMP"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.colaborador_complemento ADD COLUMN IF NOT EXISTS origem_sheet_id VARCHAR(50)"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.colaborador_complemento ADD COLUMN IF NOT EXISTS origem_row_id VARCHAR(50)"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.colaborador_complemento ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"))
        conn.execute(text(f"ALTER TABLE {schema_sql}.colaborador_complemento ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"))

    log.info("Banco de dados PostgreSQL inicializado no schema %s (DB_TARGET=%s)", schema, getattr(settings, "db_target", "auto"))


def get_db_session() -> Session:
    """Obtém a sessão do banco para o request atual."""
    if not hasattr(g, '_db_session') or g._db_session is None:
        if _SessionLocal is None:
            init_db(run_migrations=False)
        g._db_session = _SessionLocal()
    return g._db_session



def dispose_engine():
    """Descarta conexões do pool para forçar nova conexão no próximo uso.

    Útil no sincronizador local: o app inicializa o banco, depois espera o
    Smartsheet responder por vários minutos; nesse intervalo a conexão SSL
    externa do Render pode ser encerrada.
    """
    global _ENGINE
    if _ENGINE is not None:
        try:
            _ENGINE.dispose()
            log.info("Pool PostgreSQL descartado; próxima sessão abrirá nova conexão.")
        except Exception as exc:  # pragma: no cover
            log.warning("Falha ao descartar pool PostgreSQL: %s", exc)


@contextmanager
def get_session():
    """Context manager para criar e fechar uma sessão."""
    if _SessionLocal is None:
        init_db(run_migrations=False)
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
    """Retorna dados do colaborador por matrícula ou e-mail.

    A matrícula é a chave operacional. O e-mail é aceito por compatibilidade,
    sempre priorizando cadastro ATIVO quando houver duplicidade.
    """
    session = get_db_session()
    try:
        ident = str(email or '').strip()
        colab = None
        if ident and '@' not in ident:
            colab = session.query(Colaborador).filter(func.upper(Colaborador.matricula) == ident.upper()).first()
        if not colab:
            rows = session.query(Colaborador).filter(
                func.lower(Colaborador.email) == ident.lower()
            ).all()
            rows.sort(key=lambda c: (1 if str(c.status or '').strip().upper() in {'ATIVO', 'ACTIVE'} else 0, int(c.id or 0)), reverse=True)
            colab = rows[0] if rows else None

        if not colab:
            return None
        result = colab.to_dict()
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
            query = query.filter(func.upper(func.coalesce(Colaborador.status, 'ATIVO')) == str(status_filter).upper())
        
        colaboradores = query.order_by(Colaborador.nome_completo).all()
        
        return [colab.to_dict() for colab in colaboradores]
    except Exception as e:
        log.error(f"Erro ao listar colaboradores: {e}")
        return []


def get_saldos_colaborador(identificador: str) -> Dict[str, Any]:
    """Retorna saldos consolidados diretamente de saldo_periodo.

    A matrícula é a chave operacional. O e-mail é aceito apenas por
    compatibilidade para descobrir a matrícula ativa antes da consulta.
    """
    session = get_db_session()
    empty = {
        'regular': {'direito': 0, 'usado': 0, 'reservado': 0, 'disponivel': 0},
        'premium': {'direito': 0, 'usado': 0, 'reservado': 0, 'disponivel': 0},
    }
    try:
        ident = str(identificador or '').strip()
        colab = None
        if ident and '@' not in ident:
            colab = session.query(Colaborador).filter(func.upper(Colaborador.matricula) == ident.upper()).first()
        if not colab and ident:
            rows = session.query(Colaborador).filter(func.lower(Colaborador.email) == ident.lower()).all()
            rows.sort(key=lambda c: (1 if str(c.status or '').strip().upper() in {'ATIVO', 'ACTIVE'} else 0, int(c.id or 0)), reverse=True)
            colab = rows[0] if rows else None
        if not colab or not colab.matricula:
            return empty

        rows = (
            session.query(SaldoPeriodoNovo)
            .filter(func.upper(SaldoPeriodoNovo.colaborador_matricula) == str(colab.matricula).upper())
            .all()
        )

        def sumtipo(tipo: str, attr: str) -> int:
            return int(round(sum(float(getattr(r, attr) or 0) for r in rows if (r.tipo_saldo or '').upper() == tipo)))

        return {
            'regular': {
                'direito': sumtipo('REGULAR', 'saldo_inicial'),
                'usado': sumtipo('REGULAR', 'saldo_utilizado'),
                'reservado': sumtipo('REGULAR', 'saldo_reservado'),
                'disponivel': sumtipo('REGULAR', 'saldo_disponivel'),
            },
            'premium': {
                'direito': sumtipo('PREMIUM', 'saldo_inicial'),
                'usado': sumtipo('PREMIUM', 'saldo_utilizado'),
                'reservado': sumtipo('PREMIUM', 'saldo_reservado'),
                'disponivel': sumtipo('PREMIUM', 'saldo_disponivel'),
            },
        }
    except Exception as e:
        log.error(f"Erro ao obter saldos do colaborador {identificador}: {e}")
        return empty


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
    rows = session.query(Colaborador).filter(func.lower(Colaborador.email) == email).all()
    if rows:
        rows.sort(key=lambda c: (1 if str(c.status or '').strip().upper() in {'ATIVO', 'ACTIVE'} else 0, int(c.id or 0)), reverse=True)
        return rows[0]
    local = email.split('@', 1)[0] if '@' in email else email
    rows = session.query(Colaborador).filter(func.split_part(func.lower(Colaborador.email), '@', 1) == local).all()
    rows = [c for c in rows if str(c.status or '').strip().upper() in {'ATIVO', 'ACTIVE'}]
    rows.sort(key=lambda c: int(c.id or 0), reverse=True)
    return rows[0] if rows else None


def _format_periodo_alloc_v29(movimentos: List[Dict[str, Any]]) -> str:
    partes = []
    for m in movimentos or []:
        numero = int(m.get('periodo_numero') or 0)
        dias = float(m.get('dias') or 0)
        if numero <= 0 or dias <= 0:
            continue
        dias_txt = str(int(dias)) if float(dias).is_integer() else str(round(dias, 2)).rstrip('0').rstrip('.')
        partes.append(f"P{numero}:{dias_txt}")
    return " | ".join(partes)


def _parse_periodo_alloc_v29(value: Any) -> List[Dict[str, Any]]:
    import re
    text = str(value or '').strip()
    out: List[Dict[str, Any]] = []
    for numero, dias in re.findall(r"P\s*(\d+)\s*[:=\-]\s*(\d+(?:[\.,]\d+)?)", text, flags=re.IGNORECASE):
        try:
            out.append({'periodo_numero': int(numero), 'dias': float(str(dias).replace(',', '.'))})
        except Exception:
            continue
    return out


def _saldo_periodo_por_numero_v29(session, colab: Colaborador, tipo_saldo: str, numero: int):
    return (
        session.query(SaldoPeriodoNovo)
        .filter(
            SaldoPeriodoNovo.colaborador_matricula == colab.matricula,
            SaldoPeriodoNovo.tipo_saldo == (tipo_saldo or 'REGULAR').upper(),
            SaldoPeriodoNovo.periodo_numero == int(numero or 0),
        )
        .first()
    )


def _mover_saldo_status_v29(session, colab: Colaborador, solicitacao: Solicitacao, old_status: str, new_status: str):
    saldo_tipo = (solicitacao.saldo_tipo or solicitacao.tipo_ferias or 'REGULAR').upper()
    dias = _to_int_days(solicitacao.dias or solicitacao.dias_solicitados or 0)
    if dias <= 0 or saldo_tipo not in {'REGULAR', 'PREMIUM'}:
        _atualizar_complemento_cache(session, colab)
        return
    if bool(solicitacao.is_ajuste):
        if new_status in {'APROVADO', 'APROVADA'} and old_status not in {'APROVADO', 'APROVADA'}:
            movimentos = _aplicar_ajuste_saldo(session, colab, saldo_tipo, dias, solicitacao.id, None)
            if movimentos:
                solicitacao.periodo_aquisitivo_origem = _format_periodo_alloc_v29(movimentos)
        return
    alloc = _parse_periodo_alloc_v29(solicitacao.periodo_aquisitivo_origem)
    # Aprovação: transforma reserva em utilizado quando houver reserva; se não houver origem, debita do saldo disponível.
    if new_status in {'APROVADO', 'APROVADA'} and old_status not in {'APROVADO', 'APROVADA'}:
        if not alloc:
            movimentos = _reservar_saldo_periodos(session, colab, saldo_tipo, dias, solicitacao.id, None)
            alloc = movimentos
            solicitacao.periodo_aquisitivo_origem = _format_periodo_alloc_v29(movimentos)
        for item in alloc:
            saldo = _saldo_periodo_por_numero_v29(session, colab, saldo_tipo, int(item.get('periodo_numero') or 0))
            if not saldo:
                continue
            qtd = float(item.get('dias') or 0)
            reserva_abater = min(float(saldo.saldo_reservado or 0), qtd)
            saldo.saldo_reservado = max(0, float(saldo.saldo_reservado or 0) - reserva_abater)
            falta = max(0, qtd - reserva_abater)
            if falta:
                saldo.saldo_disponivel = max(0, float(saldo.saldo_disponivel or 0) - falta)
            saldo.saldo_utilizado = float(saldo.saldo_utilizado or 0) + qtd
            saldo.ultima_alteracao = datetime.utcnow()
            saldo.updated_at = datetime.utcnow()
        _atualizar_complemento_cache(session, colab)
        return
    # Cancelamento/reprovação: libera a reserva ou estorna o uso, usando o mapa gravado.
    if new_status in {'CANCELADO', 'CANCELADA', 'REPROVADO', 'REPROVADA'} and old_status not in {'CANCELADO', 'CANCELADA', 'REPROVADO', 'REPROVADA'}:
        for item in alloc:
            saldo = _saldo_periodo_por_numero_v29(session, colab, saldo_tipo, int(item.get('periodo_numero') or 0))
            if not saldo:
                continue
            qtd = float(item.get('dias') or 0)
            if old_status in {'APROVADO', 'APROVADA'}:
                abater = min(float(saldo.saldo_utilizado or 0), qtd)
                saldo.saldo_utilizado = max(0, float(saldo.saldo_utilizado or 0) - abater)
            else:
                abater = min(float(saldo.saldo_reservado or 0), qtd)
                saldo.saldo_reservado = max(0, float(saldo.saldo_reservado or 0) - abater)
            saldo.saldo_disponivel = float(saldo.saldo_disponivel or 0) + qtd
            saldo.ultima_alteracao = datetime.utcnow()
            saldo.updated_at = datetime.utcnow()
        _atualizar_complemento_cache(session, colab)


def _saldo_periodo_destino_ajuste_v29(session, colab: Colaborador, saldo_tipo: str):
    saldo_tipo = (saldo_tipo or 'REGULAR').upper()
    saldo = (
        session.query(SaldoPeriodoNovo)
        .filter(SaldoPeriodoNovo.colaborador_matricula == colab.matricula, SaldoPeriodoNovo.tipo_saldo == saldo_tipo)
        .order_by(SaldoPeriodoNovo.is_atual.desc(), SaldoPeriodoNovo.data_inicio.desc(), SaldoPeriodoNovo.periodo_numero.desc())
        .first()
    )
    if saldo:
        return saldo
    hoje = date.today()
    adm = colab.data_admissao or hoje
    saldo = SaldoPeriodoNovo(
        colaborador_id=colab.id,
        colaborador_matricula=colab.matricula,
        periodo_numero=1,
        data_inicio=adm,
        data_fim=adm + dt.timedelta(days=364),
        is_atual=True,
        tipo_saldo=saldo_tipo,
        saldo_inicial=0,
        saldo_utilizado=0,
        saldo_reservado=0,
        saldo_disponivel=0,
        ultima_alteracao=datetime.utcnow(),
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    session.add(saldo)
    session.flush()
    return saldo


def _atualizar_complemento_cache(session, colab: Colaborador):
    """Mantém apenas metadados operacionais em colaborador_complemento.

    Desde a V43, os totais de saldo e total de solicitações não são mais
    gravados nesta tabela. A fonte oficial é saldo_periodo.
    """
    comp = colab.complemento
    if not comp:
        comp = ColaboradorComplemento(
            colaborador_id=colab.id,
            colaborador_matricula=colab.matricula,
            user_type="USER",
            ativo_no_app=True,
        )
        session.add(comp)
        session.flush()
    comp.colaborador_matricula = colab.matricula
    comp.calculated_at = datetime.utcnow()
    comp.updated_at = datetime.utcnow()
    return comp


def _reservar_saldo_periodos(session, colab: Colaborador, saldo_tipo: str, dias: int, solicitacao_id: int | None = None, actor: Colaborador | None = None):
    saldo_tipo = (saldo_tipo or 'REGULAR').upper()
    restante = _to_int_days(dias)
    if restante <= 0:
        return []
    saldos = (
        session.query(SaldoPeriodoNovo)
        .filter(SaldoPeriodoNovo.colaborador_matricula == colab.matricula, SaldoPeriodoNovo.tipo_saldo == saldo_tipo)
        .order_by(SaldoPeriodoNovo.data_inicio.asc(), SaldoPeriodoNovo.periodo_numero.asc())
        .all()
    )
    movimentos = []
    for saldo in saldos:
        disponivel = float(saldo.saldo_disponivel or 0)
        if disponivel <= 0:
            continue
        consumir = min(int(disponivel), restante)
        if consumir <= 0:
            continue
        saldo.saldo_reservado = float(saldo.saldo_reservado or 0) + consumir
        saldo.saldo_disponivel = max(0, float(saldo.saldo_disponivel or 0) - consumir)
        saldo.ultima_alteracao = datetime.utcnow()
        saldo.updated_at = datetime.utcnow()
        movimentos.append({"saldo_id": saldo.id, "periodo_numero": saldo.periodo_numero, "dias": consumir})
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
    movimentos = []
    if dias == 0:
        return movimentos
    if dias > 0:
        saldo = _saldo_periodo_destino_ajuste_v29(session, colab, saldo_tipo)
        saldo.saldo_inicial = float(saldo.saldo_inicial or 0) + dias
        saldo.saldo_disponivel = float(saldo.saldo_disponivel or 0) + dias
        saldo.ultima_alteracao = datetime.utcnow()
        saldo.updated_at = datetime.utcnow()
        movimentos.append({"saldo_id": saldo.id, "periodo_numero": saldo.periodo_numero, "dias": dias})
    else:
        restante = abs(dias)
        saldos = (
            session.query(SaldoPeriodoNovo)
            .filter(SaldoPeriodoNovo.colaborador_matricula == colab.matricula, SaldoPeriodoNovo.tipo_saldo == saldo_tipo)
            .order_by(SaldoPeriodoNovo.data_inicio.asc(), SaldoPeriodoNovo.periodo_numero.asc())
            .all()
        )
        for saldo in saldos:
            disponivel = float(saldo.saldo_disponivel or 0)
            if disponivel <= 0:
                continue
            retirar = min(int(disponivel), restante)
            saldo.saldo_utilizado = float(saldo.saldo_utilizado or 0) + retirar
            saldo.saldo_disponivel = max(0, float(saldo.saldo_disponivel or 0) - retirar)
            saldo.ultima_alteracao = datetime.utcnow()
            saldo.updated_at = datetime.utcnow()
            movimentos.append({"saldo_id": saldo.id, "periodo_numero": saldo.periodo_numero, "dias": retirar})
            restante -= retirar
            if restante <= 0:
                break
        if restante > 0:
            raise ValueError(f'Ajuste negativo maior que o saldo disponível. Faltam {restante} dia(s).')
    _atualizar_complemento_cache(session, colab)
    return movimentos

def criar_solicitacao(payload: Dict[str, Any]) -> Tuple[bool, str, Optional[int]]:
    """Cria uma nova solicitação no banco novo, gravando matrícula como ID de negócio."""
    session = get_db_session()
    try:
        colaborador_email = (payload.get('colaborador_email') or '').strip().lower()
        colaborador_matricula = (payload.get('colaborador_matricula') or payload.get('matricula') or '').strip().upper()
        gestor_email = (payload.get('gestor_email') or payload.get('criado_por') or '').strip().lower()
        colab = None
        if colaborador_matricula:
            colab = session.query(Colaborador).filter(func.upper(Colaborador.matricula) == colaborador_matricula).first()
        if not colab:
            colab = _usuario_por_email(session, colaborador_email)
        if not colab:
            return False, f"Colaborador {colaborador_matricula or colaborador_email} não encontrado", None
        # O e-mail gravado é apenas informativo; a matrícula é a referência oficial.
        if not colaborador_email:
            colaborador_email = (colab.email or '').strip().lower()
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
            periodo_aquisitivo_origem=(payload.get('metadata') or {}).get('periodo_aquisitivo') if isinstance(payload.get('metadata'), dict) else None,
            source_created_at=datetime.utcnow(),
        )
        session.add(solicitacao)
        session.flush()

        # Ajustes aprovados alteram o saldo real por período.
        if is_aj and status in {'APROVADO', 'APROVADA'} and dias != 0 and saldo_tipo in {'REGULAR', 'PREMIUM'}:
            movimentos = _aplicar_ajuste_saldo(session, colab, saldo_tipo, dias, solicitacao.id, solicitante)
            if movimentos:
                solicitacao.periodo_aquisitivo_origem = _format_periodo_alloc_v29(movimentos)
        # Solicitação comum aprovada debita saldo por período imediatamente.
        elif not is_aj and status in {'APROVADO', 'APROVADA'} and dias > 0 and saldo_tipo in {'REGULAR', 'PREMIUM'}:
            movimentos = _reservar_saldo_periodos(session, colab, saldo_tipo, dias, solicitacao.id, solicitante)
            solicitacao.periodo_aquisitivo_origem = _format_periodo_alloc_v29(movimentos)
            _mover_saldo_status_v29(session, colab, solicitacao, 'PENDENTE', 'APROVADO')
        # Solicitação comum pendente reserva saldo por período imediatamente.
        elif not is_aj and status in {'PENDENTE', 'RESERVA', 'RESERVADO'} and dias > 0 and saldo_tipo in {'REGULAR', 'PREMIUM'}:
            movimentos = _reservar_saldo_periodos(session, colab, saldo_tipo, dias, solicitacao.id, solicitante)
            if movimentos:
                solicitacao.periodo_aquisitivo_origem = _format_periodo_alloc_v29(movimentos)
        else:
            _atualizar_complemento_cache(session, colab)

        session.commit()
        return True, "Solicitação criada com sucesso", solicitacao.id
    except Exception as e:
        log.error(f"Erro ao criar solicitação: {e}")
        session.rollback()
        return False, str(e), None


def atualizar_solicitacao(solicitacao_id: int, payload: Dict[str, Any]) -> Tuple[bool, str]:
    """Atualiza uma solicitação existente e reflete alteração de status em saldo_periodo."""
    session = get_db_session()
    try:
        solicitacao = session.query(Solicitacao).filter(
            Solicitacao.id == solicitacao_id
        ).first()

        if not solicitacao:
            return False, "Solicitação não encontrada"

        old_status = _norm_status_for_reserva(solicitacao.status or '')
        new_status = old_status

        if 'status' in payload:
            new_status = _norm_status_for_reserva(payload['status'])
            solicitacao.status = new_status
        if 'observacoes' in payload:
            solicitacao.observacoes = payload['observacoes']
        if 'dias' in payload:
            solicitacao.dias = payload['dias']
            solicitacao.dias_solicitados = payload['dias']
        if 'periodo_aquisitivo_origem' in payload:
            solicitacao.periodo_aquisitivo_origem = payload.get('periodo_aquisitivo_origem')

        colab = None
        if solicitacao.colaborador_id:
            colab = session.query(Colaborador).filter(Colaborador.id == solicitacao.colaborador_id).first()
        if not colab and solicitacao.colaborador_matricula:
            colab = session.query(Colaborador).filter(Colaborador.matricula == solicitacao.colaborador_matricula).first()

        if colab and new_status != old_status:
            _mover_saldo_status_v29(session, colab, solicitacao, old_status, new_status)

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


def atualizar_saldos_colaborador(*args, **kwargs) -> bool:
    """Função descontinuada.

    Os saldos não são mais gravados em colaborador_complemento. Use a tabela
    saldo_periodo ou os fluxos de solicitação/ajuste que movimentam saldos por
    matrícula e período.
    """
    log.warning("atualizar_saldos_colaborador foi chamada, mas está descontinuada desde a V43; use saldo_periodo.")
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
