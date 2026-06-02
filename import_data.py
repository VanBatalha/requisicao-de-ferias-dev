#!/usr/bin/env python3
"""
Script para importar dados do arquivo Excel (export_ferias_app.xlsx) 
para o PostgreSQL.

Uso:
    python import_data.py <database_url> <excel_file>

Exemplo:
    python import_data.py "postgresql://user:pass@localhost:5432/ferias_app" export_ferias_app.xlsx
"""

import sys
import os
import json
from datetime import datetime
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Importar modelos
sys.path.insert(0, os.path.dirname(__file__))
from ferias_app.models import (
    Base, Colaborador, ColaboradorComplemento, Solicitacao, 
    AdminConfig, SyncState
)


def import_colaboradores(session, excel_file):
    """Importa dados da aba 'colaboradores'."""
    print("📥 Importando colaboradores...")
    df = pd.read_excel(excel_file, sheet_name="colaboradores")
    
    for _, row in df.iterrows():
        # Pula se o email já existe
        email = str(row.get('email', '')).strip().lower()
        if not email or email == 'nan':
            continue
        
        existing = session.query(Colaborador).filter_by(email=email).first()
        if existing:
            continue
        
        # Parse data de admissão
        data_adm = None
        if pd.notna(row.get('data_admissao')):
            try:
                data_adm = pd.to_datetime(row['data_admissao']).date()
            except:
                pass
        
        colab = Colaborador(
            email=email,
            nome_completo=str(row.get('nome_completo', '')).strip() or None,
            status=str(row.get('status', '')).strip().upper() or None,
            data_admissao=data_adm,
            setor=str(row.get('setor', '')).strip() or None,
            cargo=str(row.get('cargo', '')).strip() or None,
            regime=str(row.get('regime', '')).strip() or None,
            dias_direito=int(row.get('dias_direito', 0)) if pd.notna(row.get('dias_direito')) else None,
            origem_sheet_id=str(row.get('origem_sheet_id', '')).strip() or None,
            origem_row_id=str(row.get('origem_row_id', '')).strip() or None,
            raw_payload=row.to_dict() if pd.notna(row.get('raw_payload')) else None,
        )
        session.add(colab)
    
    session.commit()
    count = session.query(Colaborador).count()
    print(f"✅ {count} colaboradores no banco")


def import_colaborador_complemento(session, excel_file):
    """Importa dados da aba 'colaborador_complemento'."""
    print("📥 Importando dados complementares...")
    df = pd.read_excel(excel_file, sheet_name="colaborador_complemento")
    
    for _, row in df.iterrows():
        colab_id = row.get('colaborador_id')
        
        if pd.isna(colab_id):
            continue
        
        # Verifica se o complemento já existe
        existing = session.query(ColaboradorComplemento).filter_by(
            colaborador_id=int(colab_id)
        ).first()
        if existing:
            continue
        
        # Parse periodo_aquisitivo_atual se for JSON string
        periodo = None
        if pd.notna(row.get('periodo_aquisitivo_atual')):
            try:
                if isinstance(row['periodo_aquisitivo_atual'], str):
                    periodo = json.loads(row['periodo_aquisitivo_atual'])
                else:
                    periodo = row['periodo_aquisitivo_atual']
            except:
                pass
        
        # Parse calculated_at
        calc_at = None
        if pd.notna(row.get('calculated_at')):
            try:
                calc_at = pd.to_datetime(row['calculated_at'])
            except:
                pass
        
        compl = ColaboradorComplemento(
            colaborador_id=int(colab_id),
            user_type=str(row.get('user_type', '')).strip() or None,
            gestor_direto_email=str(row.get('gestor_direto_email', '')).strip().lower() or None,
            gestor_superior_email=str(row.get('gestor_superior_email', '')).strip().lower() or None,
            ativo_no_app=bool(row.get('ativo_no_app', True)) if pd.notna(row.get('ativo_no_app')) else True,
            saldo_regular_direito=int(row.get('saldo_regular_direito', 0)),
            saldo_regular_usado=int(row.get('saldo_regular_usado', 0)),
            saldo_regular_reservado=int(row.get('saldo_regular_reservado', 0)),
            saldo_regular_disponivel=int(row.get('saldo_regular_disponivel', 0)),
            saldo_premium_direito=int(row.get('saldo_premium_direito', 0)),
            saldo_premium_usado=int(row.get('saldo_premium_usado', 0)),
            saldo_premium_reservado=int(row.get('saldo_premium_reservado', 0)),
            saldo_premium_disponivel=int(row.get('saldo_premium_disponivel', 0)),
            total_solicitacoes=int(row.get('total_solicitacoes', 0)),
            periodo_aquisitivo_atual=periodo,
            calculated_at=calc_at,
            origem_sheet_id=str(row.get('origem_sheet_id', '')).strip() or None,
            origem_row_id=str(row.get('origem_row_id', '')).strip() or None,
        )
        session.add(compl)
    
    session.commit()
    count = session.query(ColaboradorComplemento).count()
    print(f"✅ {count} complementos no banco")


