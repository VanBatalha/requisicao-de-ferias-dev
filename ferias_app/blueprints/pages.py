from __future__ import annotations

import csv
import datetime as dt
import io

from flask import Response, redirect, render_template, request, session, url_for

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
from ..services.auth_service import get_access_token
from ..services.cadastro_service import canonical_email_for
from ..services.identity_service import emails_equivalentes, normalize_email_identity
from ..utils import parse_date

SIMULAR_GESTOR_PARAM = "simular_gestor"


def _canonical_email(*identifiers: str) -> str:
    """Resolve o e-mail canônico do cadastro quando possível."""
    identifiers = tuple(x for x in identifiers if x)
    try:
        token = get_access_token()
        if token:
            return canonical_email_for(token, *identifiers)
    except Exception:
        pass
    for item in identifiers:
        norm = normalize_email_identity(item)
        if norm:
            return norm
    return ""


def _colab_email(colab: dict) -> str:
    return normalize_email_identity(
        colab.get("EMAIL DA EMPRESA")
        or colab.get("EMAIL")
        or colab.get("E-MAIL")
        or colab.get("email")
        or ""
    )


def _active_colaboradores(colaboradores_all: list[dict]) -> list[dict]:
    return [c for c in colaboradores_all if isinstance(c, dict) and is_colaborador_ativo(c) and _colab_email(c)]


def _find_matching_email(emails: list[str], target: str) -> str:
    for email in emails:
        if emails_equivalentes(email, target):
            return email
    return ""


def _nome_por_email(colaboradores_all: list[dict]) -> dict[str, str]:
    nomes: dict[str, str] = {}
    for c in colaboradores_all:
        em = _colab_email(c)
        if not em:
            continue
        nomes[em] = c.get("NOME COMPLETO") or c.get("NOME") or em
    return nomes


def _opcoes_from_emails(emails: list[str], nome_por_email: dict[str, str]) -> list[dict[str, str]]:
    seen: set[str] = set()
    opcoes: list[dict[str, str]] = []
    for email in emails:
        em = normalize_email_identity(email)
        if not em or em in seen:
            continue
        seen.add(em)
        opcoes.append({"email": em, "nome": nome_por_email.get(em) or em})
    opcoes.sort(key=lambda x: ((x.get("nome") or "").casefold(), (x.get("email") or "").casefold()))
    return opcoes


