"""Serviço para gerar relatórios de lançamento de férias."""
from __future__ import annotations

from datetime import datetime, date
from typing import List, Dict, Any, Optional

from ..logging_config import get_logger
from .solicitacao_query_service import listar_solicitacoes_equipes

log = get_logger(__name__)


def _gerar_relatorio_postgres(
    identificadores: List[str],
    mes: Optional[int],
    ano: Optional[int],
) -> Optional[Dict[str, Any]]:
    """Gera o relatório com uma única consulta leve no PostgreSQL.

    Retorna ``None`` quando PostgreSQL não está habilitado, permitindo o fallback
    legado. A consulta seleciona apenas as colunas usadas pela tela e aplica o
    período diretamente no banco, evitando carregar objetos ORM completos.
    """
    try:
        from calendar import monthrange
        from sqlalchemy import func
        from ..models import Colaborador, Solicitacao
        from .postgres_compat_service import postgres_enabled
        from .postgres_service import get_db_session

        if not postgres_enabled():
            return None

        matriculas = sorted({
            str(v or "").strip().upper()
            for v in (identificadores or [])
            if str(v or "").strip()
        })
        if not matriculas:
            return {
                "ok": True, "total": 0, "por_colaborador": {},
                "resumo_status": {"pendente": 0, "em_analise": 0, "aprovada": 0, "negada": 0, "reservada": 0},
                "total_dias": 0, "colaboradores_escopo": [],
                "mes": mes, "ano": ano,
            }

        db = get_db_session()
        dias_expr = func.coalesce(Solicitacao.dias_solicitados, Solicitacao.dias, 0)
        query = (
            db.query(
                Solicitacao.id,
                Solicitacao.colaborador_matricula,
                Colaborador.nome_completo,
                Solicitacao.data_inicio,
                Solicitacao.data_fim,
                dias_expr.label("dias"),
                Solicitacao.status,
                func.coalesce(Solicitacao.solicitacao, Solicitacao.tipo_solicitacao, "").label("solicitacao"),
                func.coalesce(Solicitacao.saldo_tipo, Solicitacao.tipo_ferias, "REGULAR").label("saldo_tipo"),
                func.coalesce(Solicitacao.observacoes, "").label("observacoes"),
            )
            .outerjoin(Colaborador, func.upper(Colaborador.matricula) == func.upper(Solicitacao.colaborador_matricula))
            .filter(
                func.upper(Solicitacao.colaborador_matricula).in_(matriculas),
                Solicitacao.is_ajuste.is_(False),
            )
        )

        if ano and mes:
            inicio = date(int(ano), int(mes), 1)
            fim = date(int(ano), int(mes), monthrange(int(ano), int(mes)))
            query = query.filter(Solicitacao.data_inicio.between(inicio, fim))
        elif ano:
            query = query.filter(Solicitacao.data_inicio.between(date(int(ano), 1, 1), date(int(ano), 12, 31)))
        elif mes:
            query = query.filter(func.extract("month", Solicitacao.data_inicio) == int(mes))

        rows = query.order_by(Solicitacao.data_inicio.desc()).all()

        por_colaborador: Dict[str, Any] = {}
        resumo_status = {"pendente": 0, "em_analise": 0, "aprovada": 0, "negada": 0, "reservada": 0}
        total_dias = 0.0

        for row in rows:
            status_norm = str(row.status or "").strip().upper()
            if "APROV" in status_norm:
                status_key = "aprovada"
            elif any(x in status_norm for x in ("REPROV", "REJEIT", "NEGAD")):
                status_key = "negada"
            elif "ANALISE" in status_norm or "ANÁLISE" in status_norm:
                status_key = "em_analise"
            elif "RESERV" in status_norm:
                status_key = "reservada"
            else:
                status_key = "pendente"
            resumo_status[status_key] += 1

            matricula = str(row.colaborador_matricula or "SEM MATRÍCULA").strip().upper()
            nome = str(row.nome_completo or "").strip()
            chave = f"{nome} ({matricula})" if nome else matricula
            info = por_colaborador.setdefault(chave, {"solicitacoes": [], "total_dias": 0.0, "total_aprovadas": 0.0})
            dias = float(row.dias or 0)
            info["solicitacoes"].append({
                "inicio": row.data_inicio.strftime("%d/%m/%Y") if row.data_inicio else "",
                "fim": row.data_fim.strftime("%d/%m/%Y") if row.data_fim else "",
                "dias": dias, "status": status_norm,
                "solicitacao": row.solicitacao or "",
                "saldo_tipo": row.saldo_tipo or "REGULAR",
                "observacoes": row.observacoes or "",
            })
            info["total_dias"] += dias
            if status_key == "aprovada":
                info["total_aprovadas"] += dias
            total_dias += dias

        return {
            "ok": True, "total": len(rows), "por_colaborador": por_colaborador,
            "resumo_status": resumo_status, "total_dias": round(total_dias, 2),
            "mes": mes, "ano": ano, "colaboradores_escopo": matriculas,
        }
    except Exception as exc:
        log.exception("Falha na consulta otimizada do relatório: %s", exc)
        raise


