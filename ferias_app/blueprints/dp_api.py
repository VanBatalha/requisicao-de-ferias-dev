from __future__ import annotations

import datetime as dt
from io import BytesIO
import re
import unicodedata

import smartsheet
from flask import jsonify, request, session, send_file

from .base import bp
from ..core import (
    ID_FOLHA_CADASTRO,
    ID_FOLHA_SOLICITACOES,
    atualizar_relacao_gestor,
    col_id_by_name,
    get_col_map,
    get_ferias_mes,
    get_resumo_ferias,
    get_sheet_solicitacoes,
    get_smartsheet_client,
    get_subordinados_direto,
    invalidate_sheet_cache,
    is_colaborador_ativo,
    listar_colaboradores,
    safe_lower,
    tem_grupo,
)
# OBS: o módulo legacy tem vários helpers com prefixo "_".
# Eles NÃO entram em importações genéricas, então precisam ser
# importados explicitamente quando usados aqui.
from ..services.core_support import (
    _canonical_status,
    _col_id,
    _cols_norm_map,
    _infer_saldo_tipo,
    _is_ajuste,
    _norm_email,
    _norm_status,
    _norm_title,
    _parse_date_value,
)
def _has_dp_access(email: str | None) -> bool:
    """Retorna True para perfis DP e ADMIN com acesso ao Painel DP.

    A sessão é verificada primeiro para que um ADMIN já autenticado não dependa
    de uma nova resolução de permissões durante cada chamada da API. A consulta
    por e-mail permanece como fallback e a função continua fail-closed.
    """
    try:
        user = session.get("user") or {}
        user_type = str(user.get("user_type") or "").strip().upper()
        grupos_sessao = {str(g or "").strip().upper() for g in (user.get("grupos") or [])}
        if user_type in {"ADMIN", "ADMINISTRADOR", "DP", "RH"}:
            return True
        if grupos_sessao.intersection({"ADMIN", "ADMINISTRADOR", "DP", "RH"}):
            return True
        if not email:
            return False
        return tem_grupo(email, "DP") or tem_grupo(email, "Administrador")
    except Exception:
        return False

@bp.route("/api/dp/colaboradores")
def api_dp_colaboradores():
    user = session.get("user")
    if not user or not _has_dp_access(user.get("email")):
        return jsonify({"ok": False, "message": "Acesso negado"}), 403

    status_filter = (request.args.get("status") or "").upper().strip()
    try:
        from ..services.postgres_compat_service import postgres_enabled
        if postgres_enabled():
            from ..services.postgres_service import listar_colaboradores_com_saldos
            colaboradores = listar_colaboradores_com_saldos(status_filter or None)
        else:
            colaboradores = listar_colaboradores()
            if status_filter == "ATIVO":
                colaboradores = [c for c in colaboradores if is_colaborador_ativo(c)]
            elif status_filter == "INATIVO":
                colaboradores = [c for c in colaboradores if not is_colaborador_ativo(c)]

        colaboradores = sorted(
            colaboradores,
            key=lambda c: (str(c.get("NOME COMPLETO") or c.get("nome_completo") or c.get("nome") or "").casefold(), str(c.get("matricula") or c.get("MATRICULA") or ""))
        )
        return jsonify({"ok": True, "colaboradores": colaboradores, "total": len(colaboradores)})
    except Exception as e:
        print(f"ERRO em api_dp_colaboradores: {e}")
        return jsonify({"ok": False, "message": str(e)}), 500


@bp.route("/api/dp/relatorio-saldos.xlsx")
def api_dp_relatorio_saldos_xlsx():
    """Exporta o relatório de saldos com o mesmo padrão visual do relatório de solicitações."""
    user = session.get("user")
    if not user or not _has_dp_access(user.get("email")):
        return jsonify({"ok": False, "message": "Acesso negado"}), 403

    status_filter = (request.args.get("status") or "").strip().upper()
    busca = (request.args.get("busca") or "").strip()
    try:
        from ..services.postgres_compat_service import postgres_enabled
        if not postgres_enabled():
            return jsonify({"ok": False, "message": "O relatório XLSX de saldos requer PostgreSQL."}), 500

        from ..services.postgres_service import listar_colaboradores_com_saldos
        from ..services.saldos_report_service import criar_relatorio_saldos_xlsx

        colaboradores = listar_colaboradores_com_saldos(status_filter or None)
        if busca:
            busca_norm = unicodedata.normalize("NFD", busca).encode("ascii", "ignore").decode("ascii").casefold()
            # O autocomplete exibe "MATRÍCULA | NOME". A busca do relatório
            # trabalha por tokens para aceitar esse rótulo e também pesquisas livres.
            tokens = [token for token in re.split(r"[^a-z0-9@._-]+", busca_norm) if token]
            filtrados = []
            for item in colaboradores:
                texto = " ".join(str(item.get(k) or "") for k in (
                    "matricula", "MATRICULA", "MATRÍCULA", "nome_completo", "NOME COMPLETO",
                    "email", "EMAIL DA EMPRESA", "cargo", "CARGO", "setor", "SETOR",
                ))
                texto_norm = unicodedata.normalize("NFD", texto).encode("ascii", "ignore").decode("ascii").casefold()
                if all(token in texto_norm for token in tokens):
                    filtrados.append(item)
            colaboradores = filtrados

        arquivo = criar_relatorio_saldos_xlsx(
            colaboradores,
            status_filtro=status_filter,
            busca=busca,
        )
        data_ref = dt.datetime.now().strftime("%Y%m%d_%H%M")
        return send_file(
            BytesIO(arquivo),
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name=f"relatorio_saldos_colaboradores_{data_ref}.xlsx",
            max_age=0,
        )
    except Exception as e:
        return jsonify({"ok": False, "message": f"Erro ao gerar relatório de saldos: {e}"}), 500


