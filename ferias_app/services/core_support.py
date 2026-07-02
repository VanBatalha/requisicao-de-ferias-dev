import os
import secrets
import urllib.parse
import datetime as dt
import datetime
import time
import json
import re
import unicodedata
from pathlib import Path  # (mantido por compatibilidade; pode ser removido se não usar)

from flask import Flask, redirect, request, session, url_for, render_template, jsonify, g
import requests
import smartsheet

# ============================================
# CONFIGURAÇÕES OAUTH / SMARTSHEET
# Em produção (Render), use variáveis de ambiente.
# ============================================

CLIENT_ID = os.getenv("SMARTSHEET_CLIENT_ID", "fwf1g3363icbdvlqozd")
CLIENT_SECRET = os.getenv("SMARTSHEET_CLIENT_SECRET", "99j9a0au32butqz139k")
REDIRECT_URI = os.getenv("SMARTSHEET_REDIRECT_URI", "http://localhost:5000/callback")
SCOPES = os.getenv("SMARTSHEET_SCOPES", "READ_SHEETS WRITE_SHEETS")
AUTH_URL = "https://app.smartsheet.com/b/authorize"
TOKEN_URL = "https://api.smartsheet.com/2.0/token"
CURRENT_USER_URL = "https://api.smartsheet.com/2.0/users/me"

# IDs das folhas no Smartsheet
ID_FOLHA_CADASTRO = int(os.getenv("ID_FOLHA_CADASTRO_PRINCIPAL", os.getenv("ID_FOLHA_CADASTRO", "1745799836133252")))  # CADASTRO DE COLABORADORES
ID_FOLHA_SOLICITACOES = int(os.getenv("ID_FOLHA_SOLICITACOES", "2890766507528068"))  # solicitações de férias

"""Observação importante (Gestores/Subordinados)

A relação Gestor -> Colaboradores é obtida a partir da planilha de CADASTRO
(ID_FOLHA_CADASTRO). Para isso, a planilha deve ter uma coluna chamada:

  - GESTOR

E cada linha de colaborador deve ter o email do gestor nessa coluna.
O email do colaborador é lido da coluna:

  - EMAIL DA EMPRESA

Assim, o DP pode manter a relação diretamente no cadastro (Smartsheet),
sem dependência de arquivo local.
"""


# ============================================
# PERMISSÕES (USER TYPE) - VIA PLANILHA CADASTRO
# ============================================
#
# A planilha de cadastro (ID_FOLHA_CADASTRO = 1745799836133252) possui a coluna:
#   - USER TYPE
#
# Valores esperados (case-insensitive):
#   - ADMIN  -> Administrador
#   - DP     -> DP
#   - USER   -> Usuário padrão
#
# As permissões do sistema passam a ser calculadas **exclusivamente** por esta coluna.
#
# Observação: o bloco GRUPOS_DB abaixo é apenas legado (mantido comentado para referência)
# e NÃO é utilizado para permissões.

GRUPOS_DB = {}  # legado (não usado)

# ============================================
# FUNÇÕES AUXILIARES
# ============================================

_SHEET_CACHE = {}  # cache simples em memória para reduzir latência do Smartsheet
_SHEET_CACHE_TTL_SECONDS = int(os.getenv("SHEET_CACHE_TTL_SECONDS", "20"))

# USER TYPE: refresh rápido para refletir mudanças de permissão no Smartsheet
USER_TYPE_SOFT_REFRESH_COOLDOWN = int(os.getenv("USER_TYPE_SOFT_REFRESH_COOLDOWN", "5"))
USER_TYPE_SOFT_REFRESH_LAST = 0.0

# ============================================
# CONFIGURAÇÕES DE RUNTIME / REGRAS DE PERÍODO
# ============================================
# As regras e persistência dessas configurações foram movidas para:
# - ferias_app/services/runtime_settings_service.py
# - ferias_app/rules.py

def _invalidate_sheet_cache(sheet_id=None):
    from .sheet_helpers_service import invalidate_sheet_cache
    return invalidate_sheet_cache(sheet_id)


def get_smartsheet_client(force_user_token: bool = False):
    from .sheet_helpers_service import get_smartsheet_client as _svc
    return _svc(force_user_token=force_user_token)


def _get_smartsheet_token() -> str | None:
    from .sheet_helpers_service import get_smartsheet_token
    return get_smartsheet_token()


def add_rows_rest(sheet_id: int, rows_to_add: list, *, timeout: int = 25) -> list[int]:
    from .sheet_helpers_service import add_rows_rest as _svc
    return _svc(sheet_id, rows_to_add, timeout=timeout)


def get_col_map(sheet):
    from .sheet_helpers_service import get_col_map as _svc
    return _svc(sheet)


def ensure_primary_cell(sheet, row, value):
    from .sheet_helpers_service import ensure_primary_cell as _svc
    return _svc(sheet, row, value)


def _get_sheet_solicitacoes(client=None, *, force_refresh: bool = False):
    """Cache por request + cache em memória (TTL) do sheet de solicitações.

    Isso reduz bastante a latência ao navegar entre telas (Smartsheet é o gargalo).
    """
    if client is None:
        client = get_smartsheet_client()
    if not client:
        return None

    if force_refresh:
        sheet = client.Sheets.get_sheet(ID_FOLHA_SOLICITACOES)
        now = time.time()
        _SHEET_CACHE[ID_FOLHA_SOLICITACOES] = {"ts": now, "sheet": sheet}
        try:
            g._sheet_solicitacoes = sheet
        except Exception:
            pass
        return sheet


    # 1) cache por request
    try:
        cached = getattr(g, "_sheet_solicitacoes", None)
        if cached is not None:
            return cached
    except Exception:
        pass

    # 2) cache em memória (TTL)
    now = time.time()
    cached_entry = _SHEET_CACHE.get(ID_FOLHA_SOLICITACOES)
    if cached_entry and (now - cached_entry.get("ts", 0) <= _SHEET_CACHE_TTL_SECONDS):
        sheet = cached_entry.get("sheet")
    else:
        sheet = client.Sheets.get_sheet(ID_FOLHA_SOLICITACOES)
        _SHEET_CACHE[ID_FOLHA_SOLICITACOES] = {"ts": now, "sheet": sheet}

    try:
        g._sheet_solicitacoes = sheet
    except Exception:
        pass
    return sheet

def _get_sheet_cadastro(client):
    """Cache por request + cache em memória (TTL) do sheet de cadastro."""
    if not client:
        return None

    # 1) cache por request
    try:
        cached = getattr(g, "_sheet_cadastro", None)
        if cached is not None:
            return cached
    except Exception:
        pass

    # 2) cache em memória (TTL)
    now = time.time()
    cached_entry = _SHEET_CACHE.get(ID_FOLHA_CADASTRO)
    if cached_entry and (now - cached_entry.get("ts", 0) <= _SHEET_CACHE_TTL_SECONDS):
        sheet = cached_entry.get("sheet")
    else:
        sheet = client.Sheets.get_sheet(ID_FOLHA_CADASTRO)
        _SHEET_CACHE[ID_FOLHA_CADASTRO] = {"ts": now, "sheet": sheet}

    try:
        g._sheet_cadastro = sheet
    except Exception:
        pass
    return sheet

def calcular_dias_ferias(data_admissao_str):
    """Calcula dias de ferias em tempo real baseado na data de admissao"""
    if not data_admissao_str:
        return 0
    try:
        s = str(data_admissao_str).strip()[:10]
        data_admissao = dt.datetime.strptime(s, "%Y-%m-%d").date()
        hoje = dt.date.today()
        
        if data_admissao > hoje:
            return 0
        
        dias_trabalhados = (hoje - data_admissao).days
        anos = dias_trabalhados / 365.25
        
        # 30 dias por ano trabalhado
        dias = int(anos * 30)
        return max(0, dias)
    except Exception as e:
        print(f"ERRO em calcular_dias_ferias: {e}")
        return 0

