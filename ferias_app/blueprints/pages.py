from __future__ import annotations

import datetime as dt
import logging
import time
import unicodedata

from flask import redirect, render_template, request, session, url_for

log = logging.getLogger(__name__)


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
    listar_colaboradores,
    listar_colaboradores_cached,
    listar_solicitacoes_equipes,
    listar_solicitacoes_todas,
    safe_lower,
    tem_grupo,
)
from ..services.simulation_service import get_simulated_gestor, is_in_simulation
from ..services.postgres_compat_service import (
    postgres_enabled,
    listar_colaboradores_opcoes_ativas_postgres,
    listar_colaboradores_opcoes_ferias_postgres,
    get_resumo_ferias_por_matricula_postgres,
    listar_solicitacoes_matricula_postgres,
)
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
    """Tela de solicitação de férias.

    V42: a seleção operacional é por matrícula e a rota foi otimizada para
    carregar apenas os dados necessários do colaborador selecionado:
    - lista leve de colaboradores ativos;
    - resumo direto da tabela saldo_periodo;
    - histórico somente da matrícula selecionada.
    """
    t0 = time.perf_counter()

    # Aceita POST como fallback caso o JS do formulário não execute (evita 405 Method Not Allowed)
    if request.method == "POST":
        resp = api_solicitar_ferias()
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
            matricula = (request.form.get("colaborador_matricula") or "").strip().upper()
            if matricula:
                return redirect(url_for("ferias.ferias", matricula=matricula))
            return redirect(url_for("ferias.ferias"))
        return resp

    user = session.get("user")
    if not user:
        return redirect(url_for("ferias.login"))

    gestor_email = safe_lower(user.get("email") or "")
    if not gestor_email:
        return redirect(url_for("ferias.logout"))

    # V64: ADMIN e DP têm acesso integral à tela de Solicitações.
    # Prioriza o perfil gravado no login para evitar que uma consulta de
    # permissão momentaneamente inconsistente reduza o escopo para gestor.
    session_user_type = str(user.get("user_type") or "").strip().upper()
    if session_user_type in {"ADMIN", "ADMINISTRADOR"}:
        role = "admin"
    elif session_user_type in {"DP", "RH"}:
        role = "DP"
    else:
        role = get_user_role(gestor_email)
    is_dp_or_admin = role in ("DP", "admin")

    simulated_gestor = get_simulated_gestor()
    is_simulating = simulated_gestor is not None and is_dp_or_admin
    if is_simulating:
        gestor_email = simulated_gestor
        is_dp_or_admin = False
        render_mode = "simulation"
    else:
        render_mode = "normal"

    # Em PostgreSQL a checagem de escopo é feita por matrícula no próprio
    # carregamento leve da lista. No legado Smartsheet preservamos a checagem
    # antiga por e-mail/grupo.
    if not postgres_enabled() and not (is_dp_or_admin or is_gestor(gestor_email)):
        return render_template(
            "sem_permissao.html",
            active_page="ferias",
            user=user,
            gestor_email=gestor_email,
        ), 403

    # V41: corta definitivamente seleção por e-mail. Se alguém acessar uma URL
    # antiga com ?colaborador=email, ela não é usada para identificar pessoa.
    if request.args.get("colaborador") and not (request.args.get("matricula") or request.args.get("colaborador_matricula")):
        log.warning("FERIAS_V41 parametro legado ignorado: colaborador=%s", request.args.get("colaborador"))
        return redirect(url_for("ferias.ferias"))

    def _matricula_colab(c: dict) -> str:
        return str(c.get("MATRICULA") or c.get("MATRÍCULA") or c.get("matricula") or "").strip().upper()

    def _email_colab(c: dict) -> str:
        return safe_lower(c.get("EMAIL DA EMPRESA") or c.get("email") or "")

    def _nome_colab(c: dict) -> str:
        return str(c.get("NOME COMPLETO") or c.get("nome") or _matricula_colab(c) or "").strip()

    t_list_start = time.perf_counter()
    subs: list[str] = []

    if postgres_enabled():
        # Consulta leve e filtrada no backend. A matrícula é a chave operacional.
        # ADMIN e DP veem todos os colaboradores ativos. Somente gestores comuns
        # ficam limitados à própria equipe. Não há filtragem por e-mail nesta tela.
        escopo_role = "ADMIN" if role == "admin" else ("DP" if role == "DP" else "GESTOR")
        opcoes_base = listar_colaboradores_opcoes_ferias_postgres(gestor_email, escopo_role)
        if not is_dp_or_admin and not opcoes_base:
            return render_template(
                "sem_permissao.html",
                active_page="ferias",
                user=user,
                gestor_email=gestor_email,
                message=(
                    "Nenhum subordinado ativo vinculado à sua matrícula. "
                    "Peça ao DP/Admin para preencher gestor_direto ou gestor_superior no cadastro."
                ),
            ), 403
    else:
        colaboradores_all = listar_colaboradores(only_ativos=True)
        opcoes_base = [
            {"matricula": _matricula_colab(c), "email": _email_colab(c), "nome": _nome_colab(c), "status": c.get("STATUS") or c.get("status")}
            for c in (colaboradores_all or [])
            if isinstance(c, dict) and is_colaborador_ativo(c) and _matricula_colab(c)
        ]

        if not is_dp_or_admin:
            subs = get_subordinados(gestor_email)
            if not subs:
                return render_template(
                    "sem_permissao.html",
                    active_page="ferias",
                    user=user,
                    gestor_email=gestor_email,
                    message=(
                        "Nenhum subordinado vinculado ao seu usuário. "
                        "Peça ao DP para preencher o gestor direto no cadastro."
                    ),
                ), 403
            allowed = {safe_lower(e) for e in ([gestor_email] + list(subs or [])) if safe_lower(e)}
            allowed_locals = {e.split('@', 1)[0] for e in allowed if '@' in e}
            opcoes_base = [
                c for c in opcoes_base
                if _email_colab(c) in allowed or (_email_colab(c).split('@', 1)[0] in allowed_locals if _email_colab(c) else False)
            ]

    opcoes = []
    seen_mats: set[str] = set()
    for c in opcoes_base or []:
        mat = _matricula_colab(c)
        if not mat or mat in seen_mats:
            continue
        seen_mats.add(mat)
        opcoes.append({"matricula": mat, "email": _email_colab(c), "nome": _nome_colab(c)})
    opcoes.sort(key=lambda x: (_sort_text_pt(x.get("nome") or ""), str(x.get("matricula") or "")))
    t_list = time.perf_counter() - t_list_start

    nome_por_email: dict[str, str] = {}
    matricula_por_email: dict[str, str] = {}
    for o in opcoes:
        mat = str(o.get("matricula") or "").upper()
        nome = str(o.get("nome") or mat)
        email = safe_lower(o.get("email") or "")
        if mat:
            nome_por_email[mat] = nome
        if email:
            # Mantido somente para exibição de históricos antigos que ainda venham por e-mail.
            nome_por_email[email] = nome
            matricula_por_email[email] = mat

    raw_matricula = str(request.args.get("matricula") or request.args.get("colaborador_matricula") or "").strip().upper()
    opcoes_por_matricula = {o["matricula"]: o for o in opcoes}

    # V63: a tela inicial não pré-seleciona o primeiro colaborador. O resumo e o
    # histórico só são consultados depois de uma seleção explícita no autocomplete.
    selecionado_matricula = raw_matricula if raw_matricula in opcoes_por_matricula else ""
    selecionado_opcao = opcoes_por_matricula.get(selecionado_matricula) or {}
    selecionado_email = safe_lower(selecionado_opcao.get("email") or "")

    t_resumo_start = time.perf_counter()
    if selecionado_matricula:
        if postgres_enabled():
            resumo = get_resumo_ferias_por_matricula_postgres(selecionado_matricula)
        else:
            resumo = get_resumo_ferias(selecionado_matricula)
    else:
        resumo = {
            "regular": {"direito": 0, "usados": 0, "reservados": 0, "saldo": 0, "periodos": [], "periodo_atual": None},
            "premium": {"direito": 0, "usados": 0, "reservados": 0, "saldo": 0, "periodos": [], "periodo_atual": None},
        }
    t_resumo = time.perf_counter() - t_resumo_start

    dias_direito = resumo["regular"]["direito"]
    dias_usados = resumo["regular"].get("usados", resumo["regular"].get("usado", 0))
    dias_reservados = resumo["regular"].get("reservados", resumo["regular"].get("reservado", 0))
    saldo = resumo["regular"].get("saldo", resumo["regular"].get("disponivel", 0))
    regular_periodos = resumo["regular"].get("periodos") or []
    periodo_aquisitivo_atual = resumo["regular"].get("periodo_atual")

    premium_direito = resumo["premium"]["direito"]
    premium_usados = resumo["premium"].get("usados", resumo["premium"].get("usado", 0))
    premium_reservados = resumo["premium"].get("reservados", resumo["premium"].get("reservado", 0))
    premium_saldo = resumo["premium"].get("saldo", resumo["premium"].get("disponivel", 0))

    t_hist_start = time.perf_counter()
    if not selecionado_matricula:
        solicitacoes = []
    elif postgres_enabled():
        # Histórico do colaborador selecionado. Evita carregar todas as solicitações
        # do banco a cada troca no autocomplete.
        solicitacoes = listar_solicitacoes_matricula_postgres(selecionado_matricula)
    elif is_dp_or_admin:
        solicitacoes = listar_solicitacoes_todas()
    else:
        solicitacoes = listar_solicitacoes_equipes([gestor_email] + subs)
    t_hist = time.perf_counter() - t_hist_start

    colaborador_nome = selecionado_opcao.get("nome") or ""
    is_viewing_own_holidays = bool(selecionado_matricula) and selecionado_email == gestor_email and not is_dp_or_admin

    total = time.perf_counter() - t0
    log.info(
        "FERIAS_PERF matricula=%s opcoes=%s escopo=%s list=%.3fs resumo=%.3fs hist=%.3fs total=%.3fs",
        selecionado_matricula,
        len(opcoes),
        "ADMIN" if role == "admin" else ("DP" if role == "DP" else "GESTOR"),
        t_list,
        t_resumo,
        t_hist,
        total,
    )

    return render_template(
        "ferias.html",
        active_page="ferias",
        user=user,
        gestor_email=gestor_email,
        colaborador_email=selecionado_email,
        colaborador_matricula=selecionado_matricula,
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
        is_dp_or_admin=is_dp_or_admin,
        has_selected_colaborador=bool(selecionado_matricula),
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