@bp.route("/api/dp/saldos/<path:identificador>")
def api_dp_saldos(identificador):
    user = session.get("user")
    if not user or not _has_dp_access(user.get("email")):
        return jsonify({"ok": False, "message": "Acesso negado"}), 403
    identificador = str(identificador or "").strip()
    if not identificador:
        return jsonify({"ok": False, "message": "Matrícula inválida"}), 400
    try:
        from ..services.postgres_compat_service import postgres_enabled
        if postgres_enabled():
            from ..services.postgres_service import get_saldos_colaborador
            saldos = get_saldos_colaborador(identificador)
            return jsonify({
                "ok": True, "matricula": identificador.upper(),
                "regular": {"direito": saldos["regular"]["direito"], "usados": saldos["regular"]["usado"], "reservados": saldos["regular"]["reservado"], "saldo": saldos["regular"]["disponivel"]},
                "premium": {"direito": saldos["premium"]["direito"], "usados": saldos["premium"]["usado"], "reservados": saldos["premium"]["reservado"], "saldo": saldos["premium"]["disponivel"]},
            })
        resumo = get_resumo_ferias(identificador)
        return jsonify({"ok": True, "identificador": identificador, "regular": resumo["regular"], "premium": resumo["premium"]})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


@bp.route("/api/dp/ajustes/lancar", methods=["POST"])
def api_dp_ajustes_lancar():
    """Lança ajuste diretamente no PostgreSQL.

    Nesta versão, o app não depende mais do Smartsheet para ajustes. O ajuste é
    registrado em solicitacoes_ferias com colaborador_matricula e altera o saldo
    real em saldo_periodo, com registro na auditoria geral.
    """
    try:
        user = session.get("user")
        if not user or not _has_dp_access(user.get("email")):
            return jsonify({"ok": False, "message": "Acesso negado"}), 403

        payload = request.get_json(silent=True) or {}
        colab_matricula = str(payload.get("colaborador_matricula") or payload.get("matricula") or "").strip().upper()
        solicitacao_raw = (payload.get("solicitacao") or "").strip()
        obs_user = (payload.get("observacoes") or "").strip()
        try:
            dias = int(float(payload.get("dias") or 0))
        except Exception:
            dias = 0

        if not colab_matricula:
            return jsonify({"ok": False, "message": "Matrícula do colaborador inválida"}), 400
        if dias == 0:
            return jsonify({"ok": False, "message": "Dias deve ser diferente de zero"}), 400

        ns = _norm_title(solicitacao_raw)
        if ns in ("ajuste ferias", "ajuste férias"):
            solicitacao = "AJUSTE FÉRIAS"
            saldo_tipo = "REGULAR"
        elif ns in ("ajuste premium", "ajuste certariana", "ajuste licenca certariana", "ajuste licença certariana", "ajuste licenca", "ajuste licença"):
            solicitacao = "AJUSTE CERTARIANA"
            saldo_tipo = "PREMIUM"
        else:
            return jsonify({"ok": False, "message": "Solicitação inválida"}), 400

        dp_email = safe_lower(user.get("email") or "")
        obs_final = obs_user
        complemento = f"Ajuste feito pelo DP ({dp_email})"
        if complemento.lower() not in obs_final.lower():
            obs_final = (obs_final + ("\n" if obs_final else "") + complemento).strip()

        from ..services.postgres_compat_service import postgres_enabled
        if not postgres_enabled():
            return jsonify({"ok": False, "message": "PostgreSQL não configurado. Ajustes via Smartsheet foram desativados nesta versão."}), 500

        from ..services.postgres_service import criar_solicitacao
        hoje = dt.date.today()
        ok, msg, row_id = criar_solicitacao({
            "colaborador_matricula": colab_matricula,
            "gestor_email": dp_email,
            "criado_por": dp_email,
            "solicitacao": solicitacao,
            "saldo_tipo": saldo_tipo,
            "data_inicio": hoje,
            "data_fim": hoje,
            "dias": dias,
            "status": "APROVADO",
            "observacoes": obs_final,
            "is_ajuste": True,
            "metadata": {"origem": "painel_dp", "tipo": "ajuste_saldo"},
        })
        if not ok:
            return jsonify({"ok": False, "message": msg or "Erro ao lançar ajuste."}), 500

        retorno = {
            "ok": True,
            "message": "Ajuste lançado com sucesso.",
            "row_id": row_id,
            "inserted_ids": [row_id] if row_id else [],
        }
        try:
            resumo = get_resumo_ferias(colab_matricula)
            retorno["regular"] = resumo.get("regular")
            retorno["premium"] = resumo.get("premium")
        except Exception as e:
            retorno["warning"] = f"Ajuste salvo, mas não foi possível recalcular os saldos imediatamente: {e}"
        return jsonify(retorno)
    except Exception as e:
        return jsonify({"ok": False, "message": f"Erro ao lançar ajuste: {e}"}), 500

