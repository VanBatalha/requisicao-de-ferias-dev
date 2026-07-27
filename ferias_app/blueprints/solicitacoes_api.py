from __future__ import annotations

from io import BytesIO

from flask import jsonify, request, session, send_file

from .base import bp
from ..services.relatorio_service import (
    RelatorioAcessoNegado,
    criar_relatorio_lancamento_xlsx,
    gerar_relatorio_lancamento,
    obter_relatorio_para_exportacao,
)
from ..services.solicitacoes_service import processar_solicitacao


@bp.route("/api/solicitar-ferias", methods=["POST"])
def api_solicitar_ferias():
    payload, status = processar_solicitacao(request.form.to_dict(flat=True), session.get("user"))
    return jsonify(payload), status


@bp.route("/api/relatorio-lancamento", methods=["GET"])
def api_relatorio_lancamento():
    """Histórico por equipe; DP/ADMIN recebem todos os registros do período."""
    user = session.get("user")
    if not user:
        return jsonify({"ok": False, "message": "Não autenticado"}), 401

    # A autenticação LDAP já validou o colaborador e salvou a matrícula na sessão.
    # Não fazemos nova busca por e-mail para gerar o relatório.
    usuario_matricula = str(user.get("matricula") or "").strip().upper()
    if not usuario_matricula:
        return jsonify({
            "ok": False,
            "message": "Sua sessão não possui matrícula. Saia do sistema e entre novamente.",
        }), 401

    try:
        mes = request.args.get("mes", type=int)
        ano = request.args.get("ano", type=int)
        if mes and not (1 <= mes <= 12):
            return jsonify({"ok": False, "message": "Mês inválido"}), 400
        if ano and not (2000 <= ano <= 2100):
            return jsonify({"ok": False, "message": "Ano inválido"}), 400

        relatorio = gerar_relatorio_lancamento(
            usuario_matricula,
            mes,
            ano,
            perfil_sessao=user.get("user_type"),
        )
        return jsonify(relatorio), (200 if relatorio.get("ok") else 503)

    except RelatorioAcessoNegado as exc:
        return jsonify({"ok": False, "message": str(exc)}), 403
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "message": f"Erro ao gerar relatório: {exc}"}), 500


@bp.route("/api/relatorio-lancamento.xlsx", methods=["GET"])
def api_relatorio_lancamento_xlsx():
    """Baixa em XLSX o mesmo relatório exibido na tela."""
    user = session.get("user")
    if not user:
        return jsonify({"ok": False, "message": "Não autenticado"}), 401

    usuario_matricula = str(user.get("matricula") or "").strip().upper()
    if not usuario_matricula:
        return jsonify({
            "ok": False,
            "message": "Sua sessão não possui matrícula. Saia do sistema e entre novamente.",
        }), 401

    try:
        mes = request.args.get("mes", type=int)
        ano = request.args.get("ano", type=int)
        if mes and not (1 <= mes <= 12):
            return jsonify({"ok": False, "message": "Mês inválido"}), 400
        if ano and not (2000 <= ano <= 2100):
            return jsonify({"ok": False, "message": "Ano inválido"}), 400

        relatorio = obter_relatorio_para_exportacao(
            usuario_matricula,
            mes,
            ano,
            perfil_sessao=user.get("user_type"),
        )
        if not relatorio.get("ok"):
            return jsonify(relatorio), 503

        arquivo = criar_relatorio_lancamento_xlsx(relatorio)
        periodo = f"{ano or 'todos'}-{int(mes):02d}" if mes else str(ano or "todos-periodos")
        nome = f"relatorio_solicitacoes_{periodo}.xlsx"
        return send_file(
            BytesIO(arquivo),
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name=nome,
            max_age=0,
        )
    except RelatorioAcessoNegado as exc:
        return jsonify({"ok": False, "message": str(exc)}), 403
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "message": f"Erro ao exportar relatório: {exc}"}), 500
