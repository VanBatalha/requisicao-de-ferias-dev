from __future__ import annotations

from flask import jsonify, request, session

from .base import bp
from ..core import (
    get_subordinados,
    get_user_role,
    is_colaborador_ativo,
    is_gestor,
    listar_colaboradores_cached,
    safe_lower,
    tem_grupo,
)
from ..services.solicitacoes_service import processar_solicitacao
from ..services.relatorio_service import gerar_relatorio_lancamento
from ..services.simulation_service import get_simulated_gestor


@bp.route("/api/solicitar-ferias", methods=["POST"])
def api_solicitar_ferias():
    payload, status = processar_solicitacao(request.form.to_dict(flat=True), session.get("user"))
    return jsonify(payload), status


def _matriculas_colaboradores_ativos() -> list[str]:
    """Retorna matrículas ativas, chave operacional do relatório."""
    out: list[str] = []
    seen: set[str] = set()
    for colab in listar_colaboradores_cached() or []:
        if not isinstance(colab, dict):
            continue
        try:
            if not is_colaborador_ativo(colab):
                continue
        except Exception:
            pass
        matricula = str(colab.get("MATRICULA") or colab.get("MATRÍCULA") or colab.get("matricula") or "").strip().upper()
        if matricula and matricula not in seen:
            seen.add(matricula)
            out.append(matricula)
    return sorted(out)


@bp.route("/api/relatorio-lancamento", methods=["GET"])
def api_relatorio_lancamento():
    """Gera relatório de lançamentos de férias respeitando o escopo visível.

    Regras:
    - Gestor comum: relatório dos colaboradores subordinados a ele.
    - Admin/DP em simulação: relatório dos subordinados do gestor simulado.
    - Admin/DP sem simulação: relatório geral dos colaboradores ativos, salvo se for
      informado ?gestor_email= ou ?email= para gerar o relatório de um gestor específico.
    """
    user = session.get("user")
    if not user:
        return jsonify({"ok": False, "message": "Não autenticado"}), 401

    user_email = safe_lower(user.get("email") or "")
    role = get_user_role(user_email)

    is_dp_or_admin = role in ("DP", "admin")
    is_gestor_user = is_gestor(user_email)

    if not (is_dp_or_admin or is_gestor_user):
        return jsonify({"ok": False, "message": "Acesso negado"}), 403

    try:
        mes = request.args.get("mes", type=int)
        ano = request.args.get("ano", type=int)

        if mes and not (1 <= mes <= 12):
            return jsonify({"ok": False, "message": "Mês inválido"}), 400

        simulated_gestor = safe_lower(get_simulated_gestor() or "")
        gestor_param = safe_lower(
            request.args.get("gestor_email")
            or request.args.get("email")
            or ""
        )

        if is_dp_or_admin and simulated_gestor:
            # Ao simular, o relatório deve refletir exatamente a visão do gestor simulado,
            # e não o e-mail do admin/DP logado.
            target_email = simulated_gestor
            subs = get_subordinados(target_email)
            escopo = "simulacao_gestor"
        elif is_dp_or_admin and gestor_param:
            # Permite ao DP/Admin gerar relatório de um gestor específico.
            target_email = gestor_param
            subs = get_subordinados(target_email)
            escopo = "gestor_especifico"
        elif is_dp_or_admin:
            # Sem simulação e sem gestor específico, DP/Admin recebem relatório geral.
            target_email = user_email
            subs = _matriculas_colaboradores_ativos()
            escopo = "todos_ativos"
        else:
            # Gestor comum: apenas os colaboradores que ele pode solicitar.
            target_email = user_email
            subs = get_subordinados(user_email)
            escopo = "gestor_logado"

        # Resolve o escopo em lote. Evita uma consulta por colaborador, que fazia
        # o relatório exceder o timeout do worker em ambientes com poucos recursos.
        identificadores = {str(v or "").strip() for v in (subs or []) if str(v or "").strip()}
        escopo_matriculas = []
        if identificadores:
            from sqlalchemy import func, or_
            from ..models import Colaborador
            from ..services.postgres_compat_service import postgres_enabled
            from ..services.postgres_service import get_db_session

            if postgres_enabled():
                matriculas_diretas = {v.upper() for v in identificadores if "@" not in v}
                emails = {safe_lower(v) for v in identificadores if "@" in v}
                filtros = []
                if matriculas_diretas:
                    filtros.append(func.upper(Colaborador.matricula).in_(sorted(matriculas_diretas)))
                if emails:
                    filtros.append(func.lower(Colaborador.email).in_(sorted(emails)))
                if filtros:
                    rows = get_db_session().query(Colaborador.matricula).filter(or_(*filtros)).all()
                    escopo_matriculas = sorted({str(r[0] or "").strip().upper() for r in rows if r[0]})
            else:
                # No modo legado, get_subordinados já devolve os identificadores
                # aceitos pelo serviço do Smartsheet.
                escopo_matriculas = sorted({v.upper() for v in identificadores})

        relatorio = gerar_relatorio_lancamento(target_email, escopo_matriculas, mes, ano)
        if isinstance(relatorio, dict):
            relatorio.setdefault("escopo", escopo)
            relatorio.setdefault("gestor_referencia", target_email)
            relatorio.setdefault("total_colaboradores_escopo", len(escopo_matriculas))
        return jsonify(relatorio)

    except Exception as e:
        return jsonify({"ok": False, "message": f"Erro ao gerar relatório: {str(e)}"}), 500