@bp.route("/api/dp/colaborador/<email>")
def api_dp_colaborador(email):
    user = session.get("user")
    if not user or not _has_dp_access(user.get("email")):
        return jsonify({"ok": False, "message": "Acesso negado"}), 403
    
    try:
        colaboradores = listar_colaboradores()
        email_lower = safe_lower(email)
        colab = next(
            (c for c in colaboradores if safe_lower(c.get("EMAIL DA EMPRESA")) == email_lower),
            None
        )
        
        if not colab:
            return jsonify({"ok": False, "message": "Colaborador não encontrado"}), 404
        
        return jsonify({
            "ok": True,
            "colaborador": colab
        })
    except Exception as e:
        print(f"ERRO em api_dp_colaborador: {e}")
        return jsonify({"ok": False, "message": str(e)}), 500


# ============================================
# API: DP - GESTORES (relacao por matricula)
# ============================================

def _gestores_session():
    from ..services.postgres_service import get_db_session
    return get_db_session()


def _is_active_status(value):
    return str(value or 'ATIVO').strip().upper() in {'ATIVO', 'ACTIVE'}


def _resolve_colaborador_identity(db, value):
    """Resolve matricula, e-mail ou local-part, priorizando o cadastro ativo."""
    from sqlalchemy import func
    from ..models import Colaborador

    value = str(value or '').strip()
    if not value:
        return None

    upper = value.upper()
    colab = db.query(Colaborador).filter(func.upper(Colaborador.matricula) == upper).first()
    if colab:
        return colab

    lower = value.lower()
    rows = db.query(Colaborador).filter(func.lower(Colaborador.email) == lower).all()
    if not rows and '@' not in lower:
        rows = db.query(Colaborador).filter(
            func.split_part(func.lower(Colaborador.email), '@', 1) == lower
        ).all()
    rows.sort(
        key=lambda c: (1 if _is_active_status(c.status) else 0, int(c.id or 0)),
        reverse=True,
    )
    return rows[0] if rows else None


def _colab_label(c):
    mat = str(getattr(c, 'matricula', '') or '').strip()
    nome = str(getattr(c, 'nome_completo', '') or '').strip() or str(getattr(c, 'email', '') or '')
    return f"{mat} | {nome}" if mat else nome


def _sort_text_pt(value):
    value = str(value or '')
    value = unicodedata.normalize('NFD', value)
    value = ''.join(ch for ch in value if unicodedata.category(ch) != 'Mn')
    return value.casefold().strip()


def _sort_key_colab(c):
    return (
        _sort_text_pt(getattr(c, 'nome_completo', '') or getattr(c, 'email', '') or ''),
        str(getattr(c, 'matricula', '') or ''),
    )


def _normalizar_ref_matricula(value, allow_dp=True, allow_gestor=True):
    raw = str(value or '').strip()
    if not raw or '@' in raw:
        return ''
    norm = raw.upper().strip()
    if allow_dp and norm in {'DP', 'RH', 'DEPARTAMENTO PESSOAL'}:
        return 'DP'
    if allow_gestor and norm in {'GESTOR', 'GESTORES', 'GESTOR DIRETO'}:
        return 'GESTOR'
    if re.fullmatch(r'MAT\d+', norm):
        return norm
    if re.fullmatch(r'\d+', norm):
        return f'MAT{int(norm):05d}'
    return ''


def _colaborador_ativo_por_matricula(db, matricula):
    from sqlalchemy import func
    from ..models import Colaborador

    mat = _normalizar_ref_matricula(matricula, allow_dp=False, allow_gestor=False)
    if not mat:
        return None
    return db.query(Colaborador).filter(
        func.upper(Colaborador.matricula) == mat,
        func.upper(func.coalesce(Colaborador.status, 'ATIVO')).in_(['ATIVO', 'ACTIVE']),
    ).first()


def _ensure_complemento(db, colab):
    from ..models import ColaboradorComplemento

    comp = db.query(ColaboradorComplemento).filter(
        ColaboradorComplemento.colaborador_id == colab.id
    ).first()
    if not comp:
        comp = ColaboradorComplemento(
            colaborador_id=colab.id,
            colaborador_matricula=colab.matricula,
            user_type='USER',
            ativo_no_app=True,
        )
        db.add(comp)
        db.flush()
    comp.colaborador_matricula = colab.matricula
    return comp


def _ensure_hierarquia(db, colab):
    from ..models import HierarquiaGestao

    h = db.query(HierarquiaGestao).filter(
        HierarquiaGestao.colaborador_matricula == colab.matricula
    ).first()
    if not h:
        h = HierarquiaGestao(
            colaborador_id=colab.id,
            colaborador_matricula=colab.matricula,
        )
        db.add(h)
        db.flush()
    h.colaborador_id = colab.id
    h.colaborador_matricula = colab.matricula
    return h


