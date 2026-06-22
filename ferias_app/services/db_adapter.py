"""
Adapter que faz bridge entre o código legado do Smartsheet e o PostgreSQL.
Redirecioná as chamadas principais para usar dados do banco.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from datetime import datetime, date

from ..logging_config import get_logger
from .postgres_service import (
    listar_colaboradores,
    get_colaborador,
    listar_solicitacoes,
    get_db_session,
)

log = get_logger(__name__)


def listar_colaboradores_bridge() -> List[Dict[str, Any]]:
    """Bridge para listar colaboradores do PostgreSQL.
    
    Retorna no formato esperado pelos blueprints (compatível com Smartsheet).
    """
    try:
        colabs = listar_colaboradores()
        
        # Converte para formato compatible com o código legado
        result = []
        for colab in colabs:
            item = {
                'EMAIL DA EMPRESA': colab.get('email', ''),
                'NOME COMPLETO': colab.get('nome_completo', ''),
                'STATUS': colab.get('status', 'ATIVO'),
                'SETOR': colab.get('setor', ''),
                'CARGO': colab.get('cargo', ''),
                'email': colab.get('email', ''),
                'nome': colab.get('nome_completo', ''),
                'status': colab.get('status', ''),
                'setor': colab.get('setor', ''),
                'cargo': colab.get('cargo', ''),
                'regime': colab.get('regime', ''),
                'dias_direito': colab.get('dias_direito', 0),
            }
            result.append(item)
        
        return result
    except Exception as e:
        log.error(f"Erro ao listar colaboradores via bridge: {e}")
        return []


def get_resumo_ferias_bridge(email: str) -> Dict[str, Any]:
    """Bridge para obter saldos do colaborador.
    
    Retorna no formato esperado pelos blueprints.
    """
    try:
        email = str(email).strip().lower()
        colab_data = get_colaborador(email)
        
        if not colab_data:
            return {
                'regular': {
                    'direito': 0,
                    'usado': 0,
                    'reservado': 0,
                    'disponivel': 0,
                },
                'premium': {
                    'direito': 0,
                    'usado': 0,
                    'reservado': 0,
                    'disponivel': 0,
                },
            }
        
        # Extrai saldos
        regular = {
            'direito': colab_data.get('saldo_regular_direito', 0),
            'usado': colab_data.get('saldo_regular_usado', 0),
            'reservado': colab_data.get('saldo_regular_reservado', 0),
            'disponivel': colab_data.get('saldo_regular_disponivel', 0),
        }
        premium = {
            'direito': colab_data.get('saldo_premium_direito', 0),
            'usado': colab_data.get('saldo_premium_usado', 0),
            'reservado': colab_data.get('saldo_premium_reservado', 0),
            'disponivel': colab_data.get('saldo_premium_disponivel', 0),
        }
        
        return {
            'regular': regular,
            'premium': premium,
        }
    except Exception as e:
        log.error(f"Erro ao obter saldos via bridge: {e}")
        return {}


def listar_gestores_bridge() -> List[str]:
    """Bridge para listar emails dos gestores."""
    try:
        session = get_db_session()
        from ..models import ColaboradorComplemento
        
        gestores = session.query(ColaboradorComplemento.gestor_direto_email).filter(
            ColaboradorComplemento.gestor_direto_email.isnot(None)
        ).distinct().all()
        
        return [g[0] for g in gestores if g[0]]
    except Exception as e:
        log.error(f"Erro ao listar gestores: {e}")
        return []


def listar_emails_colaboradores_bridge() -> List[str]:
    """Bridge para listar todos os emails de colaboradores."""
    try:
        colabs = listar_colaboradores()
        return [c.get('email', '').lower() for c in colabs if c.get('email')]
    except Exception as e:
        log.error(f"Erro ao listar emails: {e}")
        return []


def get_subordinados_bridge(gestor_email: str) -> List[Dict[str, Any]]:
    """Bridge para listar subordinados de um gestor.
    
    Busca colaboradores onde gestor_direto_email = gestor_email.
    """
    try:
        gestor_email = str(gestor_email).strip().lower()
        
        session = get_db_session()
        from ..models import Colaborador, ColaboradorComplemento
        
        subordinados = session.query(Colaborador).join(
            ColaboradorComplemento
        ).filter(
            ColaboradorComplemento.gestor_direto_email == gestor_email
        ).all()
        
        result = []
        for sub in subordinados:
            result.append({
                'EMAIL DA EMPRESA': sub.email,
                'NOME COMPLETO': sub.nome_completo,
                'email': sub.email,
                'nome': sub.nome_completo,
                'status': sub.status,
                'gestor_email': gestor_email,
            })
        
        return result
    except Exception as e:
        log.error(f"Erro ao listar subordinados: {e}")
        return []


def is_colaborador_ativo_bridge(colaborador: Dict[str, Any]) -> bool:
    """Verifica se um colaborador está ativo."""
    try:
        status = (colaborador.get('STATUS') or colaborador.get('status') or 'ATIVO').upper()
        return status == 'ATIVO'
    except Exception:
        return True


def get_colaborador_row_bridge(email: str) -> Dict[str, Any]:
    """Bridge para obter dados completos de um colaborador.
    
    Simula a estrutura de uma linha do Smartsheet.
    """
    try:
        email = str(email).strip().lower()
        colab = get_colaborador(email)
        
        if not colab:
            return {}
        
        # Converte para formato compatible
        return {
            'EMAIL DA EMPRESA': colab.get('email', ''),
            'NOME COMPLETO': colab.get('nome_completo', ''),
            'STATUS': colab.get('status', 'ATIVO'),
            'SETOR': colab.get('setor', ''),
            'CARGO': colab.get('cargo', ''),
            'DATA DE ADMISSAO': colab.get('data_admissao'),
            'REGIME DE CONTRATACAO': colab.get('regime', ''),
            'DIAS DE DIREITO': colab.get('dias_direito', 0),
            'GESTOR DIRETO': colab.get('gestor_direto_email', ''),
            'USER TYPE': colab.get('user_type', 'USER'),
            # ... adicionar mais campos conforme necessário
        }
    except Exception as e:
        log.error(f"Erro ao obter colaborador: {e}")
        return {}


def atualizar_relacao_gestor_bridge(email_colaborador: str, email_gestor: str) -> bool:
    """Bridge para atualizar a relação gestor-subordinado."""
    try:
        email_colab = str(email_colaborador).strip().lower()
        email_gest = str(email_gestor).strip().lower()
        
        session = get_db_session()
        from ..models import Colaborador, ColaboradorComplemento
        
        colab = session.query(Colaborador).filter_by(email=email_colab).first()
        if not colab:
            return False
        
        if not colab.complemento:
            colab.complemento = ColaboradorComplemento(colaborador_id=colab.id)
        
        colab.complemento.gestor_direto_email = email_gest
        session.commit()
        
        return True
    except Exception as e:
        log.error(f"Erro ao atualizar gestor: {e}")
        return False


def get_saldos_por_periodo_bridge(email: str, periodo_inicio: date, periodo_fim: date) -> Dict[str, Any]:
    """Bridge para obter saldos de um período específico."""
    try:
        email = str(email).strip().lower()
        
        # Busca solicitações aprovadas no período
        session = get_db_session()
        from ..models import Solicitacao
        
        solicitacoes = session.query(Solicitacao).filter(
            Solicitacao.colaborador_email == email,
            Solicitacao.status == 'APROVADA',
            Solicitacao.data_inicio >= periodo_inicio,
            Solicitacao.data_fim <= periodo_fim,
        ).all()
        
        dias_regular = 0
        dias_premium = 0
        
        for sol in solicitacoes:
            if sol.saldo_tipo == 'REGULAR':
                dias_regular += sol.dias
            elif sol.saldo_tipo == 'PREMIUM':
                dias_premium += sol.dias
        
        return {
            'regular': dias_regular,
            'premium': dias_premium,
            'periodo_inicio': periodo_inicio.isoformat(),
            'periodo_fim': periodo_fim.isoformat(),
        }
    except Exception as e:
        log.error(f"Erro ao obter saldos por período: {e}")
        return {'regular': 0, 'premium': 0}