def gerar_relatorio_lancamento(
    gestor_email: str,
    subordinados: List[str],
    mes: Optional[int] = None,
    ano: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Gera relatório de lançamentos de férias para um gestor.
    
    Args:
        gestor_email: Email do gestor
        subordinados: Lista de matrículas de subordinados
        mes: Mês para filtrar (1-12), ou None para todos
        ano: Ano para filtrar, ou None para todos
    
    Returns:
        Dicionário com dados do relatório
    """
    try:
        # Busca todas as solicitações dos colaboradores dentro do escopo do gestor.
        # Importante: o relatório deve mostrar os colaboradores que o gestor pode
        # solicitar/acompanhar, não apenas o e-mail do gestor logado.
        identificadores = []
        vistos = set()
        for valor in (subordinados or []):
            ident = str(valor or "").strip().upper()
            if ident and ident not in vistos:
                vistos.add(ident)
                identificadores.append(ident)

        # Compatibilidade: se for chamada sem lista de subordinados, mantém fallback
        # para o próprio gestor em vez de quebrar ou retornar erro.
        if not identificadores and gestor_email:
            identificadores.append(str(gestor_email or "").strip())

        relatorio_pg = _gerar_relatorio_postgres(identificadores, mes, ano)
        if relatorio_pg is not None:
            return relatorio_pg

        solicitacoes = listar_solicitacoes_equipes(identificadores)
        
        if not solicitacoes:
            return {
                "ok": True,
                "total": 0,
                "por_colaborador": {},
                "resumo_status": {
                    "pendente": 0,
                    "em_analise": 0,
                    "aprovada": 0,
                    "negada": 0,
                    "reservada": 0,
                },
                "total_dias": 0,
                "colaboradores_escopo": identificadores,
            }
        
        # Filtra por mês e ano se fornecidos
        solicitacoes_filtradas = []
        for sol in solicitacoes:
            try:
                # sol é uma tupla: (row_id, colab_email, inicio, fim, dias, status, solicitacao, saldo_tipo, obs)
                if len(sol) < 3:
                    continue
                
                inicio_str = sol[2]  # data de início
                if not inicio_str:
                    continue
                
                # Parse da data
                try:
                    if isinstance(inicio_str, str):
                        # Tenta formato DD/MM/YYYY
                        partes = inicio_str.split('/')
                        if len(partes) == 3:
                            sol_date = date(int(partes[2]), int(partes[1]), int(partes[0]))
                        else:
                            continue
                    else:
                        sol_date = inicio_str
                except Exception:
                    continue
                
                # Filtra por mês/ano se fornecido
                if mes and sol_date.month != mes:
                    continue
                if ano and sol_date.year != ano:
                    continue
                
                solicitacoes_filtradas.append(sol)
            except Exception as e:
                log.warning(f"Erro ao processar solicitação: {e}")
                continue
        
        # Agrupa por colaborador e status
        por_colaborador: Dict[str, Any] = {}
        resumo_status = {
            "pendente": 0,
            "em_analise": 0,
            "aprovada": 0,
            "negada": 0,
            "reservada": 0,
        }
        total_dias = 0.0
        
        for sol in solicitacoes_filtradas:
            try:
                row_id, colab_email, inicio, fim, dias, status, solicitacao, saldo_tipo, obs = sol[:9]
                
                # Normaliza status
                status_norm = (status or "").strip().upper()
                if "APROV" in status_norm:
                    status_key = "aprovada"
                elif "REPROV" in status_norm or "REJEIT" in status_norm or "NEGAD" in status_norm:
                    status_key = "negada"
                elif "ANALISE" in status_norm:
                    status_key = "em_analise"
                elif "RESERV" in status_norm:
                    status_key = "reservada"
                else:
                    status_key = "pendente"
                
                resumo_status[status_key] = resumo_status.get(status_key, 0) + 1
                
                # Agrupa por colaborador
                if colab_email not in por_colaborador:
                    por_colaborador[colab_email] = {
                        "solicitacoes": [],
                        "total_dias": 0.0,
                        "total_aprovadas": 0.0,
                    }
                
                dias_float = float(dias or 0)
                por_colaborador[colab_email]["solicitacoes"].append({
                    "inicio": inicio,
                    "fim": fim,
                    "dias": dias_float,
                    "status": status_norm,
                    "solicitacao": solicitacao,
                    "saldo_tipo": saldo_tipo,
                    "observacoes": obs or "",
                })
                
                por_colaborador[colab_email]["total_dias"] += dias_float
                if status_key == "aprovada":
                    por_colaborador[colab_email]["total_aprovadas"] += dias_float
                
                total_dias += dias_float
            except Exception as e:
                log.warning(f"Erro ao agrupar solicitação: {e}")
                continue
        
        return {
            "ok": True,
            "total": len(solicitacoes_filtradas),
            "por_colaborador": por_colaborador,
            "resumo_status": resumo_status,
            "total_dias": round(total_dias, 2),
            "mes": mes,
            "ano": ano,
            "colaboradores_escopo": identificadores,
        }
    
    except Exception as e:
        log.error(f"Erro ao gerar relatório de lançamento: {e}")
        return {
            "ok": False,
            "message": f"Erro ao gerar relatório: {str(e)}",
        }