def _refs_gestao(comp=None, hierarquia=None):
    """HierarquiaGestao e a fonte principal; complemento e apenas compatibilidade."""
    gd = str(getattr(hierarquia, 'gestor_direto_matricula', '') or '').strip().upper()
    gs = str(getattr(hierarquia, 'gestor_superior_matricula', '') or '').strip().upper()
    if not gd:
        gd = str(getattr(comp, 'gestor_direto', '') or '').strip().upper()
    if not gs:
        gs = str(getattr(comp, 'gestor_superior', '') or '').strip().upper()
    return gd, gs


def _colab_payload(c, comp=None, hierarquia=None):
    gd, gs = _refs_gestao(comp, hierarquia)
    return {
        'id': c.id,
        'matricula': c.matricula or '',
        'email': (c.email or '').lower(),
        'nome': c.nome_completo or '',
        'label': _colab_label(c),
        'status': c.status or '',
        'gestor_direto': gd,
        'gestor_superior': gs,
        'gestor_direto_email': (
            getattr(hierarquia, 'gestor_direto_email', None)
            or getattr(comp, 'gestor_direto_email', None)
            or ''
        ),
        'gestor_superior_email': (
            getattr(hierarquia, 'gestor_superior_email', None)
            or getattr(comp, 'gestor_superior_email', None)
            or ''
        ),
    }


def _listar_colaboradores_ativos_db(db):
    from sqlalchemy import func
    from ..models import Colaborador

    rows = db.query(Colaborador).filter(
        func.upper(func.coalesce(Colaborador.status, 'ATIVO')).in_(['ATIVO', 'ACTIVE'])
    ).all()
    return sorted(rows, key=_sort_key_colab)


def _listar_gestao_ativos(db):
    """Carrega cadastro, complemento e hierarquia em uma unica consulta."""
    from sqlalchemy import func
    from ..models import Colaborador, ColaboradorComplemento, HierarquiaGestao

    rows = (
        db.query(Colaborador, ColaboradorComplemento, HierarquiaGestao)
        .outerjoin(
            ColaboradorComplemento,
            ColaboradorComplemento.colaborador_id == Colaborador.id,
        )
        .outerjoin(
            HierarquiaGestao,
            HierarquiaGestao.colaborador_matricula == Colaborador.matricula,
        )
        .filter(func.upper(func.coalesce(Colaborador.status, 'ATIVO')).in_(['ATIVO', 'ACTIVE']))
        .all()
    )
    return sorted(rows, key=lambda row: _sort_key_colab(row[0]))


def _set_gestor_direto(db, colab, gestor=None):
    comp = _ensure_complemento(db, colab)
    h = _ensure_hierarquia(db, colab)
    if gestor:
        mat = str(gestor.matricula or '').upper()
        email = (gestor.email or '').lower() or None
        comp.gestor_direto = mat
        comp.gestor_direto_email = email
        h.gestor_direto_id = gestor.id
        h.gestor_direto_matricula = mat
        h.gestor_direto_email = email
    else:
        comp.gestor_direto = None
        comp.gestor_direto_email = None
        h.gestor_direto_id = None
        h.gestor_direto_matricula = None
        h.gestor_direto_email = None


@bp.route('/api/dp/gestores/mapa', methods=['GET'])
def api_dp_gestores_mapa():
    user = session.get('user')
    if not user or not _has_dp_access(user.get('email')):
        return jsonify({'ok': False, 'message': 'Acesso negado'}), 403
    try:
        db = _gestores_session()
        rows = _listar_gestao_ativos(db)
        por_mat = {
            str(c.matricula or '').upper(): (c, comp, h)
            for c, comp, h in rows
            if c.matricula
        }
        gestores_diretos = {}
        gestores_superiores = {}
        for c, comp, h in rows:
            gd, gs = _refs_gestao(comp, h)
            cm = str(c.matricula or '').upper()
            payload = _colab_payload(c, comp, h)
            if gd and gd in por_mat and gd != cm:
                gestores_diretos.setdefault(gd, []).append(payload)
            if gs:
                gestores_superiores.setdefault(gs, []).append(payload)

        diretos = []
        for gestor_mat, subs in gestores_diretos.items():
            gestor_c, gestor_comp, gestor_h = por_mat[gestor_mat]
            subs.sort(key=lambda item: (_sort_text_pt(item.get('nome')), item.get('matricula', '')))
            diretos.append(dict(
                _colab_payload(gestor_c, gestor_comp, gestor_h),
                subordinados=subs,
                total=len(subs),
            ))
        diretos.sort(key=lambda item: (_sort_text_pt(item.get('nome')), item.get('matricula', '')))

        superiores = []
        for gestor_mat, subs in gestores_superiores.items():
            subs.sort(key=lambda item: (_sort_text_pt(item.get('nome')), item.get('matricula', '')))
            if gestor_mat in por_mat:
                gestor_c, gestor_comp, gestor_h = por_mat[gestor_mat]
                base = _colab_payload(gestor_c, gestor_comp, gestor_h)
            else:
                base = {'matricula': gestor_mat, 'nome': gestor_mat, 'email': '', 'label': gestor_mat}
            superiores.append(dict(base, subordinados=subs, total=len(subs)))
        superiores.sort(key=lambda item: (_sort_text_pt(item.get('nome')), item.get('matricula', '')))

        return jsonify({
            'ok': True,
            'colaboradores': [_colab_payload(c, comp, h) for c, comp, h in rows],
            'gestores_diretos': diretos,
            'gestores_superiores': superiores,
        })
    except Exception as e:
        return jsonify({'ok': False, 'message': f'Erro ao carregar mapa de gestores: {e}'}), 500


