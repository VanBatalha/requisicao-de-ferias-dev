from __future__ import annotations

import datetime as dt
import unicodedata

from flask import redirect, render_template, request, session, url_for


def _sort_text_pt(value: str) -> str:
    value = str(value or '')
    value = unicodedata.normalize('NFD', value)
    value = ''.join(ch for ch in value if unicodedata.category(ch) != 'Mn')
    return value.casefold().strip()

from .base import bp
from .solicitacoes_api import api_solicitar_ferias
from ..core import (
    get_resumo_ferias,
    get_subordinados,
    get_user_grupos,
    get_user_role,
    is_colaborador_ativo,
    is_gestor,
    listar_colaboradores_cached,
    listar_solicitacoes_equipes,
    listar_solicitacoes_todas,
    safe_lower,
    tem_grupo,
)
from ..services.simulation_service import get_simulated_gestor, is_in_simulation

from ..logging_config import get_logger

log = get_logger(__name__)


def _empty_resumo_ferias_pages():
    return {
        "regular": {"direito": 0, "usados": 0, "reservados": 0, "saldo": 0, "periodos": [], "periodo_atual": None},
        "premium": {"direito": 0, "usados": 0, "reservados": 0, "saldo": 0, "periodos": []},
        "total_solicitacoes": 0,
    }


def _normalize_solicitacoes_para_template(rows, nome_por_email=None, matricula_por_email=None):
    """Normaliza históricos para evitar quebra de template por tuplas em formatos diferentes.

    O legado pode devolver tuplas de 8 posições para histórico individual e 9 posições
    para histórico de equipe/DP. A tela nova espera sempre um dicionário estável.
    """
    nome_por_email = nome_por_email or {}
    matricula_por_email = matricula_por_email or {}
    out = []
    for row in rows or []:
        try:
            if isinstance(row, dict):
                email = safe_lower(row.get("colaborador_email") or row.get("email") or "")
                item = {
                    "id": row.get("id") or row.get("row_id"),
                    "colaborador_email": email,
                    "colaborador_nome": row.get("colaborador_nome") or nome_por_email.get(email, email),
                    "colaborador_matricula": row.get("colaborador_matricula") or matricula_por_email.get(email, ""),
                    "inicio": row.get("inicio") or row.get("data_inicio") or "",
                    "fim": row.get("fim") or row.get("data_fim") or "",
                    "dias": row.get("dias") or row.get("dias_item") or 0,
                    "status": row.get("status") or "PENDENTE",
                    "solicitacao": row.get("solicitacao") or row.get("tipo_solicitacao") or "",
                    "saldo_tipo": (row.get("saldo_tipo") or row.get("tipo_ferias") or "REGULAR"),
                    "obs": row.get("obs") or row.get("observacoes") or "",
                }
            else:
                seq = list(row)
                # Formato equipe/DP: id, email, inicio, fim, dias, status, solicitacao, saldo_tipo, obs
                if len(seq) >= 9:
                    row_id, email, inicio, fim, dias_item, status, solicitacao, saldo_tipo, obs = seq[:9]
                # Formato individual legado: id, inicio, fim, dias, status, solicitacao, saldo_tipo, obs
                elif len(seq) == 8:
                    row_id, inicio, fim, dias_item, status, solicitacao, saldo_tipo, obs = seq
                    email = ""
                else:
                    log.warning("Histórico de solicitação ignorado por formato inesperado: %r", row)
                    continue
                email = safe_lower(email or "")
                item = {
                    "id": row_id,
                    "colaborador_email": email,
                    "colaborador_nome": nome_por_email.get(email, email),
                    "colaborador_matricula": matricula_por_email.get(email, ""),
                    "inicio": inicio or "",
                    "fim": fim or "",
                    "dias": dias_item or 0,
                    "status": status or "PENDENTE",
                    "solicitacao": solicitacao or "",
                    "saldo_tipo": saldo_tipo or "REGULAR",
                    "obs": obs or "",
                }
            item["saldo_tipo"] = str(item.get("saldo_tipo") or "REGULAR").upper()
            out.append(item)
        except Exception as exc:
            log.exception("Falha ao normalizar item do histórico de férias: %r | erro=%s", row, exc)
    return out

@bp.route("/", endpoint="home")
@bp.route("/")
def index():
    """
    Página inicial após login.

    Regras:
      - Usuário (sem grupos especiais): vai para Solicitação de Férias (/ferias)
      - DP: vai para Painel DP (aba Férias)
      - Administrador: vai para Painel Admin
    """
    user = session.get("user")
    if not user:
        return redirect(url_for("ferias.login"))

    email = (user.get("email") or "").lower()
    grupos = get_user_grupos(email)

    if "Administrador" in grupos:
        return redirect(url_for("ferias.painel_admin"))
    if "DP" in grupos:
        return redirect(url_for("ferias.painel_dp"))
    return redirect(url_for("ferias.ferias"))