def calcular_licenca_premium(data_admissao_str):
    """Calcula dias de licenca premium (5 anos + 30 dias a cada 2 anos)"""
    if not data_admissao_str:
        return 0
    try:
        s = str(data_admissao_str).strip()[:10]
        data_admissao = dt.datetime.strptime(s, "%Y-%m-%d").date()
        hoje = dt.date.today()
        
        if data_admissao > hoje:
            return 0
        
        dias_trabalhados = (hoje - data_admissao).days
        anos = dias_trabalhados / 365.25
        
        # Premium comeca aos 5 anos
        if anos < 5:
            return 0
        
        # 30 dias aos 5 anos
        dias_premium = 30
        
        # +30 dias a cada 2 anos apos 5 anos
        anos_apos_5 = anos - 5
        ciclos_2_anos = int(anos_apos_5 / 2)
        dias_premium += ciclos_2_anos * 30
        
        return dias_premium
    except Exception as e:
        print(f"ERRO em calcular_licenca_premium: {e}")
        return 0

def safe_lower(value):
    """Converte para lowercase com segurança contra None"""
    if value is None:
        return ""
    return str(value).strip().lower()

def formatar_data_br(data_str):
    """Converte data ISO para DD/MM/YYYY"""
    if not data_str:
        return ""
    try:
        s = str(data_str).strip()[:10]
        data_obj = dt.datetime.strptime(s, "%Y-%m-%d")
        return data_obj.strftime("%d/%m/%Y")
    except Exception:
        return str(data_str)

def parse_data(data_value):
    """Converte vários formatos comuns para dt.date (ou None).

    Suporta:
      - dt.date / dt.datetime
      - ISO: YYYY-MM-DD (e variantes com hora, ex.: YYYY-MM-DDTHH:MM:SSZ)
      - BR: DD/MM/YYYY e DD-MM-YYYY
      - (fallback) US: MM/DD/YYYY
    """
    if not data_value:
        return None
    try:
        if isinstance(data_value, dt.date) and not isinstance(data_value, dt.datetime):
            return data_value
        if isinstance(data_value, dt.datetime):
            return data_value.date()
    except Exception:
        pass

    s = str(data_value).strip()
    if not s:
        return None

    s10 = s[:10]

    # ISO
    try:
        return dt.datetime.strptime(s10, "%Y-%m-%d").date()
    except Exception:
        pass

    # BR
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d-%m-%y"):
        try:
            return dt.datetime.strptime(s10, fmt).date()
        except Exception:
            pass

    # US (fallback)
    for fmt in ("%m/%d/%Y", "%m-%d-%Y"):
        try:
            return dt.datetime.strptime(s10, fmt).date()
        except Exception:
            pass

    return None

# ============================================
# PERMISSÕES: USER TYPE (CADASTRO)
# ============================================

def _canon_user_type(value) -> str:
    """Normaliza o valor da coluna USER TYPE para: ADMIN | DP | USER.

    Observação: por compatibilidade, aceita sinônimos usados internamente (ex.: RH -> DP).
    """
    s = str(value or "").strip()
    if not s:
        return "USER"

    n = _norm_title(s)

    # ADMIN
    if n in ("admin", "administrador", "administrator", "adm"):
        return "ADMIN"

    # DP (sinônimos: RH)
    if n in (
        "dp",
        "departamento pessoal",
        "pessoal",
        "rh",
        "recursos humanos",
        "human resources",
        "people",
        "people ops",
        "people operations",
    ):
        return "DP"

    # USER
    return "USER"


def _get_user_type_map_cached() -> dict:
    """Mapeia email -> USER TYPE lendo a planilha de cadastro.

    Cache por request (g) para evitar varrer a planilha várias vezes.
    """
    try:
        cached = getattr(g, "_user_type_map", None)
        if cached is not None:
            return cached
    except Exception:
        pass

    colaboradores = _listar_colaboradores_cached()
    out = {}

    for c in colaboradores:
        if not isinstance(c, dict):
            continue
        email = safe_lower(c.get("EMAIL DA EMPRESA") or c.get("EMAIL") or "")
        if not email:
            continue

        # procura a coluna USER TYPE de forma tolerante a variações
        user_type_val = None
        for k, v in c.items():
            if _norm_title(k) in (
                "user type",
                "user_type",
                "usertype",
                "tipo usuario",
                "tipo de usuario",
                "tipo de usuário",
                "perfil",
            ):
                user_type_val = v
                break

        out[email] = _canon_user_type(user_type_val)

    try:
        g._user_type_map = out
    except Exception:
        pass

    return out


def get_user_type(email: str, force_refresh: bool = False, _skip_soft_refresh: bool = False) -> str:
    """Retorna o USER TYPE do usuário (ADMIN | DP | USER).

    - force_refresh=True: ignora caches e busca novamente no Smartsheet.
    - soft refresh: se o usuário atual foi promovido (ex.: USER -> DP) e ainda aparece USER,
      o sistema tenta atualizar automaticamente (com cooldown) para refletir mudanças rapidamente.
    """
    em = safe_lower(email)
    if not em:
        return "USER"

    if force_refresh:
        # força recarregar cadastro
        _invalidate_sheet_cache(ID_FOLHA_CADASTRO)
        try:
            if hasattr(g, "_sheet_cadastro"):
                delattr(g, "_sheet_cadastro")
            if hasattr(g, "_cadastro_colaboradores"):
                delattr(g, "_cadastro_colaboradores")
            if hasattr(g, "_colaboradores_list_cache"):
                delattr(g, "_colaboradores_list_cache")
            if hasattr(g, "_user_type_map"):
                delattr(g, "_user_type_map")
        except Exception:
            pass

    mp = _get_user_type_map_cached()
    ut = mp.get(em, "USER")

    # Soft refresh (somente para o usuário logado atual) quando ainda aparece USER.
    if (not force_refresh) and (not _skip_soft_refresh) and ut == "USER":
        try:
            current_email = safe_lower((session.get("user") or {}).get("email") or "")
        except Exception:
            current_email = ""

        if current_email and current_email == em:
            global USER_TYPE_SOFT_REFRESH_LAST
            now = time.time()
            if (now - float(USER_TYPE_SOFT_REFRESH_LAST or 0.0)) >= float(USER_TYPE_SOFT_REFRESH_COOLDOWN):
                USER_TYPE_SOFT_REFRESH_LAST = now
                # tenta forçar refresh uma vez
                ut2 = get_user_type(em, force_refresh=True, _skip_soft_refresh=True)
                if ut2:
                    ut = ut2

    return ut


def get_user_grupos(email):
    """Retorna lista de grupos compatível com o legado do sistema.

    Mapeamento:
      - ADMIN -> ["Administrador"]
      - DP    -> ["DP"]
      - USER  -> ["USER"]
    """
    ut = get_user_type(email)
    if ut == "ADMIN":
        return ["Administrador"]
    if ut == "DP":
        return ["DP"]
    return ["USER"]


def tem_grupo(email, grupo):
    """Verifica se usuário pertence ao grupo.

    Aceita tanto nomes legados ("Administrador", "DP") quanto valores do USER TYPE ("ADMIN", "DP", "USER").
    """
    if not grupo:
        return False

    g_in = str(grupo).strip()
    g_norm = _norm_title(g_in)

    if g_norm in ("admin", "administrador", "administrator", "adm"):
        g_check = "Administrador"
    elif g_norm in ("dp", "departamento pessoal", "pessoal"):
        g_check = "DP"
    elif g_norm in ("user", "usuario", "usuário"):
        g_check = "USER"
    else:
        g_check = g_in

    return g_check in (get_user_grupos(email) or [])


def get_user_role(email: str) -> str:
    """Retorna o papel do usuário com base no USER TYPE e na relação gestor."""
    ut = get_user_type(email)
    if ut == "ADMIN":
        return "admin"
    if ut == "DP":
        return "DP"
    if is_gestor(email):
        return "gestor"
    return "user"


def _is_ativo(colab: dict) -> bool:
    """Define se colaborador está ativo com base em colunas de status/situação.

    Aceita títulos como Status, STATUS, Situação, Situacao etc.
    Se o valor existir, somente "ativo" é considerado ativo.
    Se nenhuma coluna de status/situação existir, mantém compatibilidade e
    considera ativo.
    """
    if not isinstance(colab, dict):
        return False

    val = None
    for k, v in colab.items():
        nk = _norm_title(k)
        if nk in {"status", "situacao", "situação"}:
            val = v
            break

    if val is None:
        # Se a planilha não tiver coluna de status/situação, considera ativo
        return True

    norm = str(val).strip().lower()
    return norm == "ativo"



# ============================================
# RELAÇÃO GESTOR -> SUBORDINADOS (via planilha CADASTRO)
# ============================================

def _norm_email(email: str) -> str:
    from .normalization_service import norm_email
    return norm_email(email)


def _norm_title(s: str) -> str:
    from .normalization_service import norm_title
    return norm_title(s)