@bp.route('/api/dp/gestores/relacao', methods=['GET', 'POST'])
def api_dp_gestores_relacao():
    user = session.get('user')
    if not user or not _has_dp_access(user.get('email')):
        return jsonify({'ok': False, 'message': 'Acesso negado'}), 403

    db = _gestores_session()

    if request.method == 'GET':
        try:
            gestor_ident = (request.args.get('gestor') or '').strip()
            gestor = _resolve_colaborador_identity(db, gestor_ident)
            if not gestor:
                return jsonify({
                    'ok': True,
                    'gestor': '',
                    'gestor_matricula': '',
                    'subordinados': [],
                    'subordinados_detalhes': [],
                })

            gestor_mat = str(gestor.matricula or '').upper()
            subs = []
            for c, comp, h in _listar_gestao_ativos(db):
                gd, _ = _refs_gestao(comp, h)
                if gd == gestor_mat:
                    subs.append(_colab_payload(c, comp, h))
            return jsonify({
                'ok': True,
                'gestor': gestor.email or '',
                'gestor_matricula': gestor.matricula or '',
                'gestor_label': _colab_label(gestor),
                'subordinados': [c['matricula'] for c in subs if c.get('matricula')],
                'subordinados_detalhes': subs,
            })
        except Exception as e:
            return jsonify({'ok': False, 'message': f'Erro ao carregar relacao: {e}'}), 500

    payload = request.get_json(silent=True) or {}
    gestor = _resolve_colaborador_identity(db, payload.get('gestor') or '')
    subordinados_raw = payload.get('subordinados') or payload.get('subordinates') or []
    if isinstance(subordinados_raw, str):
        subordinados_raw = [subordinados_raw]
    if not gestor or not _is_active_status(gestor.status):
        return jsonify({'ok': False, 'message': 'Gestor ativo e obrigatorio'}), 400

    try:
        gestor_matricula = str(gestor.matricula or '').upper()
        selecionadas = set()
        for item in subordinados_raw:
            c = _resolve_colaborador_identity(db, item)
            if (
                c
                and _is_active_status(c.status)
                and c.matricula
                and str(c.matricula).upper() != gestor_matricula
            ):
                selecionadas.add(str(c.matricula).upper())

        for c, comp, h in _listar_gestao_ativos(db):
            cm = str(c.matricula or '').upper()
            atual, _ = _refs_gestao(comp, h)
            if cm in selecionadas:
                _set_gestor_direto(db, c, gestor)
            elif atual == gestor_matricula:
                _set_gestor_direto(db, c, None)

        db.commit()
        return jsonify({
            'ok': True,
            'message': 'Relacao atualizada com sucesso.',
            'gestor_matricula': gestor_matricula,
            'subordinados': sorted(selecionadas),
        })
    except Exception as e:
        db.rollback()
        return jsonify({'ok': False, 'message': f'Erro ao salvar relacao: {e}'}), 500