@bp.route("/ferias", methods=["GET", "POST"])
def ferias():
    # Aceita POST como fallback caso o JS do formulário não execute (evita 405 Method Not Allowed)
    if request.method == "POST":
        # Reutiliza a API principal de solicitação
        resp = api_solicitar_ferias()
        # Se o navegador estiver esperando HTML, redireciona de volta ao painel com uma mensagem.
        try:
            wants_html = "text/html" in request.headers.get("Accept", "")
        except Exception:
            wants_html = False
        if wants_html:
            try:
                payload = resp.get_json(silent=True) or {}
                session["_flash_msg"] = payload.get("message") or "Solicitação processada."
            except Exception:
                session["_flash_msg"] = "Solicitação processada."
            # Mantém o colaborador selecionado na URL, se existir
            colab = (request.form.get("colaborador_email") or "").strip()
            if colab:
                return redirect(url_for("ferias.ferias", colaborador=colab))
            return redirect(url_for("ferias.ferias"))
        return resp

    user = session.get("user")
    if not user:
        return redirect(url_for("ferias.login"))

    gestor_email = safe_lower(user.get("email") or "")
    if not gestor_email:
        return redirect(url_for("ferias.logout"))

    role = get_user_role(gestor_email)
    is_dp_or_admin = role in ("DP", "admin")

    # Verifica se está em modo de simulação
    simulated_gestor = get_simulated_gestor()
    is_simulating = simulated_gestor is not None and is_dp_or_admin
    
    if is_simulating:
        # Em modo de simulação, o admin vê como se fosse o gestor simulado
        gestor_email = simulated_gestor
        is_dp_or_admin = False  # Força a comportamento de gestor
        # Marca para desabilitar botões de ação na interface
        render_mode = "simulation"
    else:
        render_mode = "normal"

    # Gestores podem solicitar para sua equipe; DP/Admin podem solicitar para todos (tela de Solicitações)
    if not (is_dp_or_admin or is_gestor(gestor_email)):
        return render_template(
            "sem_permissao.html",
            active_page="ferias",
            user=user,
            gestor_email=gestor_email,
        ), 403

    try:
        colaboradores_all = listar_colaboradores_cached() or []
    except Exception as exc:
        log.exception("FERIAS_500 passo=listar_colaboradores_cached usuario=%s erro=%s", gestor_email, exc)
        colaboradores_all = []

    # carrega nomes e matrículas (para exibição e desambiguação)
    nome_por_email = {}
    matricula_por_email = {}
    for c in colaboradores_all:
        try:
            if not isinstance(c, dict):
                continue
            em = safe_lower(c.get("EMAIL DA EMPRESA") or c.get("email") or "")
            if not em:
                continue
            nome_por_email[em] = c.get("NOME COMPLETO") or c.get("nome") or em
            matricula_por_email[em] = c.get("MATRICULA") or c.get("MATRÍCULA") or c.get("matricula") or ""
        except Exception:
            continue

    # lista de colaboradores disponíveis:
    # - Gestor: somente subordinados
    # - DP/Admin: todos ativos
    disponiveis: list[str] = []
    subs: list[str] = []

    if is_dp_or_admin:
        seen = set()
        for c in colaboradores_all:
            if not isinstance(c, dict):
                continue
            if not is_colaborador_ativo(c):
                continue
            em = safe_lower(c.get("EMAIL DA EMPRESA") or "")
            if not em or em in seen:
                continue
            seen.add(em)
            disponiveis.append(em)
        disponiveis.sort()
    else:
        subs = get_subordinados(gestor_email)
        if not subs:
            return render_template(
                "sem_permissao.html",
                active_page="ferias",
                user=user,
                gestor_email=gestor_email,
                message=(
                    "Nenhum subordinado vinculado ao seu usuário. "
                    "Peça ao DP para preencher a coluna 'GESTOR DIRETO' (ou 'GESTOR') na planilha de cadastro."
                ),
            ), 403

        seen = set()
        for e in subs:
            e = safe_lower(e)
            if e and e not in seen:
                seen.add(e)
                disponiveis.append(e)
        
        # Adiciona o próprio gestor à lista (para que possa ver suas férias)
        if gestor_email not in seen:
            seen.add(gestor_email)
            disponiveis.append(gestor_email)

    opcoes = [{"email": e, "nome": (nome_por_email.get(e) or e), "matricula": (matricula_por_email.get(e) or "")} for e in disponiveis]
    opcoes.sort(key=lambda x: (_sort_text_pt(x.get("nome") or ""), str(x.get("matricula") or ""), (x.get("email") or "").casefold()))

    selecionado = safe_lower(request.args.get("colaborador") or (opcoes[0]["email"] if opcoes else ""))
    if selecionado not in [o["email"] for o in opcoes]:
        selecionado = opcoes[0]["email"] if opcoes else ""

    try:
        resumo = get_resumo_ferias(selecionado) or _empty_resumo_ferias_pages()
    except Exception as exc:
        log.exception("FERIAS_500 passo=get_resumo_ferias selecionado=%s erro=%s", selecionado, exc)
        resumo = _empty_resumo_ferias_pages()
    regular_resumo = resumo.get("regular") or {}
    premium_resumo = resumo.get("premium") or {}
    dias_direito = regular_resumo.get("direito") or 0
    dias_usados = regular_resumo.get("usados") or 0
    dias_reservados = regular_resumo.get("reservados") or 0
    saldo = regular_resumo.get("saldo") or 0
    regular_periodos = regular_resumo.get("periodos") or []
    periodo_aquisitivo_atual = regular_resumo.get("periodo_atual")
    
    premium_direito = premium_resumo.get("direito") or 0
    premium_usados = premium_resumo.get("usados") or 0
    premium_reservados = premium_resumo.get("reservados") or 0
    premium_saldo = premium_resumo.get("saldo") or 0
    
    # Histórico:
    # - Gestor: solicitações do gestor e de seus subordinados
    # - DP/Admin: todas as solicitações
    try:
        if is_dp_or_admin:
            solicitacoes_raw = listar_solicitacoes_todas()
        else:
            solicitacoes_raw = listar_solicitacoes_equipes([gestor_email] + subs)
    except Exception as exc:
        log.exception("FERIAS_500 passo=listar_solicitacoes usuario=%s erro=%s", gestor_email, exc)
        solicitacoes_raw = []
    solicitacoes = _normalize_solicitacoes_para_template(solicitacoes_raw, nome_por_email, matricula_por_email)

    colaborador_nome = next((o["nome"] for o in opcoes if o["email"] == selecionado), selecionado)

    # Verifica se o gestor está visualizando suas próprias férias
    is_viewing_own_holidays = selecionado == gestor_email and not is_dp_or_admin

    return render_template(
        "ferias.html",
        active_page="ferias",
        user=user,
        gestor_email=gestor_email,
        colaborador_email=selecionado,
        colaborador_nome=colaborador_nome,
        colaboradores_opcoes=opcoes,
        nome_por_email=nome_por_email,
        dias_direito=dias_direito,
        dias_usados=dias_usados,
        dias_reservados=dias_reservados,
        saldo=saldo,
        regular_periodos=regular_periodos,
        periodo_aquisitivo_atual=periodo_aquisitivo_atual,
        solicitacoes=solicitacoes,
        premium_direito=premium_direito,
        premium_usados=premium_usados,
        premium_reservados=premium_reservados,
        premium_saldo=premium_saldo,
        is_simulating=is_simulating,
        simulated_gestor=simulated_gestor,
        render_mode=render_mode,
        is_viewing_own_holidays=is_viewing_own_holidays,
    )

@bp.route("/painel-admin")
def painel_admin():
    user = session.get("user")
    if not user:
        return redirect(url_for("ferias.login"))
    
    email = user.get("email")
    if not tem_grupo(email, "Administrador"):
        return redirect(url_for("ferias.ferias"))
    
    return render_template("painel_admin.html", user=user, active_page="admin")

@bp.route("/painel-dp")
def painel_dp():
    user = session.get("user")
    if not user:
        return redirect(url_for("ferias.login"))
    
    email = user.get("email")
    if not (tem_grupo(email, "DP") or tem_grupo(email, "Administrador")):
        return redirect(url_for("ferias.ferias"))
    
    hoje = dt.date.today()
    mes_atual = hoje.month
    ano_atual = hoje.year
    
    # Próximo mês
    if mes_atual == 12:
        proximo_mes = 1
        proximo_ano = ano_atual + 1
    else:
        proximo_mes = mes_atual + 1
        proximo_ano = ano_atual
    
    return render_template(
        "painel_dp.html",
        active_page="dp",
        user=user,
        mes_atual=mes_atual,
        ano_atual=ano_atual,
        proximo_mes=proximo_mes,
        proximo_ano=proximo_ano,
    )

