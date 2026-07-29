"""Modelos SQLAlchemy para o novo banco PostgreSQL do App de Férias.

A estrutura canônica usa a matrícula como identificador externo do colaborador.
A tabela ``saldo_periodo`` é a única fonte de períodos e saldos. As tabelas
legadas ``periodos_aquisitivos`` e ``saldos_periodo`` foram removidas na V58.
Todas as solicitações usam matrícula como identificador operacional.
"""
from __future__ import annotations

from datetime import datetime, date
import os
import re

from sqlalchemy import (
    Boolean, Column, Date, DateTime, ForeignKey, Integer, Numeric, String,
    Text, UniqueConstraint
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
from sqlalchemy.dialects.postgresql import JSONB

Base = declarative_base()


def _model_schema_name() -> str:
    schema = (os.getenv("DB_SCHEMA") or "app_ferias").strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
        schema = "app_ferias"
    return schema


_MODEL_SCHEMA = _model_schema_name()


class Colaborador(Base):
    """Cadastro principal do colaborador.

    id continua inteiro para compatibilidade com a base já criada, mas a matrícula
    é o identificador de negócio e deve ser usado em novas relações/registros.
    """
    __tablename__ = "colaboradores"
    __table_args__ = {"schema": _MODEL_SCHEMA}

    id = Column(Integer, primary_key=True, autoincrement=False)
    matricula = Column(String(50), nullable=False, unique=True, index=True)
    email = Column(String(255), nullable=True, index=True)
    nome_completo = Column(String(255), nullable=False)
    cargo = Column(Text, nullable=True)
    setor = Column(Text, nullable=True)
    unidade = Column(String(100), nullable=True)
    empresa = Column(String(100), nullable=True)
    telefone = Column(String(50), nullable=True)
    regime = Column(String(50), nullable=True)
    data_admissao = Column(Date, nullable=True)
    status = Column(String(50), nullable=True, default="ATIVO")

    # Campos de compatibilidade com versões anteriores do app.
    dias_direito = Column(Integer, nullable=False, default=0, server_default="0")
    origem_sheet_id = Column(String(50), nullable=True)
    origem_row_id = Column(String(50), nullable=True)
    raw_payload = Column(JSONB, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    complemento = relationship(
        "ColaboradorComplemento",
        back_populates="colaborador",
        uselist=False,
        cascade="all, delete-orphan",
        foreign_keys="ColaboradorComplemento.colaborador_id",
    )
    solicitacoes = relationship("Solicitacao", back_populates="colaborador", foreign_keys="Solicitacao.colaborador_id")

    def to_dict(self):
        return {
            "id": self.id,
            "matricula": self.matricula,
            "email": self.email,
            "nome_completo": self.nome_completo,
            "status": self.status,
            "data_admissao": self.data_admissao.isoformat() if self.data_admissao else None,
            "setor": self.setor,
            "cargo": self.cargo,
            "regime": self.regime,
            "dias_direito": self.dias_direito or 0,
        }


class PermissaoUsuario(Base):
    __tablename__ = "permissoes_usuario"
    __table_args__ = (
        UniqueConstraint("colaborador_matricula", "role", name="uq_permissoes_usuario_matricula_role"),
        {"schema": _MODEL_SCHEMA},
    )

    colaborador_id = Column(Integer, ForeignKey(f"{_MODEL_SCHEMA}.colaboradores.id"), nullable=True, primary_key=True)
    colaborador_matricula = Column(String(50), ForeignKey(f"{_MODEL_SCHEMA}.colaboradores.matricula"), nullable=True, index=True)
    role = Column(String(20), nullable=False, primary_key=True)  # USER, DP, ADMIN, ADMINISTRADOR


class HierarquiaGestao(Base):
    __tablename__ = "hierarquia_gestao"
    __table_args__ = {"schema": _MODEL_SCHEMA}

    id = Column(Integer, primary_key=True)
    colaborador_id = Column(Integer, ForeignKey(f"{_MODEL_SCHEMA}.colaboradores.id"), nullable=True, unique=True)
    colaborador_matricula = Column(String(50), ForeignKey(f"{_MODEL_SCHEMA}.colaboradores.matricula"), nullable=True, unique=True, index=True)
    gestor_direto_id = Column(Integer, ForeignKey(f"{_MODEL_SCHEMA}.colaboradores.id"), nullable=True)
    # A matricula e a chave operacional. Nao usamos FK aqui porque
    # gestor_superior_matricula tambem pode guardar os marcadores DP/GESTOR.
    gestor_direto_matricula = Column(String(50), nullable=True, index=True)
    gestor_direto_email = Column(String(255), nullable=True)
    gestor_superior_id = Column(Integer, ForeignKey(f"{_MODEL_SCHEMA}.colaboradores.id"), nullable=True)
    # Pode ser uma matricula real (ex.: MAT00801) ou marcador operacional DP/GESTOR.
    gestor_superior_matricula = Column(String(50), nullable=True, index=True)
    gestor_superior_email = Column(String(255), nullable=True)


class SaldoPeriodoNovo(Base):
    """Fonte oficial de saldo por período aquisitivo.

    Esta tabela é intencionalmente independente de auditoria_saldos.
    solicitacoes_ferias guarda os eventos; saldo_periodo guarda o saldo vivo
    por matrícula/período/tipo.
    """
    __tablename__ = "saldo_periodo"
    __table_args__ = (
        UniqueConstraint("colaborador_matricula", "periodo_numero", "tipo_saldo", name="uq_saldo_periodo_matricula_periodo_tipo"),
        {"schema": _MODEL_SCHEMA},
    )

    id = Column(Integer, primary_key=True)
    colaborador_id = Column(Integer, ForeignKey(f"{_MODEL_SCHEMA}.colaboradores.id"), nullable=False, index=True)
    colaborador_matricula = Column(String(50), ForeignKey(f"{_MODEL_SCHEMA}.colaboradores.matricula"), nullable=False, index=True)
    periodo_numero = Column(Integer, nullable=False)
    data_inicio = Column(Date, nullable=False)
    data_fim = Column(Date, nullable=False)
    is_atual = Column(Boolean, default=False)
    tipo_saldo = Column(String(20), nullable=False, default="REGULAR")
    saldo_inicial = Column(Numeric(6, 2), default=0)
    saldo_utilizado = Column(Numeric(6, 2), default=0)
    saldo_reservado = Column(Numeric(6, 2), default=0)
    saldo_disponivel = Column(Numeric(6, 2), default=0)
    ultima_alteracao = Column(DateTime, default=datetime.utcnow, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    @property
    def disponivel(self) -> float:
        return float(self.saldo_disponivel or 0)


class Solicitacao(Base):
    """Solicitações no novo banco.

    A tabela canônica é solicitacoes_ferias. Mantemos colunas compatíveis com o
    app antigo para reduzir risco na migração. Novos registros preenchem também
    colaborador_matricula e solicitante_matricula.
    """
    __tablename__ = "solicitacoes_ferias"
    __table_args__ = {"schema": _MODEL_SCHEMA}

    id = Column(Integer, primary_key=True)
    colaborador_id = Column(Integer, ForeignKey(f"{_MODEL_SCHEMA}.colaboradores.id"), nullable=True)
    colaborador_matricula = Column(String(50), ForeignKey(f"{_MODEL_SCHEMA}.colaboradores.matricula"), nullable=True, index=True)
    solicitante_id = Column(Integer, ForeignKey(f"{_MODEL_SCHEMA}.colaboradores.id"), nullable=True)
    solicitante_matricula = Column(String(50), ForeignKey(f"{_MODEL_SCHEMA}.colaboradores.matricula"), nullable=True, index=True)

    tipo_solicitacao = Column(String(50), nullable=True)
    tipo_ferias = Column(String(20), nullable=True)
    data_inicio = Column(Date, nullable=False)
    data_fim = Column(Date, nullable=True)
    dias_solicitados = Column(Numeric(6, 2), nullable=False, default=0)
    status = Column(String(50), nullable=False, default="PENDENTE")
    observacoes = Column(Text, nullable=True)

    # Colunas de compatibilidade com serviços antigos.
    origem_sheet_id = Column(String(50), nullable=True)
    smartsheet_row_id = Column(String(50), nullable=True)
    colaborador_email = Column(String(255), nullable=True, index=True)
    gestor_solicitante_email = Column(String(255), nullable=True)
    criado_por = Column(String(255), nullable=True)
    solicitacao = Column(String(255), nullable=True)
    saldo_tipo = Column(String(50), nullable=True, default="REGULAR")
    dias = Column(Integer, nullable=True)
    is_ajuste = Column(Boolean, default=False)
    metadata_json = Column("metadata", JSONB, nullable=True)
    periodo_aquisitivo_origem = Column(Text, nullable=True)
    raw_payload = Column(JSONB, nullable=True)
    source_created_at = Column(DateTime, nullable=True)
    source_modified_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    colaborador = relationship("Colaborador", back_populates="solicitacoes", foreign_keys=[colaborador_id])

    def to_dict(self):
        return {
            "id": self.id,
            "colaborador_id": self.colaborador_id,
            "colaborador_matricula": self.colaborador_matricula,
            "colaborador_email": self.colaborador_email,
            "gestor_solicitante_email": self.gestor_solicitante_email,
            "solicitacao": self.solicitacao or self.tipo_solicitacao,
            "saldo_tipo": self.saldo_tipo or self.tipo_ferias,
            "data_inicio": self.data_inicio.isoformat() if self.data_inicio else None,
            "data_fim": self.data_fim.isoformat() if self.data_fim else None,
            "dias": int(self.dias if self.dias is not None else (self.dias_solicitados or 0)),
            "status": self.status,
            "observacoes": self.observacoes,
            "is_ajuste": self.is_ajuste,
            "metadata": self.metadata_json,
            "periodo_aquisitivo_origem": self.periodo_aquisitivo_origem,
        }


class AuditoriaSaldos(Base):
    __tablename__ = "auditoria_saldos"
    __table_args__ = {"schema": _MODEL_SCHEMA}

    id = Column(Integer, primary_key=True)
    saldo_id = Column(Integer, ForeignKey(f"{_MODEL_SCHEMA}.saldo_periodo.id", ondelete="SET NULL"), nullable=True)
    usuario_alterou_id = Column(Integer, ForeignKey(f"{_MODEL_SCHEMA}.colaboradores.id"), nullable=True)
    usuario_alterou_matricula = Column(String(50), ForeignKey(f"{_MODEL_SCHEMA}.colaboradores.matricula"), nullable=True)
    data_movimento = Column(DateTime, default=datetime.utcnow, nullable=False)
    tipo_movimento = Column(String(50), nullable=False)
    dias_anteriores = Column(Numeric(6, 2), nullable=True)
    dias_alterados = Column(Numeric(6, 2), nullable=True)
    dias_novos = Column(Numeric(6, 2), nullable=True)
    observacoes = Column(Text, nullable=True)


class ColaboradorComplemento(Base):
    """Complemento operacional do cadastro.

    Mantém permissões, hierarquia e flags do colaborador. Os saldos reais e
    consolidados ficam exclusivamente na tabela saldo_periodo.
    """
    __tablename__ = "colaborador_complemento"
    __table_args__ = {"schema": _MODEL_SCHEMA}

    id = Column(Integer, primary_key=True)
    colaborador_id = Column(Integer, ForeignKey(f"{_MODEL_SCHEMA}.colaboradores.id"), nullable=False, unique=True)
    colaborador_matricula = Column(String(50), ForeignKey(f"{_MODEL_SCHEMA}.colaboradores.matricula"), nullable=True, index=True)
    user_type = Column(String(50), nullable=True, default="USER")
    # Campos legados por e-mail continuam para compatibilidade, mas a relação
    # operacional nova é por matrícula/texto especial (DP/GESTOR).
    gestor_direto_email = Column(String(255), nullable=True)
    gestor_superior_email = Column(String(255), nullable=True)
    gestor_direto = Column(String(50), nullable=True)
    gestor_superior = Column(String(50), nullable=True)
    ativo_no_app = Column(Boolean, default=True)
    flags_internas = Column(JSONB, nullable=True)

    calculated_at = Column(DateTime, nullable=True)
    origem_sheet_id = Column(String(50), nullable=True)
    origem_row_id = Column(String(50), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    colaborador = relationship("Colaborador", back_populates="complemento", foreign_keys=[colaborador_id])

    def to_dict(self):
        return {
            "user_type": self.user_type,
            "gestor_direto_email": self.gestor_direto_email,
            "gestor_superior_email": self.gestor_superior_email,
            "gestor_direto": self.gestor_direto,
            "gestor_superior": self.gestor_superior,
            "ativo_no_app": self.ativo_no_app,
        }


class AdminConfig(Base):
    __tablename__ = "admin_configs"
    __table_args__ = {"schema": _MODEL_SCHEMA}

    id = Column(Integer, primary_key=True)
    rule_type = Column(String(100), nullable=False)
    target_type = Column(String(50), nullable=False)
    target_value = Column(String(255), nullable=True)
    enabled = Column(Boolean, default=True)
    valid_from = Column(DateTime, nullable=True)
    valid_until = Column(DateTime, nullable=True)
    reason = Column(String(500), nullable=True)
    config_data = Column(JSONB, nullable=True)
    created_by = Column(String(255), nullable=False)
    revoked_by = Column(String(255), nullable=True)
    revoked_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class Auditoria(Base):
    __tablename__ = "auditoria"
    __table_args__ = {"schema": _MODEL_SCHEMA}

    id = Column(Integer, primary_key=True)
    actor_email = Column(String(255), nullable=False)
    action = Column(String(100), nullable=False)
    entity_type = Column(String(50), nullable=False)
    entity_id = Column(Integer, nullable=False)
    before_data = Column(JSONB, nullable=True)
    after_data = Column(JSONB, nullable=True)
    context = Column(JSONB, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class SyncState(Base):
    __tablename__ = "sync_state"
    __table_args__ = {"schema": _MODEL_SCHEMA}

    sync_name = Column(String(100), primary_key=True)
    last_started_at = Column(DateTime, nullable=True)
    last_finished_at = Column(DateTime, nullable=True)
    last_success_at = Column(DateTime, nullable=True)
    last_status = Column(String(50), nullable=True)
    last_error = Column(Text, nullable=True)
    extra = Column(JSONB, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