@bp.route('/api/dp/gestores/superior', methods=['GET', 'POST'])
def api_dp_gestor_superior():
    user = session.get('user')
    if not user or not _has_dp_access(user.get('email')):
        return jsonify({'ok': False, 'message': 'Acesso negado'}), 403
    db = _gestores_session()

    if request.method == 'GET':
        colaborador_ident = (request.args.get('colaborador') or '').strip()
        colab = _resolve_colaborador_identity(db, colaborador_ident)
        if not colab:
            return jsonify({'ok': False, 'message': 'Colaborador nao encontrado'}), 404
        comp = _ensure_complemento(db, colab)
        h = _ensure_hierarquia(db, colab)
        _, valor = _refs_gestao(comp, h)
        return jsonify({
            'ok': True,
            'colaborador': colab.matricula or '',
            'gestor_superior': valor or 'GESTOR',
        })

    payload = request.get_json(silent=True) or {}
    colab = _resolve_colaborador_identity(db, payload.get('colaborador') or '')
    valor_raw = (payload.get('gestor_superior') or payload.get('valor') or '').strip()
    if not colab or not _is_active_status(colab.status):
        return jsonify({'ok': False, 'message': 'Colaborador ativo e obrigatorio'}), 400
    if not valor_raw:
        return jsonify({'ok': False, 'message': 'Gestor Superior e obrigatorio'}), 400

    try:
        comp = _ensure_complemento(db, colab)
        h = _ensure_hierarquia(db, colab)
        norm = valor_raw.strip().upper()
        if norm in {'DP', 'RH'}:
            comp.gestor_superior = 'DP'
            comp.gestor_superior_email = 'dp'
            h.gestor_superior_id = None
            h.gestor_superior_matricula = 'DP'
            h.gestor_superior_email = 'dp'
        elif norm in {'GESTOR', 'GESTORES', 'GESTOR DIRETO'}:
            comp.gestor_superior = 'GESTOR'
            comp.gestor_superior_email = 'gestor'
            h.gestor_superior_id = None
            h.gestor_superior_matricula = 'GESTOR'
            h.gestor_superior_email = 'gestor'
        else:
            sup = _resolve_colaborador_identity(db, valor_raw)
            if not sup or not _is_active_status(sup.status):
                return jsonify({'ok': False, 'message': 'Gestor Superior ativo nao encontrado'}), 404
            comp.gestor_superior = sup.matricula
            comp.gestor_superior_email = (sup.email or '').lower() or None
            h.gestor_superior_id = sup.id
            h.gestor_superior_matricula = sup.matricula
            h.gestor_superior_email = (sup.email or '').lower() or None
        db.commit()
        return jsonify({
            'ok': True,
            'message': 'Gestor Superior atualizado com sucesso.',
            'gestor_superior': h.gestor_superior_matricula,
        })
    except Exception as e:
        db.rollback()
        return jsonify({'ok': False, 'message': f'Erro ao atualizar Gestor Superior: {e}'}), 500


# ============================================
# API: DP - HISTORICO SOMENTE LEITURA
# ============================================

def _serialize_dp_history_date(value):
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    return value if value not in (None, '') else None


def _solicitacao_dp_payload(row):
    dias = row.dias if row.dias is not None else row.dias_solicitados
    titulo = row.solicitacao or row.tipo_solicitacao or ('AJUSTE' if row.is_ajuste else 'FERIAS')
    saldo_tipo = (row.saldo_tipo or row.tipo_ferias or 'REGULAR').upper()
    ajuste = bool(row.is_ajuste) or 'AJUSTE' in _norm_title(titulo).upper()
    return {
        'id': row.id,
        'is_ajuste': ajuste,
        'solicitacao': titulo,
        'saldo_tipo': saldo_tipo,
        'data_inicio': _serialize_dp_history_date(row.data_inicio),
        'data_fim': _serialize_dp_history_date(row.data_fim),
        'dias': float(dias or 0),
        'status': row.status or '',
        'observacoes': row.observacoes or '',
        'periodo_aquisitivo_origem': row.periodo_aquisitivo_origem or '',
        'solicitante_matricula': row.solicitante_matricula or '',
        'criado_por': row.criado_por or row.gestor_solicitante_email or '',
        'created_at': _serialize_dp_history_date(row.created_at),
        'updated_at': _serialize_dp_history_date(row.updated_at),
    }


@bp.route('/api/dp/historico/<path:identificador>', methods=['GET'])
def api_dp_historico_colaborador(identificador):
    """Exibe solicitacoes e ajustes ao DP sem endpoints de alteracao/exclusao."""
    user = session.get('user')
    if not user or not _has_dp_access(user.get('email')):
        return jsonify({'ok': False, 'message': 'Acesso negado'}), 403

    try:
        from sqlalchemy import and_, func, or_
        from ..models import Colaborador, Solicitacao

        db = _gestores_session()
        colab = _resolve_colaborador_identity(db, identificador)
        if not colab:
            return jsonify({'ok': False, 'message': 'Colaborador nao encontrado'}), 404

        vinculos = [
            Solicitacao.colaborador_matricula == colab.matricula,
            Solicitacao.colaborador_id == colab.id,
        ]
        if colab.email:
            vinculos.append(and_(
                Solicitacao.colaborador_matricula.is_(None),
                Solicitacao.colaborador_id.is_(None),
                func.lower(Solicitacao.colaborador_email) == str(colab.email).lower(),
            ))

        rows = (
            db.query(Solicitacao)
            .filter(or_(*vinculos))
            .order_by(Solicitacao.data_inicio.desc().nullslast(), Solicitacao.id.desc())
            .all()
        )
        itens = [_solicitacao_dp_payload(row) for row in rows]
        ajustes = [item for item in itens if item['is_ajuste']]
        solicitacoes = [item for item in itens if not item['is_ajuste']]

        return jsonify({
            'ok': True,
            'colaborador': {
                'id': colab.id,
                'matricula': colab.matricula or '',
                'nome': colab.nome_completo or '',
                'email': colab.email or '',
                'status': colab.status or '',
            },
            'solicitacoes': solicitacoes,
            'ajustes': ajustes,
            'totais': {
                'solicitacoes': len(solicitacoes),
                'ajustes': len(ajustes),
            },
        })
    except Exception as e:
        return jsonify({'ok': False, 'message': f'Erro ao carregar historico: {e}'}), 500


# API: dp - FÉRIAS (Planilha 2890766507528068)
# ============================================

