"""Modelos SQLAlchemy para o banco de dados PostgreSQL."""
from __future__ import annotations

from datetime import datetime, date
from typing import Optional
import json
import os
import re

from sqlalchemy import (
    Column, Integer, String, Date, DateTime, Boolean, JSON, Text,
    ForeignKey, create_engine, event
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship, Session
from sqlalchemy.dialects.postgresql import JSON as PGJSON

Base = declarative_base()


def _model_schema_name() -> str:
    """Schema PostgreSQL onde as tabelas do app ficam.

    Mantém o mesmo padrão do postgres_service. Usar schema explícito nos
    modelos evita que conexões com search_path diferente consultem tabelas
    duplicadas no public ou criem estruturas fora de ferias_app.
    """
    schema = (os.getenv("DB_SCHEMA") or "ferias_app").strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
        schema = "ferias_app"
    return schema


_MODEL_SCHEMA = _model_schema_name()



class Colaborador(Base):
    """Cadastro base do colaborador - origem CONTROLE_DP (Smartsheet)."""
    __tablename__ = 'colaboradores'
    __table_args__ = {'schema': _MODEL_SCHEMA}

    id = Column(Integer, primary_key=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    matricula = Column(String(50), nullable=True, index=True)  # ID externo/código de cadastro vindo da coluna MATRÍCULA
    nome_completo = Column(String(255), nullable=True)
    status = Column(String(50), nullable=True)  # ATIVO, INATIVO
    data_admissao = Column(Date, nullable=True)
    setor = Column(String(150), nullable=True)
    cargo = Column(String(150), nullable=True)
    regime = Column(String(100), nullable=True)  # CLT, PJ, etc
    dias_direito = Column(Integer, nullable=False, default=0, server_default="0")  # dias de direito base
    origem_sheet_id = Column(String(50), nullable=True)  # ID da sheet no Smartsheet
    origem_row_id = Column(String(50), nullable=True)  # ID da linha no Smartsheet
    raw_payload = Column(PGJSON, nullable=True)  # JSON com dados originais completos
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # Relacionamentos
    complemento = relationship("ColaboradorComplemento", back_populates="colaborador", uselist=False, cascade="all, delete-orphan")
    solicitacoes = relationship("Solicitacao", back_populates="colaborador", cascade="all, delete-orphan")

    def to_dict(self):
        return {
            'id': self.id,
            'email': self.email,
            'matricula': self.matricula,
            'nome_completo': self.nome_completo,
            'status': self.status,
            'data_admissao': self.data_admissao.isoformat() if self.data_admissao else None,
            'setor': self.setor,
            'cargo': self.cargo,
            'regime': self.regime,
            'dias_direito': self.dias_direito,
        }


class ColaboradorComplemento(Base):
    """Dados complementares e saldos calculados do colaborador."""
    __tablename__ = 'colaborador_complemento'
    __table_args__ = {'schema': _MODEL_SCHEMA}

    id = Column(Integer, primary_key=True)
    colaborador_id = Column(Integer, ForeignKey(f'{_MODEL_SCHEMA}.colaboradores.id'), nullable=False, unique=True)
    user_type = Column(String(50), nullable=True)  # USER, DP, ADMIN
    gestor_direto_email = Column(String(255), nullable=True)
    gestor_superior_email = Column(String(255), nullable=True)
    ativo_no_app = Column(Boolean, default=True)
    flags_internas = Column(PGJSON, nullable=True)  # JSON para flags futuras

    # Saldos regulares
    saldo_regular_direito = Column(Integer, default=0)
    saldo_regular_usado = Column(Integer, default=0)
    saldo_regular_reservado = Column(Integer, default=0)
    saldo_regular_disponivel = Column(Integer, default=0)

    # Saldos premium/licença
    saldo_premium_direito = Column(Integer, default=0)
    saldo_premium_usado = Column(Integer, default=0)
    saldo_premium_reservado = Column(Integer, default=0)
    saldo_premium_disponivel = Column(Integer, default=0)

    total_solicitacoes = Column(Integer, default=0)
    periodo_aquisitivo_atual = Column(PGJSON, nullable=True)  # JSON com período vigente
    calculated_at = Column(DateTime, nullable=True)  # Quando foram calculados os saldos
    origem_sheet_id = Column(String(50), nullable=True)
    origem_row_id = Column(String(50), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # Relacionamentos
    colaborador = relationship("Colaborador", back_populates="complemento")

    def to_dict(self):
        return {
            'user_type': self.user_type,
            'gestor_direto_email': self.gestor_direto_email,
            'ativo_no_app': self.ativo_no_app,
            'saldo_regular': {
                'direito': self.saldo_regular_direito,
                'usado': self.saldo_regular_usado,
                'reservado': self.saldo_regular_reservado,
                'disponivel': self.saldo_regular_disponivel,
            },
            'saldo_premium': {
                'direito': self.saldo_premium_direito,
                'usado': self.saldo_premium_usado,
                'reservado': self.saldo_premium_reservado,
                'disponivel': self.saldo_premium_disponivel,
            },
        }


class Solicitacao(Base):
    """Solicitações, reservas e ajustes de férias/licença."""
    __tablename__ = 'solicitacoes'
    __table_args__ = {'schema': _MODEL_SCHEMA}

    id = Column(Integer, primary_key=True)
    origem_sheet_id = Column(String(50), nullable=True)  # ID da sheet Smartsheet
    smartsheet_row_id = Column(String(50), nullable=True)  # ID da linha no Smartsheet
    colaborador_id = Column(Integer, ForeignKey(f'{_MODEL_SCHEMA}.colaboradores.id'), nullable=True)
    colaborador_email = Column(String(255), nullable=False, index=True)
    gestor_solicitante_email = Column(String(255), nullable=True)
    criado_por = Column(String(255), nullable=True)
    solicitacao = Column(String(255), nullable=True)  # Ex: AJUSTE FÉRIAS, GOZO, LICENÇA
    saldo_tipo = Column(String(50), nullable=False, default='REGULAR')  # REGULAR ou PREMIUM
    data_inicio = Column(Date, nullable=False)
    data_fim = Column(Date, nullable=False)
    dias = Column(Integer, nullable=False)
    status = Column(String(50), nullable=False, default='PENDENTE')  # APROVADA, PENDENTE, REJEITADA
    observacoes = Column(Text, nullable=True)
    is_ajuste = Column(Boolean, default=False)  # True se for ajuste
    metadata_json = Column("metadata", PGJSON, nullable=True)  # JSON com dados técnicos auxiliares
    raw_payload = Column(PGJSON, nullable=True)  # JSON original do Smartsheet
    source_created_at = Column(DateTime, nullable=True)
    source_modified_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # Relacionamentos
    colaborador = relationship("Colaborador", back_populates="solicitacoes")

    def to_dict(self):
        return {
            'id': self.id,
            'colaborador_email': self.colaborador_email,
            'gestor_solicitante_email': self.gestor_solicitante_email,
            'solicitacao': self.solicitacao,
            'saldo_tipo': self.saldo_tipo,
            'data_inicio': self.data_inicio.isoformat(),
            'data_fim': self.data_fim.isoformat(),
            'dias': self.dias,
            'status': self.status,
            'observacoes': self.observacoes,
            'is_ajuste': self.is_ajuste,
            'metadata': self.metadata_json,
        }


class AdminConfig(Base):
    """Configurações e exceções do Painel Admin."""
    __tablename__ = 'admin_configs'
    __table_args__ = {'schema': _MODEL_SCHEMA}

    id = Column(Integer, primary_key=True)
    rule_type = Column(String(100), nullable=False)  # tipo de regra
    target_type = Column(String(50), nullable=False)  # USER, GROUP, GESTOR, ALL
    target_value = Column(String(255), nullable=True)  # email/grupo específico
    enabled = Column(Boolean, default=True)
    valid_from = Column(DateTime, nullable=True)
    valid_until = Column(DateTime, nullable=True)
    reason = Column(String(500), nullable=True)
    config_data = Column(PGJSON, nullable=True)  # JSON com detalhes
    created_by = Column(String(255), nullable=False)
    revoked_by = Column(String(255), nullable=True)
    revoked_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class Auditoria(Base):
    """Registro de auditoria de ações administrativas."""
    __tablename__ = 'auditoria'
    __table_args__ = {'schema': _MODEL_SCHEMA}

    id = Column(Integer, primary_key=True)
    actor_email = Column(String(255), nullable=False)
    action = Column(String(100), nullable=False)
    entity_type = Column(String(50), nullable=False)  # colaborador, config, solicitação
    entity_id = Column(Integer, nullable=False)
    before_data = Column(PGJSON, nullable=True)
    after_data = Column(PGJSON, nullable=True)
    context = Column(PGJSON, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class SyncState(Base):
    """Controle de estado das sincronizações."""
    __tablename__ = 'sync_state'
    __table_args__ = {'schema': _MODEL_SCHEMA}

    sync_name = Column(String(100), primary_key=True)  # cadastro, solicitacoes, saldos
    last_started_at = Column(DateTime, nullable=True)
    last_finished_at = Column(DateTime, nullable=True)
    last_success_at = Column(DateTime, nullable=True)
    last_status = Column(String(50), nullable=True)  # SUCCESS, FAILED, RUNNING
    last_error = Column(Text, nullable=True)
    extra = Column(PGJSON, nullable=True)

    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