def _resolver_contexto_ferias(user: dict) -> dict:
    """Monta o contexto de permissão/visibilidade para tela e relatórios."""
    raw_email = user.get("email") or ""
    real_email = _canonical_email(raw_email, user.get("ldap_email") or "", user.get("username") or "")
    if not real_email:
        return {"ok": False, "message": "Usuário inválido."}

    real_role = get_user_role(real_email)
    real_is_admin = real_role == "admin"
    simulation_active = False
    simulated_gestor_email = ""
    effective_gestor_email = real_email

    sim_raw = normalize_email_identity(request.args.get(SIMULAR_GESTOR_PARAM) or "")
    if real_is_admin and sim_raw:
        simulated_gestor_email = _canonical_email(sim_raw)
        if simulated_gestor_email:
            simulation_active = True
            effective_gestor_email = simulated_gestor_email

    colaboradores_all = listar_colaboradores_cached()
    active_colabs = _active_colaboradores(colaboradores_all)
    active_emails = sorted({_colab_email(c) for c in active_colabs if _colab_email(c)})
    nome_por_email = _nome_por_email(colaboradores_all)
    gestores_simulaveis = _opcoes_from_emails(active_emails, nome_por_email) if real_is_admin else []

    effective_role = get_user_role(effective_gestor_email)
    # Quando ADMIN está simulando, a tela deve se comportar como a visão do gestor simulado,
    # não como DP/Admin. Isso evita mascarar problemas de vínculo gestor->colaborador.
    effective_is_dp_or_admin = (not simulation_active) and real_role in ("DP", "admin")

    subs: list[str] = []
    disponiveis: list[str] = []

    if effective_is_dp_or_admin:
        disponiveis = active_emails
    else:
        if not (simulation_active or is_gestor(effective_gestor_email)):
            return {
                "ok": False,
                "message": (
                    "Nenhum subordinado vinculado ao seu usuário. "
                    "Peça ao DP para preencher a coluna 'GESTOR DIRETO' (ou 'GESTOR') na planilha de cadastro."
                ),
                "real_email": real_email,
                "effective_gestor_email": effective_gestor_email,
                "simulation_active": simulation_active,
                "gestores_simulaveis": gestores_simulaveis,
            }

        subs = get_subordinados(effective_gestor_email)
        seen: set[str] = set()

        # Gestor pode visualizar seu próprio saldo, mas não pode abrir solicitação para si.
        self_email = _find_matching_email(active_emails, effective_gestor_email) or effective_gestor_email
        if self_email:
            seen.add(self_email)
            disponiveis.append(self_email)

        for sub in subs:
            sub_norm = normalize_email_identity(sub)
            if not sub_norm or sub_norm in seen:
                continue
            seen.add(sub_norm)
            disponiveis.append(sub_norm)

    opcoes = _opcoes_from_emails(disponiveis, nome_por_email)
    return {
        "ok": True,
        "real_email": real_email,
        "real_role": real_role,
        "real_is_admin": real_is_admin,
        "effective_gestor_email": effective_gestor_email,
        "effective_role": effective_role,
        "effective_is_dp_or_admin": effective_is_dp_or_admin,
        "simulation_active": simulation_active,
        "simulated_gestor_email": simulated_gestor_email,
        "gestores_simulaveis": gestores_simulaveis,
        "colaboradores_all": colaboradores_all,
        "nome_por_email": nome_por_email,
        "subs": subs,
        "disponiveis": [o["email"] for o in opcoes],
        "opcoes": opcoes,
    }


def _selecionar_colaborador(opcoes: list[dict[str, str]]) -> str:
    requested = normalize_email_identity(request.args.get("colaborador") or "")
    emails = [o["email"] for o in opcoes]
    if requested:
        match = _find_matching_email(emails, requested)
        if match:
            return match
    return emails[0] if emails else ""


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
            colab = (request.form.get("colaborador_email") or "").strip()
            if colab:
                return redirect(url_for("ferias.ferias", colaborador=colab))
            return redirect(url_for("ferias.ferias"))
        return resp

    user = session.get("user")
    if not user:
        return redirect(url_for("ferias.login"))

    ctx = _resolver_contexto_ferias(user)
    if not ctx.get("ok"):
        return render_template(
            "sem_permissao.html",
            active_page="ferias",
            user=user,
            gestor_email=ctx.get("effective_gestor_email") or ctx.get("real_email") or "",
            message=ctx.get("message") or "Sem permissão.",
        ), 403

    opcoes = ctx["opcoes"]
    if not opcoes:
        return render_template(
            "sem_permissao.html",
            active_page="ferias",
            user=user,
            gestor_email=ctx["effective_gestor_email"],
            message="Nenhum colaborador ativo disponível para consulta.",
        ), 403

    selecionado = _selecionar_colaborador(opcoes)
    resumo = get_resumo_ferias(selecionado)
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

    if ctx["effective_is_dp_or_admin"]:
        solicitacoes = listar_solicitacoes_todas()
    else:
        solicitacoes = listar_solicitacoes_equipes(ctx["disponiveis"])

    colaborador_nome = next((o["nome"] for o in opcoes if o["email"] == selecionado), selecionado)
    is_self_selected = emails_equivalentes(selecionado, ctx["real_email"])
    disable_solicitacoes = bool(ctx["simulation_active"] or is_self_selected)
    disable_reason = ""
    if ctx["simulation_active"]:
        disable_reason = "Você está em modo de simulação de gestor. As solicitações ficam desabilitadas para evitar lançamentos indevidos."
    elif is_self_selected:
        disable_reason = "Você pode consultar o próprio saldo, mas não pode abrir solicitação para si próprio por esta tela."

    hoje = dt.date.today()

    return render_template(
        "ferias.html",
        active_page="ferias",
        user=user,
        gestor_email=ctx["effective_gestor_email"],
        real_user_email=ctx["real_email"],
        colaborador_email=selecionado,
        colaborador_nome=colaborador_nome,
        colaboradores_opcoes=opcoes,
        nome_por_email=ctx["nome_por_email"],
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
        admin_can_simulate=ctx["real_is_admin"],
        simulation_active=ctx["simulation_active"],
        simulated_gestor_email=ctx["simulated_gestor_email"],
        gestores_simulaveis=ctx["gestores_simulaveis"],
        disable_solicitacoes=disable_solicitacoes,
        disable_reason=disable_reason,
        report_mes=hoje.month,
        report_ano=hoje.year,
        simular_gestor_param=SIMULAR_GESTOR_PARAM,
    )