@bp.route("/api/dp/ferias-mes")
def api_dp_ferias_mes():
    user = session.get("user")
    if not user or not _has_dp_access(user.get("email")):
        return jsonify({"ok": False, "message": "Acesso negado"}), 403
    
    mes = request.args.get("mes", type=int)
    ano = request.args.get("ano", type=int)
    
    if not mes or not ano:
        hoje = dt.date.today()
        mes = hoje.month
        ano = hoje.year
    
    try:
        ferias = get_ferias_mes(mes, ano)
        
        return jsonify({
            "ok": True,
            "ferias": ferias,
            "mes": mes,
            "ano": ano
        })
    except Exception as e:
        print(f"ERRO em api_dp_ferias_mes: {e}")
        return jsonify({"ok": False, "message": str(e)}), 500

# ============================================
# API: DP ALTERAR STATUS
# ============================================

@bp.route("/api/dp/atualizar-status-solicitacao", methods=["POST"])
def api_dp_atualizar_status():
    """DP pode alterar status das solicitacoes"""
    user = session.get("user")
    if not user or not _has_dp_access(user.get("email")):
        return jsonify({"ok": False, "message": "Acesso negado"}), 403
    
    payload = request.get_json(silent=True) or {}
    row_id = payload.get("row_id")
    novo_status = (payload.get("status") or "").strip()
    
    if not row_id or not novo_status:
        return jsonify({"ok": False, "message": "row_id e status sao obrigatorios"}), 400
    
    # Status permitidos
    status_permitidos = ["APROVADA", "CANCELADA", "REPROVADO", "EM ANÁLISE", "EM ANALISE", "PENDENTE"]
    novo_status_upper = novo_status.upper()
    if novo_status_upper not in status_permitidos:
        return jsonify({"ok": False, "message": f"Status nao permitido. Use um de: {', '.join(status_permitidos)}"}), 400
    
    try:
        from ..services.postgres_compat_service import postgres_enabled
        if postgres_enabled():
            from ..services.postgres_service import atualizar_solicitacao
            ok, msg = atualizar_solicitacao(int(row_id), {"status": _canonical_status(novo_status_upper)})
            if not ok:
                return jsonify({"ok": False, "message": msg or "Erro ao atualizar status"}), 500
            return jsonify({"ok": True, "message": f"Status atualizado para {novo_status_upper}"})

        client = get_smartsheet_client()
        sheet_sol = get_sheet_solicitacoes(client)
        cols_sol = get_col_map(sheet_sol)
        
        row_id_int = int(row_id)
        col_status = col_id_by_name(sheet_sol, "STATUS")

        if not col_status:
            return jsonify({"ok": False, "message": "Coluna STATUS nao encontrada"}), 500
        
        row_update = smartsheet.models.Row()
        row_update.id = row_id_int
        row_update.cells = [{"column_id": col_status, "value": _canonical_status(novo_status_upper)}]
        
        client.Sheets.update_rows(ID_FOLHA_SOLICITACOES, [row_update])
        invalidate_sheet_cache(ID_FOLHA_SOLICITACOES)
        
        return jsonify({"ok": True, "message": f"Status atualizado para {novo_status_upper}"})
    except Exception as e:
        print(f"ERRO em api_dp_atualizar_status: {e}")
        return jsonify({"ok": False, "message": str(e)}), 500

# ============================================
# LICENÇA CERTARIANA (PREMIUM) — REGRAS DE FRACIONAMENTO
# ============================================