def _cols_norm_map(cols: dict) -> dict:
    from .normalization_service import cols_norm_map
    return cols_norm_map(cols)


def _col_id(cols_norm: dict, *candidates: str):
    from .normalization_service import col_id
    return col_id(cols_norm, *candidates)


def _norm_solicitacao(s: str) -> str:
    from .normalization_service import norm_solicitacao
    return norm_solicitacao(s)


def _norm(s: str | None) -> str:
    from .normalization_service import norm
    return norm(s)


def _is_ajuste(solic: str) -> bool:
    from .normalization_service import is_ajuste
    return is_ajuste(solic)


def _infer_saldo_tipo(obs: str, explicit: str = "") -> str:
    from .normalization_service import infer_saldo_tipo
    return infer_saldo_tipo(obs, explicit=explicit)


def _add_years(d: dt.date, years: int) -> dt.date:
    """Soma anos em uma data, ajustando 29/02 para 28/02 quando necessário."""
    if not d:
        return d
    try:
        return d.replace(year=d.year + years)
    except ValueError:
        # ex.: 29/02 em ano não bissexto
        return d.replace(month=2, day=28, year=d.year + years)


def _janela_licenca_certariana(admissao: dt.date, hoje: dt.date | None = None):
    """Calcula a janela vigente da Licença Certariana.

    Regras:
      - Conquista 30 dias ao completar 5 anos de empresa
      - Nova conquista de 30 dias a cada 5 anos (10, 15, 20, ...)
      - NÃO cumulativa (não soma ciclos)
      - Uso permitido somente dentro da janela de 2 anos após a conquista vigente
      - Se não utilizar dentro da janela, o direito vence (não acumula para o próximo ciclo)

    Retorna: (dias_base, inicio_janela, fim_janela_exclusivo)
    """
    if not admissao:
        return 0, None, None

    hoje = hoje or dt.date.today()

    # anos completos de empresa
    years = hoje.year - admissao.year
    if (hoje.month, hoje.day) < (admissao.month, admissao.day):
        years -= 1

    # ainda não conquistou
    if years < 5:
        return 0, None, None

    # última conquista em múltiplos de 5: 5, 10, 15, ...
    anos_da_conquista = (years // 5) * 5
    if anos_da_conquista < 5:
        return 0, None, None

    inicio = _add_years(admissao, anos_da_conquista)
    fim_excl = _add_years(inicio, 2)  # janela de 2 anos

    # fora da janela vigente -> sem direito base (pode haver saldo apenas por ajustes)
    if hoje >= fim_excl:
        return 0, None, None

    return 30, inicio, fim_excl
def _calcular_premium_por_tempo(admissao: dt.date, hoje: dt.date | None = None) -> int:
    """Compat: retorna o direito base vigente da Licença Certariana (não cumulativa)."""
    dias, _, _ = _janela_licenca_certariana(admissao, hoje=hoje)
    return int(dias or 0)

def _col_id_by_name(sheet, *candidates: str) -> int | None:
    """Encontra coluna por título (case-insensitive, sem acentos)."""
    if not sheet or not getattr(sheet, "columns", None):
        return None
    cand = {_norm_title(c) for c in candidates if c}
    for col in sheet.columns:
        if _norm_title(col.title) in cand:
            return col.id
    return None


# ============================================
# Helpers: datas / status / férias
# ============================================

def _parse_date_value(v):
    """Converte value do Smartsheet (string/date/datetime) para dt.date (ou None)."""
    # Reaproveita o parser mais robusto (ISO + BR + datetime)
    return parse_data(v)

def _add_months(d: dt.date, months: int) -> dt.date:
    """Soma meses em uma data, preservando o dia quando possível."""
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    # último dia do mês alvo
    import calendar
    last_day = calendar.monthrange(y, m)[1]
    day = min(d.day, last_day)
    return dt.date(y, m, day)

from .normalization_service import STATUS_APROVADA, STATUS_CANON, STATUS_RESERVA


def _norm_status(s: str) -> str:
    from .normalization_service import norm_status
    return norm_status(s)


def _canonical_status(s: str) -> str:
    from .normalization_service import canonical_status
    return canonical_status(s)
def _listar_segmentos_premium(
    email: str,
    win_start: dt.date,
    win_end: dt.date,
    exclude_row_id: int | None = None,
    include_statuses: set[str] | None = None,
):
    """Lista segmentos (dias) já lançados/pendentes de LICENÇA CERTARIANA (PREMIUM) dentro da janela atual."""
    sheet = _get_sheet_solicitacoes(force_refresh=False)

    # Usa o helper local (legacy) para resolver IDs de colunas por nome.
    col_email = _col_id_by_name(sheet, "COLABORADOR", "EMAIL", "EMAIL DO COLABORADOR", "EMAIL DA EMPRESA")
    col_saldo = _col_id_by_name(sheet, "SALDO TIPO", "SALDO", "TIPO SALDO")
    col_dias = _col_id_by_name(sheet, "DIAS", "DIAS (GOZO)", "DIAS GOZO")
    col_status = _col_id_by_name(sheet, "STATUS")
    col_ini = _col_id_by_name(sheet, "DATA INICIO", "DATA INÍCIO", "INICIO", "INÍCIO")
    col_sol = _col_id_by_name(sheet, "SOLICITAÇÃO", "SOLICITACAO", "TIPO", "TIPO SOLICITACAO")

    target = _norm_email(email)
    out = []

    for row in getattr(sheet, "rows", []) or []:
        if exclude_row_id and getattr(row, 'id', None) == exclude_row_id:
            continue
        em = _norm_email(_cell_value(row, col_email))
        if not em or em != target:
            continue

        saldo = str(_cell_value(row, col_saldo) or "")
        saldo_n = _norm(saldo)
        # Aceita variações históricas do campo SALDO TIPO
        if not (saldo_n == "premium" or "certar" in saldo_n):
            continue
        sol = str(_cell_value(row, col_sol) or "")
        # No DEV, a Licença Certariana é identificada principalmente pelo SALDO TIPO = PREMIUM.
        # A coluna "SOLICITAÇÃO" costuma vir como "Gozo".
        # Mantemos apenas o bloqueio para linhas de ajuste.
        if "ajuste" in _norm(sol):
            continue

        st = _canonical_status(str(_cell_value(row, col_status) or ""))
        stn = _norm_status(st)

        # Se o chamador especificar quais status considerar, respeita.
        # Caso contrário, mantém o padrão: aprovadas + reservas.
        if include_statuses is not None:
            if stn not in include_statuses:
                continue
        else:
            if stn not in STATUS_APROVADA and stn not in STATUS_RESERVA:
                continue

        dt_ini = _parse_date_value(_cell_value(row, col_ini))
        if not dt_ini:
            continue
        d = dt_ini.date()
        if d < win_start or d > win_end:
            continue

        try:
            dias = float(str(_cell_value(row, col_dias) or "").replace(",", "."))
        except Exception:
            dias = 0
        if dias:
            out.append(int(round(dias)))
    return out


def _listar_periodos_premium(
    email: str,
    win_start: dt.date,
    win_end: dt.date,
    exclude_row_id: int | None = None,
    include_statuses: set[str] | None = None,
    *,
    force_refresh: bool = False,
):
    """Lista períodos (ini/fim/dias) já lançados/pendentes de Licença Certariana (PREMIUM) na janela.
    Retorna lista de dicts: {ini: date, fim: date, dias: int, row_id: int|None, status: str, solicitacao: str}
    """
    sheet = _get_sheet_solicitacoes(force_refresh=False)
    
    col_email = _col_id_by_name(sheet, "COLABORADOR", "EMAIL", "EMAIL DO COLABORADOR", "EMAIL DA EMPRESA")
    col_saldo = _col_id_by_name(sheet, "SALDO TIPO", "SALDO", "TIPO SALDO")
    col_dias = _col_id_by_name(sheet, "DIAS", "DIAS (GOZO)", "DIAS GOZO")
    col_status = _col_id_by_name(sheet, "STATUS")
    col_ini = _col_id_by_name(sheet, "DATA INICIO", "DATA INÍCIO", "INICIO", "INÍCIO")
    col_fim = _col_id_by_name(sheet, "DATA FIM", "DATA FINAL", "FIM")
    col_sol = _col_id_by_name(sheet, "SOLICITAÇÃO", "SOLICITACAO", "TIPO", "TIPO SOLICITACAO")

    target = _norm_email(email)
    out: list[dict] = []

    for row in getattr(sheet, "rows", []) or []:
        if exclude_row_id and getattr(row, "id", None) == exclude_row_id:
            continue

        em = _norm_email(_cell_value(row, col_email))
        if not em or em != target:
            continue

        saldo = str(_cell_value(row, col_saldo) or "")
        saldo_n = _norm(saldo)
        if not (saldo_n == "premium" or "certar" in saldo_n):
            continue

        sol = str(_cell_value(row, col_sol) or "")
        if "ajuste" in _norm(sol):
            continue

        st = _canonical_status(str(_cell_value(row, col_status) or ""))
        stn = _norm_status(st)

        if include_statuses is not None:
            if stn not in include_statuses:
                continue
        else:
            if stn not in STATUS_APROVADA and stn not in STATUS_RESERVA:
                continue

        dt_ini = _parse_date_value(_cell_value(row, col_ini))
        if not dt_ini:
            continue
        ini_d = dt_ini.date()
        if win_start and ini_d < win_start:
            continue
        if win_end and ini_d > win_end:
            continue

        # dias
        try:
            dias = float(str(_cell_value(row, col_dias) or "").replace(",", "."))
        except Exception:
            dias = 0.0
        dias_i = int(round(dias)) if dias else 0

        # fim
        dt_fim = _parse_date_value(_cell_value(row, col_fim))
        if dt_fim:
            fim_d = dt_fim.date()
        elif dias_i > 0:
            fim_d = ini_d + dt.timedelta(days=dias_i - 1)
        else:
            fim_d = ini_d

        out.append(
            {
                "ini": ini_d,
                "fim": fim_d,
                "dias": dias_i,
                "row_id": getattr(row, "id", None),
                "status": st,
                "solicitacao": sol,
            }
        )

    out.sort(key=lambda x: x["ini"])
    return out

def _validar_fracionamento_certariana(email: str, dias_solicitados: float, dt_inicio: datetime.datetime | None = None):
    """
    Regras Licença Certariana (PREMIUM):
    - Até 3 períodos dentro da janela (30 dias).
    - Cada período >= 10 dias.
    - Se 3 períodos, obrigatoriamente 3x10.
    - Não pode sobrar saldo < 10 (senão for 0).
    """
    # valida mínimo do novo período
    try:
        dias = float(dias_solicitados)
    except Exception:
        dias = 0.0
    if dias < 10:
        raise ValueError("Na Licença Certariana, cada período deve ter no mínimo 10 dias.")

    # calcula janela premium
    adm = _colaborador_admissao(email)
    if not adm:
        # se não achar admissão, aplica regra só pelo saldo (mais seguro)
        win_start = datetime.date.min
        win_end = datetime.date.max
    else:
        # _janela_licenca_certariana retorna (dias_base, win_start, win_end)
        _, win_start, win_end = _janela_licenca_certariana(adm)

    # lista segmentos existentes
    existentes = _listar_segmentos_premium(email, win_start, win_end)
    total_exist = sum(existentes)
    periodos_exist = len(existentes)

    total = total_exist + int(round(dias))
    if total > 30:
        raise ValueError(f"Licença Certariana excede 30 dias na janela atual (tentativa: {total} dias).")

    periodos = periodos_exist + 1
    if periodos > 3:
        raise ValueError("Licença Certariana permite no máximo 3 períodos na janela atual.")

    # regra de saldo restante (se não for 0, não pode ser <10)
    restante = 30 - total
    if restante != 0 and restante < 10:
        raise ValueError("O saldo restante da Licença Certariana não pode ficar menor que 10 dias (ou deve zerar).")

    # regra específica de 3 períodos: 3x10
    if periodos == 3:
        todos = existentes + [int(round(dias))]
        if total != 30 or any(x != 10 for x in todos):
            raise ValueError("Se a Licença Certariana for dividida em 3 períodos, deve ser obrigatoriamente 3×10 (total 30).")

    return True


def _cell_value(row, col_id):
    """Retorna o valor da célula da linha para a coluna (ou None)."""
    try:
        if not col_id or int(col_id) <= 0:
            return None
        cid = int(col_id)
        return next((c.value for c in row.cells if c.column_id == cid), None)
    except Exception:
        return None

def _colaborador_por_email(email: str):
    """Localiza colaborador no cadastro de forma tolerante.

    O login LDAP/Smartsheet pode retornar o mesmo usuário com domínio diferente.
    Para cálculos de saldo e admissão, tentamos primeiro o match exato e, se
    necessário, comparamos também a parte antes do @.
    """
    email = safe_lower(email)
    if not email:
        return None

    wanted_local = email.split("@", 1)[0].strip() if "@" in email else email
    local_matches = []

    for c in _listar_colaboradores_cached():
        if not isinstance(c, dict):
            continue

        # Busca a coluna de email por título normalizado para aceitar variações.
        row_email = ""
        for k, v in c.items():
            if _norm_title(k) in {"email da empresa", "email", "e mail", "e mail da empresa"}:
                row_email = safe_lower(v)
                break

        if not row_email:
            row_email = safe_lower(c.get("EMAIL DA EMPRESA"))
        if not row_email:
            continue

        if row_email == email:
            return c

        row_local = row_email.split("@", 1)[0].strip() if "@" in row_email else row_email
        if wanted_local and row_local == wanted_local:
            local_matches.append(c)

    if len(local_matches) == 1:
        return local_matches[0]
    if len(local_matches) > 1:
        # Se houver mais de um domínio, prioriza uma linha ativa quando possível.
        for c in local_matches:
            if _is_ativo(c):
                return c
        return local_matches[0]
    return None


def _value_by_normalized_key(row: dict, *candidate_titles: str):
    """Retorna valor aceitando títulos com/sem acento, hífen ou variações."""
    if not isinstance(row, dict):
        return None
    wanted = {_norm_title(x) for x in candidate_titles if x}
    for k, v in row.items():
        nk = _norm_title(k)
        if nk in wanted:
            return v
    return None


def _colaborador_regime(email: str) -> str:
    c = _colaborador_por_email(email) or {}
    regime = _value_by_normalized_key(
        c,
        "REGIME DE CONTRATAÇÃO",
        "REGIME DE CONTRATACAO",
        "REGIME",
    )
    return str(regime or "").strip()


def _colaborador_admissao(email: str):
    c = _colaborador_por_email(email) or {}
    if not c:
        return None

    # Primeiro tenta nomes conhecidos/normalizados.
    value = _value_by_normalized_key(
        c,
        "DATA DE ADMISSÃO",
        "DATA DE ADMISSAO",
        "DATA ADMISSÃO",
        "DATA ADMISSAO",
        "ADMISSÃO",
        "ADMISSAO",
        "DATA ADMISSÃO COLABORADOR",
        "DATA ADMISSAO COLABORADOR",
    )
    parsed = _parse_date_value(value)
    if parsed:
        return parsed

    # Fallback: qualquer coluna cujo título contenha 'admiss'.
    for k, v in c.items():
        if v and "admiss" in _norm_title(k):
            parsed = _parse_date_value(v)
            if parsed:
                return parsed
    return None

def calcular_dias_ferias_real_time(admissao: dt.date, hoje=None) -> int:
    """Legado: mantido apenas para compatibilidade histórica."""
    if not admissao:
        return 0
    hoje = hoje or dt.date.today()
    if hoje < admissao:
        return 0
    dias_trabalhados = (hoje - admissao).days
    return max(0, int((dias_trabalhados / 365.0) * 30.0))


def _completed_aquisitive_periods(admissao: dt.date | None, hoje: dt.date | None = None) -> int:
    """Quantidade de períodos aquisitivos completos de 12 meses."""
    if not admissao:
        return 0
    hoje = hoje or dt.date.today()
    if hoje < admissao:
        return 0
    count = 0
    while _add_months(admissao, (count + 1) * 12) <= hoje:
        count += 1
    return count


def _periodo_bounds(admissao: dt.date, numero_periodo: int) -> tuple[dt.date, dt.date]:
    ini = _add_months(admissao, (numero_periodo - 1) * 12)
    fim_exclusivo = _add_months(admissao, numero_periodo * 12)
    fim = fim_exclusivo - dt.timedelta(days=1)
    return ini, fim


def _current_partial_period(admissao: dt.date | None, hoje: dt.date | None = None):
    if not admissao:
        return None
    hoje = hoje or dt.date.today()
    completos = _completed_aquisitive_periods(admissao, hoje)
    ini = _add_months(admissao, completos * 12)
    prox = _add_months(admissao, (completos + 1) * 12)
    if hoje < ini:
        return None
    return {
        "numero": completos + 1,
        "inicio": ini,
        "fim": prox - dt.timedelta(days=1),
        "completo": False,
    }


def _allocate_period_balance(total_direito: int, total_usados: int, total_reservados: int, admissao: dt.date | None, hoje: dt.date | None = None):
    """Monta o saldo disponível por período aquisitivo para a fase de transição.

    Regra temporária até a base histórica ficar totalmente saneada:
    - considera apenas períodos aquisitivos COMPLETOS como fonte de saldo;
    - distribui o saldo disponível atual (direito - usados - reservados) dos
      períodos completos mais recentes para trás;
    - mantém o consumo FIFO: ao solicitar, o sistema usa primeiro o período mais
      antigo entre os que ainda possuem saldo.

    Exemplo:
    - colaborador com 7 períodos completos e saldo disponível de 45 dias;
    - detalhamento gerado: P6=15 e P7=30;
    - solicitação de 21 dias: consome P6:15 | P7:6.
    """
    hoje = hoje or dt.date.today()
    total_direito = max(0, int(total_direito or 0))
    total_usados = max(0, int(total_usados or 0))
    total_reservados = max(0, int(total_reservados or 0))
    saldo_disponivel = max(0, total_direito - total_usados - total_reservados)

    if not admissao or saldo_disponivel <= 0:
        return []

    completos = _completed_aquisitive_periods(admissao, hoje)
    if completos <= 0:
        return []

    qtd_periodos = max(1, (saldo_disponivel + 29) // 30)
    ultimo_num = completos
    primeiro_num = max(1, ultimo_num - qtd_periodos + 1)

    numeros = list(range(primeiro_num, ultimo_num + 1))
    saldos_map = {n: 0 for n in numeros}
    restante = saldo_disponivel

    # Preenche dos períodos completos mais recentes para trás, deixando eventual
    # saldo parcial no período mais antigo dentre os que ainda têm saldo.
    for n in reversed(numeros):
        if restante <= 0:
            break
        alocar = min(30, restante)
        saldos_map[n] = alocar
        restante -= alocar

    periodos = []
    for n in numeros:
        ini, fim = _periodo_bounds(admissao, n)
        saldo = int(saldos_map.get(n, 0))
        if saldo <= 0:
            continue
        periodos.append({
            "numero": n,
            "inicio": ini,
            "fim": fim,
            "direito": saldo,
            "usados": 0,
            "reservados": 0,
            "saldo": saldo,
            "completo": True,
            "atual": False,
            "origem_transitoria": True,
        })

    return periodos


def _serialize_periodo_aquisitivo_alloc(alloc: list[dict]) -> str:
    """Serializa a distribuição por período para gravar no Smartsheet."""
    parts = []
    for item in alloc or []:
        n = int(item.get("numero") or 0)
        dias = int(item.get("dias") or item.get("consumidos") or 0)
        if n > 0 and dias > 0:
            parts.append(f"P{n}:{dias}")
    return " | ".join(parts)


def distribuir_solicitacao_por_periodo(email: str, dias_solicitados: int, hoje: dt.date | None = None) -> list[dict]:
    """Distribui uma solicitação regular no modelo FIFO, do período mais antigo para o mais novo."""
    hoje = hoje or dt.date.today()
    resumo = get_resumo_ferias(email)
    periodos = resumo.get("regular", {}).get("periodos", []) or []
    restantes = int(dias_solicitados or 0)
    alloc = []
    for p in periodos:
        saldo = int(p.get("saldo") or 0)
        if saldo <= 0:
            continue
        consumir = min(saldo, restantes)
        if consumir > 0:
            alloc.append({
                "numero": int(p.get("numero") or 0),
                "inicio": p.get("inicio"),
                "fim": p.get("fim"),
                "dias": consumir,
            })
            restantes -= consumir
        if restantes <= 0:
            break
    if restantes > 0:
        raise ValueError(f"Saldo insuficiente para distribuir {dias_solicitados} dia(s). Faltam {restantes}.")
    return alloc


def get_periodo_aquisitivo_atual(email: str, hoje: dt.date | None = None):
    adm = _colaborador_admissao(email)
    atual = _current_partial_period(adm, hoje or dt.date.today()) if adm else None
    if not atual:
        return None
    return {
        "numero": atual["numero"],
        "inicio": atual["inicio"],
        "fim": atual["fim"],
        "label": f"Período {atual['numero']} — {atual['inicio'].strftime('%d/%m/%Y')} a {atual['fim'].strftime('%d/%m/%Y')}",
    }

def get_resumo_ferias(email: str):
    """
    Retorna saldos separados:
      - REGULAR (férias)
      - PREMIUM (Licença Certariana)
    Inclui:
      - usados (APROVADA)
      - reservados (PENDENTE/EM ANÁLISE)
      - ajustes (linhas SOLICITAÇÃO = AJUSTE FÉRIAS / AJUSTE PREMIUM) somados ao direito
    """
    client = get_smartsheet_client()
    if not client:
        raise RuntimeError("Usuário não autenticado")

    email = safe_lower(email)

    # ===== DIREITOS BASE (cadastro) =====
    regular_base = 0
    try:
        colab = _colaborador_por_email(email) or {}
        adm = _colaborador_admissao(email)
        if adm:
            regular_base = _completed_aquisitive_periods(adm) * 30
        else:
            regular_base = colab.get("DIAS DE DIREITO") or colab.get("DIAS DIREITO") or 0
        try:
            regular_base = int(regular_base or 0)
        except Exception:
            regular_base = 0
    except Exception:
        regular_base = 0
    premium_base = 0
    prem_win_start = None
    prem_win_end = None
    try:
        adm = _colaborador_admissao(email)
        premium_base, prem_win_start, prem_win_end = _janela_licenca_certariana(adm) if adm else (0, None, None)
    except Exception:
        premium_base = 0
        prem_win_start = None
        prem_win_end = None

    # ===== ACÚMULOS POR SOLICITAÇÕES =====
    regular_usados = 0
    regular_reservados = 0
    premium_usados = 0
    premium_reservados = 0
    total_solicitacoes = 0

    ajuste_regular = 0
    ajuste_premium = 0

    try:
        sheet_sol = _get_sheet_solicitacoes(client)
        cols = get_col_map(sheet_sol)
        colsN = _cols_norm_map(cols)

        # IMPORTANTE (Planilha 2890766507528068):
        # - O e-mail do colaborador (tanto em solicitações quanto em ajustes) fica na coluna "COLABORADOR".
        col_colab = _col_id(colsN, "COLABORADOR")
        col_status = _col_id(colsN, "STATUS")
        col_dias = _col_id(colsN, "DIAS")
        col_solic = _col_id(colsN, "SOLICITAÇÃO", "SOLICITACAO")
        col_obs = _col_id(colsN, "OBSERVAÇÕES", "OBSERVACOES", "OBSERVACAO")
        col_tipo = _col_id(colsN, "SALDO TIPO", "TIPO SALDO", "TIPO_DE_SALDO", "TIPO DE SALDO")
        col_inicio = _col_id(colsN, "DATA INICIO", "DATA INÍCIO", "DATA INICIAL", "INICIO", "INÍCIO")

        for row in sheet_sol.rows:
            solicit = next((c.value for c in row.cells if c.column_id == (col_solic or -1)), "") or ""

            # Identificação do colaborador sempre pela coluna COLABORADOR
            row_key = next((c.value for c in row.cells if c.column_id == (col_colab or -1)), None)
            if not row_key or safe_lower(str(row_key)) != email:
                continue

            status = next((c.value for c in row.cells if c.column_id == (col_status or -1)), "") or ""
            dias = next((c.value for c in row.cells if c.column_id == (col_dias or -1)), 0) or 0
            obs = next((c.value for c in row.cells if c.column_id == (col_obs or -1)), "") or ""
            explicit_tipo = next((c.value for c in row.cells if c.column_id == (col_tipo or -1)), "") or ""

            inicio_val = next((c.value for c in row.cells if c.column_id == (col_inicio or -1)), None)
            dt_ini_row = _parse_date_value(inicio_val) if inicio_val else None

            try:
                dias = int(float(dias or 0))
            except Exception:
                dias = 0

            st = _norm_status(status)

            # ===== AJUSTES =====
            if _is_ajuste(solicit):
                # só considera ajuste se estiver APROVADO (auditável)
                if st in STATUS_APROVADA:
                    ns = _norm_solicitacao(solicit)
                    if ("premium" in ns) or ("certariana" in ns):
                        ajuste_premium += dias
                    else:
                        ajuste_regular += dias
                continue

            # ===== SOLICITAÇÕES DE FÉRIAS / AFASTAMENTOS =====
            ns_solic = _norm_solicitacao(solicit)
            if ("licenca maternidade" in ns_solic) or ("licenca paternidade" in ns_solic):
                # Afastamentos não impactam saldo de férias.
                continue

            total_solicitacoes += 1
            saldo_tipo = _infer_saldo_tipo(obs, explicit_tipo)

            # Licença Certariana: não cumulativa -> só impacta saldo vigente dentro da janela atual
            if saldo_tipo == "PREMIUM" and prem_win_start and prem_win_end and dt_ini_row:
                if not (prem_win_start <= dt_ini_row < prem_win_end):
                    continue

            if st in STATUS_APROVADA:
                if saldo_tipo == "PREMIUM":
                    premium_usados += dias
                else:
                    regular_usados += dias
            elif st in STATUS_RESERVA:
                if saldo_tipo == "PREMIUM":
                    premium_reservados += dias
                else:
                    regular_reservados += dias

    except Exception as e:
        print(f"ERRO em get_resumo_ferias (novo): {e}")

    regular_direito = int(regular_base) + int(ajuste_regular)
    premium_direito = int(premium_base) + int(ajuste_premium)

    regular_saldo = regular_direito - int(regular_usados) - int(regular_reservados)
    premium_saldo = premium_direito - int(premium_usados) - int(premium_reservados)
    adm_reg = _colaborador_admissao(email)
    regular_periodos = _allocate_period_balance(regular_direito, regular_usados, regular_reservados, adm_reg)
    periodo_atual = get_periodo_aquisitivo_atual(email)

    return {
        "regular": {
            "direito": int(regular_direito),
            "usados": int(regular_usados),
            "reservados": int(regular_reservados),
            "saldo": int(regular_saldo),
            "ajustes": int(ajuste_regular),
            "periodos": regular_periodos,
            "periodo_atual": periodo_atual,
        },
        "premium": {
            "direito": int(premium_direito),
            "usados": int(premium_usados),
            "reservados": int(premium_reservados),
            "saldo": int(premium_saldo),
            "ajustes": int(ajuste_premium),
        },
        "total_solicitacoes": int(total_solicitacoes),
    }

def _listar_colaboradores_cached() -> list[dict]:
    """Cache por request (evita múltiplos get_sheet no mesmo request)."""
    try:
        cached = getattr(g, "_cadastro_colaboradores", None)
        if cached is not None:
            return cached
        cols = listar_colaboradores()
        g._cadastro_colaboradores = cols
        return cols
    except Exception:
        # fora de contexto de request
        return listar_colaboradores()


def listar_emails_colaboradores(only_ativos: bool = True) -> list[str]:
    """Retorna emails de colaboradores (cadastro), opcionalmente filtrando somente ativos."""
    colaboradores = _listar_colaboradores_cached()
    out = []
    seen = set()
    for c in colaboradores:
        if not isinstance(c, dict):
            continue
        if only_ativos and not _is_ativo(c):
            continue
        em = safe_lower(c.get("EMAIL DA EMPRESA") or c.get("EMAIL") or "")
        if not em or em in seen:
            continue
        seen.add(em)
        out.append(em)
    return sorted(out)

def get_subordinados(gestor_email: str, only_ativos: bool = True) -> list[str]:
    """MODIFICADO para suportar GESTOR_SUPERIOR"""
    gestor_email = _norm_email(gestor_email)
    if not gestor_email:
        return []
    
    colaboradores = _listar_colaboradores_cached()
    out = []
    seen = set()
    
    # Verificar se usuario eh do grupo DP
    is_dp_user = tem_grupo(gestor_email, "DP")
    
    for c in colaboradores:
        try:
            if not isinstance(c, dict):
                continue
            
            colab_email = safe_lower(c.get("EMAIL DA EMPRESA") or "")
            gestor_direto = safe_lower(c.get("GESTOR DIRETO") or c.get("GESTOR") or "")
            gestor_superior = safe_lower(c.get("GESTOR SUPERIOR") or "")
            
            if not colab_email:
                continue
            
            if colab_email in seen:
                continue
            
            # Logica:
            # 1. Se GESTOR_SUPERIOR = "dp" e usuario eh DP -> eh subordinado
            # 2. Se GESTOR_SUPERIOR = gestor_email -> eh subordinado
            # 3. Se GESTOR_DIRETO = gestor_email -> eh subordinado
            
            match = False
            if is_dp_user and gestor_superior == "dp":
                match = True
            elif gestor_superior and gestor_superior == gestor_email:
                match = True
            elif gestor_direto == gestor_email:
                match = True
            
            if not match:
                continue
            
            if only_ativos and not _is_ativo(c):
                continue
            
            if colab_email == gestor_email:
                continue
            
            seen.add(colab_email)
            out.append(colab_email)
        except Exception:
            continue
    
    return sorted(out)

def get_subordinados_direto(gestor_email, only_ativos=True):
    """Subordinados DIRETOS (coluna GESTOR DIRETO / fallback GESTOR)."""
    gestor_email = _norm_email(gestor_email)
    if not gestor_email:
        return []
    colaboradores = _listar_colaboradores_cached()
    subs = []
    for c in colaboradores:
        if not isinstance(c, dict):
            continue
        if only_ativos and not _is_ativo(c):
            continue
        email = _norm_email(c.get("EMAIL DA EMPRESA") or "")
        if not email or email == gestor_email:
            continue
        gestor_direto = _norm_email(c.get("GESTOR DIRETO") or c.get("GESTOR") or "")
        if gestor_direto == gestor_email:
            subs.append(email)
    return sorted(subs)



def is_gestor(email: str) -> bool:
    email = _norm_email(email)
    if not email:
        return False
    return len(get_subordinados(email, only_ativos=True)) > 0

def atualizar_relacao_gestor(gestor_email: str, subordinados: list[str]) -> dict:
    """Atualiza a coluna GESTOR DIRETO (fallback: GESTOR) na planilha de cadastro:
    - Define gestor_email para os subordinados informados
    - Remove o gestor_email dos colaboradores que estavam vinculados e foram desmarcados

    Retorna: {ok, updated, message}
    """
    gestor_email = _norm_email(gestor_email)
    subordinados_norm = sorted({_norm_email(e) for e in (subordinados or []) if _norm_email(e) and _norm_email(e) != gestor_email})

    client = get_smartsheet_client()
    if not client:
        return {"ok": False, "updated": 0, "message": "Não autenticado"}

    try:
        sheet = client.Sheets.get_sheet(ID_FOLHA_CADASTRO)
        col_email = _col_id_by_name(sheet, "EMAIL DA EMPRESA")
        col_gestor = _col_id_by_name(sheet, "GESTOR DIRETO", "GESTOR")
        if not col_email:
            return {"ok": False, "updated": 0, "message": "Coluna 'EMAIL DA EMPRESA' não encontrada na planilha de cadastro."}
        if not col_gestor:
            return {"ok": False, "updated": 0, "message": "Coluna 'GESTOR DIRETO' (ou 'GESTOR') não encontrada. Crie a coluna 'GESTOR DIRETO' na planilha de cadastro (ou mantenha 'GESTOR' como fallback)."}

        # monta mapa email -> (row_id, gestor_atual)
        email_to_row = {}
        gestor_atual_por_email = {}
        for row in sheet.rows:
            row_email = None
            row_gestor = None
            for cell in row.cells:
                if cell.column_id == col_email:
                    row_email = safe_lower(cell.value)
                elif cell.column_id == col_gestor:
                    row_gestor = safe_lower(cell.value)
            if row_email:
                email_to_row[row_email] = row.id
                gestor_atual_por_email[row_email] = row_gestor or ""

        updates: list[smartsheet.models.Row] = []

        # Remove vínculo dos que eram desse gestor e foram desmarcados
        for email, g_atual in gestor_atual_por_email.items():
            if g_atual == gestor_email and email not in subordinados_norm:
                r = smartsheet.models.Row()
                r.id = email_to_row[email]
                r.cells = [{"column_id": col_gestor, "value": ""}]
                updates.append(r)

        # Aplica vínculo aos marcados
        for sub in subordinados_norm:
            row_id = email_to_row.get(sub)
            if not row_id:
                continue
            if gestor_atual_por_email.get(sub, "") == gestor_email:
                continue
            r = smartsheet.models.Row()
            r.id = row_id
            r.cells = [{"column_id": col_gestor, "value": gestor_email}]
            updates.append(r)

        if updates:
            client.Sheets.update_rows(ID_FOLHA_CADASTRO, updates)

        # invalida cache do request
        try:
            if hasattr(g, "_cadastro_colaboradores"):
                delattr(g, "_cadastro_colaboradores")
        except Exception:
            pass

        return {"ok": True, "updated": len(updates), "message": "Relação atualizada com sucesso."}
    except Exception as e:
        return {"ok": False, "updated": 0, "message": f"Erro ao atualizar relação: {e}"}


def inject_user_context():
    """Dados do usuario logado - mostra email e papel/permissão (USER TYPE)."""
    user = session.get("user")
    if not user:
        return {}

    email = user.get("email") or ""
    # garante refresh rápido quando trocar USER TYPE no cadastro
    ut = get_user_type(email)

    # grupos compatíveis com legado
    if ut == "ADMIN":
        grupos = ["Administrador"]
    elif ut == "DP":
        grupos = ["DP"]
    else:
        grupos = ["USER"]

    # papel
    if ut == "ADMIN":
        role = "admin"
    elif ut == "DP":
        role = "DP"
    else:
        role = "gestor" if is_gestor(email) else "user"

    display = email or "Usuario"

    role_label = {
        "admin": "ADMIN",
        "DP": "DP",
        "gestor": "GESTOR",
        "user": "USUARIO",
    }.get(role, str(role).upper())

    avatar_seed = (display or email or "U").strip()
    avatar = (avatar_seed[:1] or "U").upper()

    return dict(
        current_user=user,
        user_email=email,
        user_grupos=grupos,
        user_role=role,
        user_role_label=role_label,
        user_display_name=display,
        user_avatar=avatar,
        first=email.split("@")[0],
        last="",
    )

def listar_colaboradores():
    """Lista todos os colaboradores da folha de CADASTRO (1745799836133252)"""
    # Cache por request (evita múltiplos get_sheet no mesmo request)
    try:
        cached = getattr(g, "_colaboradores_list_cache", None)
        if cached is not None:
            return cached
    except Exception:
        pass

    client = get_smartsheet_client()
    if not client:
        print("ERRO: Cliente Smartsheet não autenticado")
        return []
    
    try:
        print(f"[COLABORADORES] Conectando ao Smartsheet CADASTRO com ID: {ID_FOLHA_CADASTRO}")
        sheet_cad = _get_sheet_cadastro(client)
        
        if sheet_cad is None:
            print("ERRO: Folha de cadastro não encontrada")
            return []
        
        print(f"[COLABORADORES] Folha encontrada. Colunas: {[col.title for col in sheet_cad.columns]}")
        cols = get_col_map(sheet_cad)
        
        if not cols:
            print("ERRO: Nenhuma coluna encontrada na folha")
            return []
        
        colaboradores = []
        for row in sheet_cad.rows:
            colaborador = {}
            
            # Pega todas as informações
            for col_name, col_id in cols.items():
                value = next(
                    (c.value for c in row.cells if c.column_id == col_id),
                    None
                )
                colaborador[col_name] = value
            
            # Inclui linhas com email (ou com nome), para suportar gestão por email
            if colaborador.get("EMAIL DA EMPRESA") or colaborador.get("NOME COMPLETO"):
                colaborador["row_id"] = row.id
                colaboradores.append(colaborador)
        
        print(f"[COLABORADORES] Total encontrado: {len(colaboradores)}")
        try:
            g._colaboradores_list_cache = colaboradores
        except Exception:
            pass
        return colaboradores
    except Exception as e:
        print(f"[COLABORADORES] ERRO: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return []

def get_ferias_mes(mes, ano):
    """Retorna férias que intersectam o mês/ano (inclui pendentes), ignorando AJUSTES."""
    client = get_smartsheet_client()
    if not client:
        return []

    try:
        sheet_sol = _get_sheet_solicitacoes(client)
        cols = get_col_map(sheet_sol)
        colsN = _cols_norm_map(cols)

        col_colab = _col_id(colsN, "COLABORADOR")
        col_inicio = _col_id(colsN, "DATA INICIO", "DATA INÍCIO", "DATA INICIAL")
        col_fim = _col_id(colsN, "DATA FIM", "DATA FINAL")
        col_dias = _col_id(colsN, "DIAS")
        col_status = _col_id(colsN, "STATUS")
        col_solic = _col_id(colsN, "SOLICITAÇÃO", "SOLICITACAO")
        col_obs = _col_id(colsN, "OBSERVAÇÕES", "OBSERVACOES", "OBSERVACAO")
        col_tipo = _col_id(colsN, "SALDO TIPO", "TIPO SALDO", "TIPO_DE_SALDO", "TIPO DE SALDO")

        # janela do mês
        primeiro = dt.date(int(ano), int(mes), 1)
        ultimo = dt.date(int(ano), int(mes), 28)
        while True:
            try:
                ultimo = dt.date(int(ano), int(mes), ultimo.day + 1)
            except Exception:
                break

        ferias = []
        for row in sheet_sol.rows:
            email = next((c.value for c in row.cells if c.column_id == (col_colab or -1)), None)
            if not email:
                continue
            email = safe_lower(email)

            solicit_raw = next((c.value for c in row.cells if c.column_id == (col_solic or -1)), "") or ""
            solicit = str(solicit_raw).strip()
            if _is_ajuste(solicit):
                continue

            inicio_raw = next((c.value for c in row.cells if c.column_id == (col_inicio or -1)), None)
            fim_raw = next((c.value for c in row.cells if c.column_id == (col_fim or -1)), None)

            dt_inicio = _parse_date_value(inicio_raw)
            dt_fim = _parse_date_value(fim_raw)
            if not dt_inicio or not dt_fim:
                continue

            # intersecta mês?
            if dt_inicio > ultimo or dt_fim < primeiro:
                continue

            dias = next((c.value for c in row.cells if c.column_id == (col_dias or -1)), 0) or 0
            status = next((c.value for c in row.cells if c.column_id == (col_status or -1)), "") or ""
            if not status:
                status = "PENDENTE"


            # padroniza valores para exibição e regras
            status = _canonical_status(status)
            obs = next((c.value for c in row.cells if c.column_id == (col_obs or -1)), "") or ""
            explicit_tipo_raw = next((c.value for c in row.cells if c.column_id == (col_tipo or -1)), "") or ""
            explicit_tipo = str(explicit_tipo_raw).strip()
            saldo_tipo = str(_infer_saldo_tipo(obs, explicit_tipo) or explicit_tipo or "-").strip() or "-"

            colab = _colaborador_por_email(email) or {}
            nome = colab.get("NOME COMPLETO") or colab.get("NOME") or email
            cargo = colab.get("CARGO") or colab.get("FUNÇÃO") or colab.get("FUNCAO") or ""
            setor = colab.get("SETOR") or colab.get("DEPARTAMENTO") or ""

            ferias.append({
                "row_id": row.id,
                "email": email,
                "nome_completo": nome,
                "cargo": cargo,
                "setor": setor,
                "data_inicio": formatar_data_br(dt_inicio),
                "data_fim": formatar_data_br(dt_fim),
                "dias": dias,
                "status": status,
                "solicitacao": solicit or "-",
                "saldo_tipo": saldo_tipo or "-",
            })

        # ordena por início
        ferias.sort(key=lambda x: (parse_data(x.get("data_inicio")) or dt.date.min))
        return ferias

    except Exception as e:
        print(f"ERRO em get_ferias_mes (novo): {e}")
        return []


def get_direito_e_usado(email):
    """Compat: retorna (direito_regular, usados_aprovados_regular, reservados_pendentes_regular).

    Obs.: O app atual trabalha com 2 saldos (REGULAR e PREMIUM).
    Esta função existe só para rotas antigas que assumem um único saldo.
    """
    resumo = get_resumo_ferias(email)
    return resumo["regular"]["direito"], resumo["regular"]["usados"], resumo["regular"]["reservados"]


def listar_solicitacoes(email):
    """Lista solicitações de férias (exclui AJUSTES) com saldo_tipo e observações."""
    client = get_smartsheet_client()
    if not client:
        return []

    try:
        sheet_sol = _get_sheet_solicitacoes(client)
        cols = get_col_map(sheet_sol)
        colsN = _cols_norm_map(cols)

        col_colab = _col_id(colsN, "COLABORADOR")
        col_inicio = _col_id(colsN, "DATA INICIO", "DATA INÍCIO", "DATA INICIAL")
        col_fim = _col_id(colsN, "DATA FIM", "DATA FINAL")
        col_dias = _col_id(colsN, "DIAS")
        col_status = _col_id(colsN, "STATUS")
        col_solic = _col_id(colsN, "SOLICITAÇÃO", "SOLICITACAO")
        col_obs = _col_id(colsN, "OBSERVAÇÕES", "OBSERVACOES", "OBSERVACAO")
        col_tipo = _col_id(colsN, "SALDO TIPO", "TIPO SALDO", "TIPO_DE_SALDO", "TIPO DE SALDO")

        dados = []
        for row in sheet_sol.rows:
            row_email = next((c.value for c in row.cells if c.column_id == (col_colab or -1)), None)
            if not row_email or safe_lower(row_email) != safe_lower(email):
                continue

            solicit_raw = next((c.value for c in row.cells if c.column_id == (col_solic or -1)), "") or ""
            solicit = str(solicit_raw).strip()
            if _is_ajuste(solicit):
                continue

            inicio_raw = next((c.value for c in row.cells if c.column_id == (col_inicio or -1)), "") or ""
            fim_raw = next((c.value for c in row.cells if c.column_id == (col_fim or -1)), "") or ""
            dias = next((c.value for c in row.cells if c.column_id == (col_dias or -1)), 0) or 0
            status = next((c.value for c in row.cells if c.column_id == (col_status or -1)), "") or ""
            obs = next((c.value for c in row.cells if c.column_id == (col_obs or -1)), "") or ""
            explicit_tipo = next((c.value for c in row.cells if c.column_id == (col_tipo or -1)), "") or ""

            inicio_br = formatar_data_br(inicio_raw)
            fim_br = formatar_data_br(fim_raw)

            saldo_tipo = _infer_saldo_tipo(obs, explicit_tipo)

            dados.append((row.id, inicio_br, fim_br, dias, status, (solicit or ""), saldo_tipo, (obs or "")))

        return dados
    except Exception as e:
        print(f"ERRO em listar_solicitacoes (novo): {e}")
        return []



def listar_solicitacoes_equipes(emails: list[str]):
    """Lista solicitações (exclui AJUSTES) para um conjunto de emails.

    Retorna tuplas:
      (row_id, colaborador_email, inicio_br, fim_br, dias, status, solicitacao, saldo_tipo, obs)

    """
    client = get_smartsheet_client()
    if not client:
        return []

    allowed = {safe_lower(e) for e in (emails or []) if safe_lower(e)}
    if not allowed:
        return []

    try:
        sheet_sol = _get_sheet_solicitacoes(client)
        cols = get_col_map(sheet_sol)
        colsN = _cols_norm_map(cols)
        col_colab = _col_id(colsN, "COLABORADOR")

        col_inicio = _col_id(colsN, "DATA INICIO", "DATA INÍCIO", "DATA INICIAL")
        col_fim = _col_id(colsN, "DATA FIM", "DATA FINAL")
        col_dias = _col_id(colsN, "DIAS")
        col_status = _col_id(colsN, "STATUS")
        col_solic = _col_id(colsN, "SOLICITAÇÃO", "SOLICITACAO")
        col_obs = _col_id(colsN, "OBSERVAÇÕES", "OBSERVACOES", "OBSERVACAO")
        col_tipo = _col_id(colsN, "SALDO TIPO", "TIPO SALDO", "TIPO_DE_SALDO", "TIPO DE SALDO")

        dados = []
        for row in sheet_sol.rows:
            row_email = next((c.value for c in row.cells if c.column_id == (col_colab or -1)), None)
            row_email_n = safe_lower(row_email)
            if not row_email_n or row_email_n not in allowed:
                continue

            solicit_raw = next((c.value for c in row.cells if c.column_id == (col_solic or -1)), "") or ""
            solicit = str(solicit_raw).strip()
            if _is_ajuste(solicit):
                continue

            inicio_raw = next((c.value for c in row.cells if c.column_id == (col_inicio or -1)), "") or ""
            fim_raw = next((c.value for c in row.cells if c.column_id == (col_fim or -1)), "") or ""
            dias = next((c.value for c in row.cells if c.column_id == (col_dias or -1)), 0) or 0
            status = next((c.value for c in row.cells if c.column_id == (col_status or -1)), "") or ""
            obs = next((c.value for c in row.cells if c.column_id == (col_obs or -1)), "") or ""
            explicit_tipo = next((c.value for c in row.cells if c.column_id == (col_tipo or -1)), "") or ""

            inicio_br = formatar_data_br(inicio_raw)
            fim_br = formatar_data_br(fim_raw)
            saldo_tipo = _infer_saldo_tipo(obs, explicit_tipo)

            dados.append((row.id, row_email_n, inicio_br, fim_br, dias, status, (solicit or ""), saldo_tipo, (obs or "")))

        return dados
    except Exception as e:
        print(f"ERRO em listar_solicitacoes_equipes: {e}")
        return []


def listar_solicitacoes_todas():
    """Lista todas as solicitações (exclui AJUSTES).

    Retorna tuplas:
      (row_id, colaborador_email, inicio_br, fim_br, dias, status, solicitacao, saldo_tipo, obs)
    """
    client = get_smartsheet_client()
    if not client:
        return []

    try:
        sheet_sol = _get_sheet_solicitacoes(client)
        cols = get_col_map(sheet_sol)
        colsN = _cols_norm_map(cols)
        col_colab = _col_id(colsN, "COLABORADOR")

        col_inicio = _col_id(colsN, "DATA INICIO", "DATA INÍCIO", "DATA INICIAL")
        col_fim = _col_id(colsN, "DATA FIM", "DATA FINAL")
        col_dias = _col_id(colsN, "DIAS")
        col_status = _col_id(colsN, "STATUS")
        col_solic = _col_id(colsN, "SOLICITAÇÃO", "SOLICITACAO")
        col_obs = _col_id(colsN, "OBSERVAÇÕES", "OBSERVACOES", "OBSERVACAO")
        col_tipo = _col_id(colsN, "SALDO TIPO", "TIPO SALDO", "TIPO_DE_SALDO", "TIPO DE SALDO")

        dados = []
        for row in sheet_sol.rows:
            row_email = next((c.value for c in row.cells if c.column_id == (col_colab or -1)), None)
            row_email_n = safe_lower(row_email)
            if not row_email_n:
                continue

            solicit_raw = next((c.value for c in row.cells if c.column_id == (col_solic or -1)), "") or ""
            solicit = str(solicit_raw).strip()
            if _is_ajuste(solicit):
                continue

            inicio_raw = next((c.value for c in row.cells if c.column_id == (col_inicio or -1)), "") or ""
            fim_raw = next((c.value for c in row.cells if c.column_id == (col_fim or -1)), "") or ""
            dias = next((c.value for c in row.cells if c.column_id == (col_dias or -1)), 0) or 0
            status = next((c.value for c in row.cells if c.column_id == (col_status or -1)), "") or ""
            obs = next((c.value for c in row.cells if c.column_id == (col_obs or -1)), "") or ""
            explicit_tipo = next((c.value for c in row.cells if c.column_id == (col_tipo or -1)), "") or ""

            inicio_br = formatar_data_br(inicio_raw)
            fim_br = formatar_data_br(fim_raw)
            saldo_tipo = _infer_saldo_tipo(obs, explicit_tipo)

            dados.append((row.id, row_email_n, inicio_br, fim_br, dias, status, (solicit or ""), saldo_tipo, (obs or "")))

        return dados
    except Exception as e:
        print(f"ERRO em listar_solicitacoes_todas: {e}")
        return []

def periodo_permitido(dt_inicio, dt_fim, requester_email: str | None = None):
    """Compatibilidade com a regra centralizada em `ferias_app.rules`."""
    from ..rules import validate_request_period

    return validate_request_period(dt_inicio, dt_fim, requester_email=requester_email)

# ============================================
# ROTAS WEB
# ============================================