@bp.route("/relatorios/solicitacoes.csv")
def relatorio_solicitacoes():
    user = session.get("user")
    if not user:
        return redirect(url_for("ferias.login"))

    ctx = _resolver_contexto_ferias(user)
    if not ctx.get("ok"):
        return (ctx.get("message") or "Sem permissão.", 403)

    allowed = ctx.get("disponiveis") or []
    if ctx["effective_is_dp_or_admin"]:
        solicitacoes = listar_solicitacoes_todas()
    else:
        solicitacoes = listar_solicitacoes_equipes(allowed)

    colaborador_filter = normalize_email_identity(request.args.get("colaborador") or "")
    if colaborador_filter and colaborador_filter.lower() not in ("todos", "all"):
        if not any(emails_equivalentes(colaborador_filter, email) for email in allowed):
            return ("Colaborador fora da sua visibilidade.", 403)
        solicitacoes = [r for r in solicitacoes if len(r) > 1 and emails_equivalentes(r[1], colaborador_filter)]

    mes_raw = (request.args.get("mes") or "").strip()
    ano_raw = (request.args.get("ano") or "").strip()
    mes = int(mes_raw) if mes_raw.isdigit() and 1 <= int(mes_raw) <= 12 else None
    ano = int(ano_raw) if ano_raw.isdigit() else None

    if mes or ano:
        filtradas = []
        for row in solicitacoes:
            inicio = parse_date(row[2]) if len(row) > 2 else None
            if not inicio:
                continue
            if mes and inicio.month != mes:
                continue
            if ano and inicio.year != ano:
                continue
            filtradas.append(row)
        solicitacoes = filtradas

    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow([
        "ID da Linha",
        "Colaborador",
        "Nome",
        "Data Início",
        "Data Fim",
        "Dias",
        "Status",
        "Solicitação",
        "Saldo Tipo",
        "Observações",
        "Gerado por",
        "Visão/gestor",
    ])

    nome_por_email = ctx.get("nome_por_email") or {}
    for row in solicitacoes:
        row_id, colab_email, inicio, fim, dias_item, status, solicitacao, saldo_tipo, obs = row
        writer.writerow([
            row_id,
            colab_email,
            nome_por_email.get(colab_email, colab_email),
            inicio,
            fim,
            dias_item,
            status,
            solicitacao,
            saldo_tipo,
            obs,
            ctx["real_email"],
            ctx["effective_gestor_email"],
        ])

    filename = "relatorio_solicitacoes"
    if colaborador_filter and colaborador_filter.lower() not in ("todos", "all"):
        filename += "_colaborador"
    if mes:
        filename += f"_{mes:02d}"
    if ano:
        filename += f"_{ano}"
    filename += ".csv"

    data = "\ufeff" + output.getvalue()
    return Response(
        data,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
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
