from __future__ import annotations

from .base import bp
from ..core import *  # noqa: F401,F403

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
        return render_template("base.html", content="login")

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
        return render_template("base.html", content="login")

    gestor_email = safe_lower(user.get("email") or "")
    if not gestor_email:
        return redirect(url_for("ferias.logout"))

    role = get_user_role(gestor_email)
    is_dp_or_admin = role in ("DP", "admin")

    # Gestores podem solicitar para sua equipe; DP/Admin podem solicitar para todos (tela de Solicitações)
    if not (is_dp_or_admin or is_gestor(gestor_email)):
        return render_template(
            "sem_permissao.html",
            active_page="ferias",
            user=user,
            gestor_email=gestor_email,
        ), 403

    colaboradores_all = _listar_colaboradores_cached()

    # carrega nomes (para exibição)
    nome_por_email = {}
    for c in colaboradores_all:
        em = safe_lower(c.get("EMAIL DA EMPRESA") or "")
        if not em:
            continue
        nome_por_email[em] = c.get("NOME COMPLETO") or em

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
            if not _is_ativo(c):
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
            if e and e not in seen and e != gestor_email:
                seen.add(e)
                disponiveis.append(e)

    opcoes = [{"email": e, "nome": (nome_por_email.get(e) or e)} for e in disponiveis]

    selecionado = safe_lower(request.args.get("colaborador") or (opcoes[0]["email"] if opcoes else ""))
    if selecionado not in [o["email"] for o in opcoes]:
        selecionado = opcoes[0]["email"] if opcoes else ""

    resumo = get_resumo_ferias(selecionado)
    dias_direito = resumo["regular"]["direito"]
    dias_usados = resumo["regular"]["usados"]
    dias_reservados = resumo["regular"]["reservados"]
    saldo = resumo["regular"]["saldo"]
    
    premium_direito = resumo["premium"]["direito"]
    premium_usados = resumo["premium"]["usados"]
    premium_reservados = resumo["premium"]["reservados"]
    premium_saldo = resumo["premium"]["saldo"]
    
    # Histórico:
    # - Gestor: solicitações do gestor e de seus subordinados
    # - DP/Admin: todas as solicitações
    if is_dp_or_admin:
        solicitacoes = listar_solicitacoes_todas()
    else:
        solicitacoes = listar_solicitacoes_equipes([gestor_email] + subs)

    colaborador_nome = next((o["nome"] for o in opcoes if o["email"] == selecionado), selecionado)

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
        solicitacoes=solicitacoes,
        premium_direito=premium_direito,
        premium_usados=premium_usados,
        premium_reservados=premium_reservados,
        premium_saldo=premium_saldo,

    )

@bp.route("/painel-admin")
def painel_admin():
    user = session.get("user")
    if not user:
        return redirect(url_for("ferias.login"))
    
    email = user.get("email")
    if not tem_grupo(email, "Administrador"):
        return "Acesso negado. Você não é administrador.", 403
    
    return render_template("painel_admin.html", user=user, active_page="admin")

@bp.route("/painel-dp")
def painel_dp():
    user = session.get("user")
    if not user:
        return redirect(url_for("ferias.login"))
    
    email = user.get("email")
    if not (tem_grupo(email, "DP") or tem_grupo(email, "Administrador")):
        return "Acesso negado. Você não é do DP.", 403
    
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

