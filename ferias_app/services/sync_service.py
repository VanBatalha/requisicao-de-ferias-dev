# ferias_app/services/sync_service.py
"""Serviço de sincronização de cadastro via Smartsheet."""
from __future__ import annotations

import os
import re
import smartsheet
from datetime import datetime
from sqlalchemy import text
from ..logging_config import get_logger

log = get_logger(__name__)


def clean_val(val):
    """Limpa valores nulos ou vazios vindos do Smartsheet."""
    if val is None or str(val).strip().lower() in ['nan', 'nat', '', 'none']:
        return None
    return str(val).strip()


def extract_id_from_matricula(matricula):
    """Extrai a parte numérica da matrícula para usar como ID no banco.
    
    Ex: 'MAT00027' -> 27, 'MAT00133' -> 133
    """
    if not matricula:
        return None
    match = re.search(r'\d+', matricula)
    if match:
        return int(match.group())
    return None


def sincronizar_cadastro_smartsheet():
    """Busca a planilha no Smartsheet e sincroniza com o PostgreSQL.
    
    Regras:
    - Se a matrícula já existe no banco: PRESERVA os dados editados no app
    - Se a matrícula NÃO existe: INSERE novo registro
    - Se o email já existe (como EXT_): ATUALIZA com a matrícula real
    
    Returns:
        str: Mensagem de resultado da sincronização
    """
    token = os.getenv('SMARTSHEET_SERVICE_TOKEN')
    sheet_id = os.getenv('ID_FOLHA_COLABORADORES')
    
    if not token or not sheet_id:
        return "Erro: Variáveis SMARTSHEET_SERVICE_TOKEN ou ID_FOLHA_COLABORADORES não configuradas."
    
    try:
        log.info("🔄 Iniciando conexão com o Smartsheet...")
        ss = smartsheet.Smartsheet(token)
        ss.errors_as_exceptions(True)
        sheet = ss.Sheets.get_sheet(sheet_id)
        
        # Mapeia o ID da coluna para o Nome da Coluna (cabeçalho)
        col_map = {col.id: col.title for col in sheet.columns}
        
        inseridos = 0
        atualizados = 0
        preservados = 0
        
        # Importa o banco de dados
        from .postgres_service import get_session
        db = get_session()
        
        try:
            for row in sheet.rows:
                # Converte as células da linha em um dicionário {Nome_Coluna: Valor}
                row_data = {}
                for cell in row.cells:
                    col_name = col_map.get(cell.column_id, '').upper()
                    row_data[col_name] = cell.value
                
                matricula = clean_val(row_data.get('MATRÍCULA'))
                if not matricula:
                    continue
                    
                id_num = extract_id_from_matricula(matricula)
                if not id_num:
                    continue
                    
                # Verifica se a matrícula JÁ EXISTE no banco
                result = db.execute(
                    text("SELECT id FROM app_ferias.colaboradores WHERE matricula = :m"),
                    {"m": matricula}
                )
                existing = result.fetchone()
                
                if existing:
                    # MATRÍCULA JÁ EXISTE: Preserva dados editados no app
                    # Apenas atualiza campos que vieram do Smartsheet se estiverem vazios no banco
                    preservados += 1
                    log.debug(f"  ⏭️ Preservado: {matricula}")
                    continue
                
                # Matrícula NÃO existe, vamos inserir ou atualizar
                email = clean_val(row_data.get('E-MAIL EMPRESA'))
                nome = clean_val(row_data.get('NOME COMPLETO')) or clean_val(row_data.get('NOME SE ATIVO'))
                if not nome:
                    nome = f"COLABORADOR SEM NOME ({matricula})"
                    
                # Pega apenas a primeira linha do cargo/setor (ignora histórico)
                cargo_raw = clean_val(row_data.get('CARGO'))
                cargo = cargo_raw.split('\n')[0].strip() if cargo_raw else None
                
                setor_raw = clean_val(row_data.get('SETOR'))
                setor = setor_raw.split('\n')[0].strip() if setor_raw else None
                
                status = clean_val(row_data.get('STATUS')) or 'Ativo'
                
                # Tratamento de Data de Admissão
                data_adm_raw = row_data.get('DATA DE ADMISSÃO')
                data_admissao = None
                if data_adm_raw:
                    if isinstance(data_adm_raw, datetime):
                        data_admissao = data_adm_raw.date()
                    else:
                        try:
                            data_admissao = datetime.strptime(str(data_adm_raw).split(' ')[0], '%d/%m/%Y').date()
                        except:
                            data_admissao = None
                
                # Verifica se o email já existe (pode ser um registro EXT_ criado pelo app)
                if email:
                    result_email = db.execute(
                        text("SELECT id FROM app_ferias.colaboradores WHERE email = :e"),
                        {"e": email}
                    )
                    existing_email = result_email.fetchone()
                    
                    if existing_email:
                        # Email já existe, atualiza com a matrícula real
                        db.execute(text("""
                            UPDATE app_ferias.colaboradores 
                            SET id = :id, matricula = :m, nome_completo = :nome, 
                                cargo = :cargo, setor = :setor, status = :status, 
                                data_admissao = :data, updated_at = CURRENT_TIMESTAMP
                            WHERE id = :uid
                        """), {
                            "id": id_num, "m": matricula, "nome": nome, "cargo": cargo,
                            "setor": setor, "status": status, "data": data_admissao,
                            "uid": existing_email[0]
                        })
                        atualizados += 1
                        log.debug(f"  🔄 Atualizado (email já existia): {matricula} - {nome}")
                        continue
                
                # INSERT novo colaborador
                db.execute(text("""
                    INSERT INTO app_ferias.colaboradores 
                    (id, matricula, nome_completo, email, cargo, setor, status, data_admissao)
                    VALUES (:id, :m, :nome, :email, :cargo, :setor, :status, :data)
                """), {
                    "id": id_num, "m": matricula, "nome": nome, "email": email,
                    "cargo": cargo, "setor": setor, "status": status, "data": data_admissao
                })
                inseridos += 1
                log.debug(f"  ➕ Inserido: {matricula} - {nome}")
            
            db.commit()
            
            msg = f"Sincronização concluída! {inseridos} novos, {atualizados} atualizados, {preservados} preservados."
            log.info(msg)
            return msg
            
        except Exception as e:
            db.rollback()
            log.error(f"Erro na sincronização: {str(e)}")
            return f"Erro na sincronização: {str(e)}"
        finally:
            db.close()
            
    except Exception as e:
        log.error(f"Erro ao conectar com Smartsheet: {str(e)}")
        return f"Erro ao conectar com Smartsheet: {str(e)}"