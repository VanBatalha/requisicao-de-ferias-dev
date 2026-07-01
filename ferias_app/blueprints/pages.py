from __future__ import annotations

import datetime as dt
import logging
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

    if not (is_dp_or_admin or is_gestor(gestor_email)):
        return render_template(
            "sem_permissao.html",
            active_page="ferias",
            user=user,
            gestor_email=gestor_email,
        ), 403

    # V38: a tela de solicitações deve operar exclusivamente por matrícula.
    # O parâmetro legado ?colaborador=email não deve selecionar ninguém, pois
    # e-mails podem estar duplicados ou preenchidos incorretamente.
    if request.args.get("colaborador"):
        mat_param = str(request.args.get("matricula") or request.args.get("colaborador_matricula") or "").strip().upper()
        log.warning("FERIAS_V38 parametro legado ignorado: colaborador=%s matricula=%s", request.args.get("colaborador"), mat_param)
        if mat_param:
            return redirect(url_for("ferias.ferias", matricula=mat_param))
        return redirect(url_for("ferias.ferias"))

    try:
        colaboradores_all = listar_colaboradores(only_ativos=True)
    except Exception:
        colaboradores_all = listar_colaboradores_cached()

    def _matricula_colab(c: dict) -> str:
        return str(c.get("MATRICULA") or c.get("MATRÍCULA") or c.get("matricula") or "").strip().upper()

    def _email_colab(c: dict) -> str:
        return safe_lower(c.get("EMAIL DA EMPRESA") or c.get("email") or "")

    def _nome_colab(c: dict) -> str:
        return str(c.get("NOME COMPLETO") or c.get("nome") or _email_colab(c) or _matricula_colab(c) or "").strip()

    ativos_por_matricula: dict[str, dict] = {}
    ativos_por_email: dict[str, dict] = {}
    def _mat_num(mat: str) -> int:
        import re
        m = re.search(r"(\d+)", str(mat or ""))
        return int(m.group(1)) if m else 0

    def _prefer_colab_atual(novo: dict, atual: dict | None) -> dict:
        if not atual:
            return novo
        # Se o mesmo e-mail aparece em mais de uma matrícula ATIVA, preferimos a
        # matrícula mais alta/recente. Isso corrige e-mails reaproveitados ou
        # preenchidos indevidamente sem voltar a usar e-mail como chave.
        n_mat = _matricula_colab(novo)
        a_mat = _matricula_colab(atual)
        return novo if _mat_num(n_mat) >= _mat_num(a_mat) else atual

    for c in colaboradores_all or []:
        if not isinstance(c, dict) or not is_colaborador_ativo(c):
            continue
        mat = _matricula_colab(c)
        if not mat:
            continue
        ativos_por_matricula[mat] = c
        em = _email_colab(c)
        if em:
            ativos_por_email[em] = _prefer_colab_atual(c, ativos_por_email.get(em))

    nome_por_email: dict[str, str] = {}
    matricula_por_email: dict[str, str] = {}
    for c in ativos_por_matricula.values():
        em = _email_colab(c)
        if not em:
            continue
        nome_por_email[em] = _nome_colab(c)
        matricula_por_email[em] = _matricula_colab(c)

    disponiveis: list[dict] = []
    subs: list[str] = []

    if is_dp_or_admin:
        disponiveis = list(ativos_por_matricula.values())
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
                    "Peça ao DP para preencher a coluna 'GESTOR DIRETO' (ou 'GESTOR') no cadastro."
                ),
            ), 403
        seen_mats: set[str] = set()
        for e in subs:
            c = ativos_por_email.get(safe_lower(e))
            if not c:
                continue
            mat = _matricula_colab(c)
            if mat and mat not in seen_mats:
                seen_mats.add(mat)
                disponiveis.append(c)
        own = ativos_por_email.get(gestor_email)
        if own:
            mat = _matricula_colab(own)
            if mat and mat not in seen_mats:
                disponiveis.append(own)

    opcoes = [
        {"matricula": _matricula_colab(c), "email": _email_colab(c), "nome": _nome_colab(c)}
        for c in disponiveis
        if _matricula_colab(c)
    ]
    opcoes.sort(key=lambda x: (_sort_text_pt(x.get("nome") or ""), str(x.get("matricula") or "")))

    raw_matricula = str(request.args.get("matricula") or request.args.get("colaborador_matricula") or "").strip().upper()

    # V38: nenhuma seleção operacional por e-mail. A seleção só é válida quando
    # vier uma matrícula existente na lista ativa/autorizada.
    selecionado_matricula = raw_matricula

    opcoes_por_matricula = {o["matricula"]: o for o in opcoes}
    if selecionado_matricula not in opcoes_por_matricula:
        selecionado_matricula = opcoes[0]["matricula"] if opcoes else ""

    selecionado_opcao = opcoes_por_matricula.get(selecionado_matricula) or {}
    selecionado_email = safe_lower(selecionado_opcao.get("email") or "")

    # A fonte operacional é a matrícula. O e-mail é apenas informativo.
    resumo = get_resumo_ferias(selecionado_matricula)
    dias_direito = resumo["regular"]["direito"]
    dias_usados = resumo["regular"]["usados"]
    dias_reservados = resumo["regular"]["reservados"]
    saldo = resumo["regular"]["saldo"]
    regular_periodos = resumo["regular"].get("periodos") or []
    periodo_aquisitivo_atual = resumo["regular"].get("periodo_atual")

    premium_direito = resumo["premium"]["direito"]
    premium_usados = resumo["premium"]["usados"]
    premium_reservados = resumo["premium"]["reservados"]
    premium_saldo = resumo["premium"]["saldo"]

    if is_dp_or_admin:
        solicitacoes = listar_solicitacoes_todas()
    else:
        solicitacoes = listar_solicitacoes_equipes([gestor_email] + subs)

    colaborador_nome = selecionado_opcao.get("nome") or selecionado_matricula or selecionado_email
    is_viewing_own_holidays = selecionado_email == gestor_email and not is_dp_or_admin

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