def import_solicitacoes(session, excel_file):
    """Importa dados da aba 'solicitacoes'."""
    print("📥 Importando solicitações...")
    df = pd.read_excel(excel_file, sheet_name="solicitacoes")
    
    for _, row in df.iterrows():
        email = str(row.get('colaborador_email', '')).strip().lower()
        if not email or email == 'nan':
            continue
        
        # Busca o colaborador pelo email
        colab = session.query(Colaborador).filter_by(email=email).first()
        
        # Parse datas
        data_inicio = None
        if pd.notna(row.get('data_inicio')):
            try:
                data_inicio = pd.to_datetime(row['data_inicio']).date()
            except:
                pass
        
        data_fim = None
        if pd.notna(row.get('data_fim')):
            try:
                data_fim = pd.to_datetime(row['data_fim']).date()
            except:
                pass
        
        if not data_inicio or not data_fim:
            continue
        
        # Parse dates para source_*
        source_created = None
        if pd.notna(row.get('source_created_at')):
            try:
                source_created = pd.to_datetime(row['source_created_at'])
            except:
                pass
        
        source_modified = None
        if pd.notna(row.get('source_modified_at')):
            try:
                source_modified = pd.to_datetime(row['source_modified_at'])
            except:
                pass
        
        # Parse metadata e raw_payload se forem JSON strings
        metadata = None
        if pd.notna(row.get('metadata')):
            try:
                if isinstance(row['metadata'], str):
                    metadata = json.loads(row['metadata'])
                else:
                    metadata = row['metadata']
            except:
                pass
        
        raw_payload = None
        if pd.notna(row.get('raw_payload')):
            try:
                if isinstance(row['raw_payload'], str):
                    raw_payload = json.loads(row['raw_payload'])
                else:
                    raw_payload = row['raw_payload']
            except:
                pass
        
        sol = Solicitacao(
            origem_sheet_id=str(row.get('origem_sheet_id', '')).strip() or None,
            smartsheet_row_id=str(row.get('smartsheet_row_id', '')).strip() or None,
            colaborador_id=colab.id if colab else None,
            colaborador_email=email,
            gestor_solicitante_email=str(row.get('gestor_solicitante_email', '')).strip().lower() or None,
            criado_por=str(row.get('criado_por', '')).strip().lower() or None,
            solicitacao=str(row.get('solicitacao', '')).strip() or None,
            saldo_tipo=str(row.get('saldo_tipo', 'REGULAR')).strip().upper(),
            data_inicio=data_inicio,
            data_fim=data_fim,
            dias=int(row.get('dias', 0)),
            status=str(row.get('status', 'PENDENTE')).strip().upper(),
            observacoes=str(row.get('observacoes', '')).strip() or None,
            is_ajuste=bool(row.get('is_ajuste', False)) if pd.notna(row.get('is_ajuste')) else False,
            metadata_json=metadata,
            raw_payload=raw_payload,
            source_created_at=source_created,
            source_modified_at=source_modified,
        )
        session.add(sol)
    
    session.commit()
    count = session.query(Solicitacao).count()
    print(f"✅ {count} solicitações no banco")


def import_sync_state(session, excel_file):
    """Importa dados da aba 'sync_state'."""
    print("📥 Importando estado de sincronizações...")
    df = pd.read_excel(excel_file, sheet_name="sync_state")
    
    for _, row in df.iterrows():
        sync_name = str(row.get('sync_name', '')).strip()
        if not sync_name:
            continue
        
        existing = session.query(SyncState).filter_by(sync_name=sync_name).first()
        if existing:
            continue
        
        # Parse datas
        last_started = None
        if pd.notna(row.get('last_started_at')):
            try:
                last_started = pd.to_datetime(row['last_started_at'])
            except:
                pass
        
        last_finished = None
        if pd.notna(row.get('last_finished_at')):
            try:
                last_finished = pd.to_datetime(row['last_finished_at'])
            except:
                pass
        
        last_success = None
        if pd.notna(row.get('last_success_at')):
            try:
                last_success = pd.to_datetime(row['last_success_at'])
            except:
                pass
        
        extra = None
        if pd.notna(row.get('extra')):
            try:
                if isinstance(row['extra'], str):
                    extra = json.loads(row['extra'])
                else:
                    extra = row['extra']
            except:
                pass
        
        sync = SyncState(
            sync_name=sync_name,
            last_started_at=last_started,
            last_finished_at=last_finished,
            last_success_at=last_success,
            last_status=str(row.get('last_status', '')).strip() or None,
            last_error=str(row.get('last_error', '')).strip() or None,
            extra=extra,
        )
        session.add(sync)
    
    session.commit()
    count = session.query(SyncState).count()
    print(f"✅ {count} sync states no banco")


def main():
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
    
    # Criar engine e sessionmaker
    engine = create_engine(database_url, echo=False)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    
    try:
        import_colaboradores(session, excel_file)
        import_colaborador_complemento(session, excel_file)
        import_solicitacoes(session, excel_file)
        import_sync_state(session, excel_file)
        
        print("\n✅ Importação concluída com sucesso!")
        
        # Resumo
        colabs = session.query(Colaborador).count()
        sols = session.query(Solicitacao).count()
        print(f"   📊 Total: {colabs} colaboradores, {sols} solicitações")
        
    except Exception as e:
        print(f"\n❌ Erro durante importação: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        session.close()


if __name__ == "__main__":
    main()