def _listar_segmentos_premium(colaborador_email: str, win_start: dt.date | None, win_end: dt.date | None,
                             exclude_row_id: int | None = None,
                             include_statuses: set[str] | None = None) -> list[dict]:
    """Lista segmentos PREMIUM (Licença Certariana) do colaborador, filtrando pela janela.

    Retorna dicts: {row_id, ini, fim, dias, status}
    - Considera apenas linhas que NÃO são ajustes.
    - Determina o tipo PREMIUM via coluna 'SALDO TIPO' ou marker em OBSERVAÇÕES.
    - Se win_start/win_end forem None, não filtra por janela.
    """
    client = get_smartsheet_client()
    if not client:
        return []

    colaborador_email = safe_lower(colaborador_email)
    if not colaborador_email:
        return []

    sheet_sol = get_sheet_solicitacoes(client)
    cols = get_col_map(sheet_sol)
    colsN = _cols_norm_map(cols)

    col_colab = _col_id(colsN, "COLABORADOR")
    col_status = _col_id(colsN, "STATUS")
    col_dias = _col_id(colsN, "DIAS")
    col_solic = _col_id(colsN, "SOLICITAÇÃO", "SOLICITACAO")
    col_obs = _col_id(colsN, "OBSERVAÇÕES", "OBSERVACOES", "OBSERVACAO")
    col_tipo = _col_id(colsN, "SALDO TIPO", "TIPO SALDO", "TIPO_DE_SALDO", "TIPO DE SALDO")
    col_inicio = _col_id(colsN, "DATA INICIO", "DATA INÍCIO", "DATA INICIAL", "INICIO", "INÍCIO")
    col_fim = _col_id(colsN, "DATA FIM", "DATA FINAL", "FIM")

    out = []
    for row in sheet_sol.rows:
        try:
            if exclude_row_id is not None and int(row.id) == int(exclude_row_id):
                continue
        except Exception:
            pass

        solicit = next((c.value for c in row.cells if c.column_id == (col_solic or -1)), "") or ""
        if _is_ajuste(solicit):
            continue

        row_key = next((c.value for c in row.cells if c.column_id == (col_colab or -1)), None)
        if not row_key or safe_lower(str(row_key)) != colaborador_email:
            continue

        status = next((c.value for c in row.cells if c.column_id == (col_status or -1)), "") or ""
        st_norm = _norm_status(status)
        if include_statuses and st_norm not in include_statuses:
            continue

        dias_val = next((c.value for c in row.cells if c.column_id == (col_dias or -1)), 0) or 0
        try:
            dias = int(float(dias_val or 0))
        except Exception:
            dias = 0

        obs = next((c.value for c in row.cells if c.column_id == (col_obs or -1)), "") or ""
        explicit_tipo = next((c.value for c in row.cells if c.column_id == (col_tipo or -1)), "") or ""

        saldo_tipo = _infer_saldo_tipo(obs, explicit_tipo)
        if saldo_tipo != "PREMIUM":
            continue

        ini_val = next((c.value for c in row.cells if c.column_id == (col_inicio or -1)), None)
        fim_val = next((c.value for c in row.cells if c.column_id == (col_fim or -1)), None)

        ini = _parse_date_value(ini_val) if ini_val else None
        fim = _parse_date_value(fim_val) if fim_val else None

        # fallback: se não tiver data fim, estima por dias
        if ini and not fim and dias > 0:
            fim = ini + dt.timedelta(days=dias - 1)

        if not ini or not fim:
            continue

        if win_start and win_end:
            if not (win_start <= ini < win_end):
                continue

        out.append({
            "row_id": getattr(row, "id", None),
            "ini": ini,
            "fim": fim,
            "dias": int(dias),
            "status": str(status or ""),
            "status_norm": st_norm,
        })

    out.sort(key=lambda x: x["ini"])
    return out


def _validar_fracionamento_certariana(
    direito_total: int,
    dt_inicio: dt.date,
    dt_fim: dt.date,
    dias_novos: int,
    segmentos_existentes: list[dict],
) -> tuple[bool, str]:
    """Valida as regras de fracionamento da Licença Certariana.

    Regras (DP):
    - Até 3 períodos.
    - Cada período >= 10 dias.
    - Se forem 3 períodos: somente 3×10 (total 30).
    - Se forem 2 períodos: nenhum < 10 (ex.: 20+10, 16+14, etc.).
    - 1 período por solicitação (este endpoint), reconhecendo os períodos anteriores.
    """
    try:
        direito_total = int(direito_total or 0)
    except Exception:
        direito_total = 0

    if dias_novos < 10:
        return False, "Para Licença Certariana, o período mínimo é de 10 dias."

    if direito_total <= 0:
        return False, "Licença Certariana indisponível (direito total = 0)."

    # Não permitir sobreposição com períodos já registrados (aprovados ou reservados)
    for seg in segmentos_existentes:
        ini = seg.get("ini")
        fim = seg.get("fim")
        if not ini or not fim:
            continue
        if not (dt_fim < ini or dt_inicio > fim):
            return False, "Este período conflita (sobrepõe) com outro período de Licença Certariana já registrado."

    seg_dias = [int(s.get("dias") or 0) for s in segmentos_existentes]
    seg_count = len(seg_dias)
    used_sum = sum(seg_dias)

    if seg_count >= 3:
        return False, "Já existem 3 períodos de Licença Certariana registrados nesta janela. Não é possível adicionar outro."

    total_after = used_sum + int(dias_novos)
    if total_after > direito_total:
        return False, f"Total de dias excede o direito da Licença Certariana ({direito_total} dias) nesta janela."

    seg_after = seg_count + 1
    remaining = direito_total - total_after

    # Mínimo por período = 10, então saldo 1-9 é impossível
    if 0 < remaining < 10:
        return False, f"Este fracionamento deixaria um saldo de {remaining} dia(s), mas o mínimo por período é 10."

    # 3 períodos: somente 3×10 (assumindo direito 30)
    if seg_after == 3:
        all10 = all(d == 10 for d in (seg_dias + [dias_novos]))
        if not (direito_total == 30 and total_after == 30 and all10):
            return False, "Para utilizar 3 períodos, a Licença Certariana deve ser fracionada em 3×10 dias (total 30)."
        return True, ""

    # Se após este lançamento ainda restar saldo e ele iria virar um 3º período, exige 3×10
    if seg_after == 2 and remaining > 0:
        all10 = all(d == 10 for d in (seg_dias + [dias_novos]))
        if not (direito_total == 30 and remaining == 10 and all10):
            return False, "Para deixar um 3º período, a Licença Certariana deve seguir 3×10 (cada período com 10 dias)."
        return True, ""

    return True, ""


# ============================================
# API: SOLICITAÇÃO DE FÉRIAS
# ============================================

