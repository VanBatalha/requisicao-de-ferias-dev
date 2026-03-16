import os
import secrets
import urllib.parse
import datetime as dt
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
ID_FOLHA_CADASTRO = int(os.getenv("ID_FOLHA_CADASTRO", "3609445264215940"))  # cadastro colaboradores
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

SECRET_KEY_FLASK = os.getenv("FLASK_SECRET_KEY", "uma_chave_bem_grande_e_fixa_aqui")

app = Flask(__name__)
app.secret_key = SECRET_KEY_FLASK

# ============================================
# PERMISSÕES (USER TYPE) - VIA PLANILHA CADASTRO
# ============================================
#
# A planilha de cadastro (ID_FOLHA_CADASTRO = 3609445264215940) possui a coluna:
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

# ============================================
# CONFIGURAÇÕES DE RUNTIME (ARQUIVO LOCAL)
# ============================================

# Render normalmente permite escrita em /tmp (mais seguro do que no diretório do projeto)
RUNTIME_SETTINGS_PATH = os.getenv(
    "RUNTIME_SETTINGS_PATH",
    "/tmp/requisicao_ferias_runtime_settings.json",
)

_DEFAULT_RUNTIME_SETTINGS = {
    "same_month": {
        # Liberação excepcional para permitir solicitações/edições no mês vigente
        "enabled": True,
        # Implantação 2026: permitir até 11/02/2026
        "until": "2026-02-11",
        "scope": {
            # Como a tela de Solicitações é restrita a Gestores/DP, "all" aqui significa
            # "todos os usuários que já conseguem solicitar".
            "all": True,
            "gestores": False,
            "groups": [],
            "users": [],
        },
    }
}


def _load_runtime_settings() -> dict:
    """Carrega configurações de runtime (com fallback no default)."""
    data = {}
    try:
        if os.path.exists(RUNTIME_SETTINGS_PATH):
            with open(RUNTIME_SETTINGS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
    except Exception:
        data = {}

    # Merge raso com defaults
    out = json.loads(json.dumps(_DEFAULT_RUNTIME_SETTINGS))
    try:
        for k, v in (data or {}).items():
            if isinstance(v, dict) and isinstance(out.get(k), dict):
                out[k].update(v)
            else:
                out[k] = v
    except Exception:
        pass
    return out


def _save_runtime_settings(payload: dict) -> None:
    """Salva configurações de runtime."""
    try:
        with open(RUNTIME_SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(payload or {}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"ERRO ao salvar runtime settings: {e}")


def _parse_iso_date(s: str) -> dt.date | None:
    if not s:
        return None
    try:
        return dt.datetime.strptime(str(s).strip()[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _same_month_override_allowed(requester_email: str) -> bool:
    """Define se o usuário pode solicitar/editar férias no mês vigente (exceção).

    - DP e Administrador: sempre liberados
    - Demais: depende de configuração de runtime + data limite
    """
    requester_email = safe_lower(requester_email)
    if not requester_email:
        return False

    # DP/Admin sempre liberados
    if tem_grupo(requester_email, "DP") or tem_grupo(requester_email, "Administrador"):
        return True

    cfg = _load_runtime_settings().get("same_month", {}) or {}
    if not bool(cfg.get("enabled", False)):
        return False

    until = _parse_iso_date(cfg.get("until") or "")
    if until and dt.date.today() > until:
        return False

    scope = (cfg.get("scope") or {})
    if bool(scope.get("all", False)):
        return True

    if bool(scope.get("gestores", False)) and is_gestor(requester_email):
        return True

    allowed_users = {safe_lower(u) for u in (scope.get("users") or []) if safe_lower(u)}
    if requester_email in allowed_users:
        return True

    allowed_groups = {str(g).strip() for g in (scope.get("groups") or []) if str(g).strip()}
    user_groups = set(get_user_grupos(requester_email) or [])
    if allowed_groups and user_groups.intersection(allowed_groups):
        return True

    return False

def _invalidate_sheet_cache(sheet_id=None):
    """Invalida cache de sheets (chame após qualquer escrita)."""
    try:
        if sheet_id is None:
            _SHEET_CACHE.clear()
        else:
            _SHEET_CACHE.pop(sheet_id, None)
    except Exception:
        pass


def get_smartsheet_client():
    access_token = session.get("access_token")
    if not access_token:
        return None
    return smartsheet.Smartsheet(access_token)

def get_col_map(sheet):
    """Retorna mapa de colunas com tratamento de erro"""
    try:
        if sheet is None or not hasattr(sheet, 'columns'):
            print("ERRO: sheet é None ou não tem columns")
            return {}
        return {col.title: col.id for col in sheet.columns}
    except Exception as e:
        print(f"ERRO em get_col_map: {e}")
        return {}
      


def _get_sheet_solicitacoes(client):
    """Cache por request + cache em memória (TTL) do sheet de solicitações.

    Isso reduz bastante a latência ao navegar entre telas (Smartsheet é o gargalo).
    """
    if not client:
        return None

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
    """Normaliza o valor da coluna USER TYPE para: ADMIN | DP | USER."""
    s = str(value or "").strip()
    if not s:
        return "USER"

    n = _norm_title(s)

    # ADMIN
    if n in ("admin", "administrador", "administrator", "adm"):
        return "ADMIN"

    # DP
    if n in ("dp", "departamento pessoal", "pessoal"):
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


def get_user_type(email: str) -> str:
    """Retorna o USER TYPE do usuário (ADMIN | DP | USER)."""
    em = safe_lower(email)
    if not em:
        return "USER"
    mp = _get_user_type_map_cached()
    return mp.get(em, "USER")


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
    """Define se colaborador está ativo com base no campo Status/STATUS."""
    if not isinstance(colab, dict):
        return False
    val = colab.get("Status")
    if val is None:
        val = colab.get("STATUS")
    if val is None:
        # Se a planilha não tiver coluna de status, considera ativo
        return True
    return str(val).strip().lower() == "ativo"



# ============================================
# RELAÇÃO GESTOR -> SUBORDINADOS (via planilha CADASTRO)
# ============================================

def _norm_email(email: str) -> str:
    return safe_lower(email)

def _norm_title(s: str) -> str:
    s = "" if s is None else str(s)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = " ".join(s.strip().lower().split())
    return s

def _cols_norm_map(cols: dict) -> dict:
    """Map {normalized_title: col_id}"""
    out = {}
    for k, v in (cols or {}).items():
        try:
            out[_norm_title(k)] = v
        except Exception:
            continue
    return out

def _col_id(cols_norm: dict, *candidates: str):
    for name in candidates:
        cid = cols_norm.get(_norm_title(name))
        if cid:
            return cid
    return None

def _norm_solicitacao(s: str) -> str:
    """Normaliza SOLICITAÇÃO: remove acentos, caixa baixa, reduz espaços."""
    return _norm_title(s)

def _is_ajuste(solic: str) -> bool:
    t = _norm_solicitacao(solic)
    return "ajuste" in t

def _infer_saldo_tipo(obs: str, explicit: str = "") -> str:
    """Define se a linha é REGULAR ou PREMIUM."""
    exp = (_norm_title(explicit) if explicit else "")
    if exp in ("premium", "licenca premium", "licença premium"):
        return "PREMIUM"
    if exp in ("regular", "ferias", "férias", "ferias regulares", "férias regulares"):
        return "REGULAR"

    o = _norm_title(obs or "")
    if "saldo: premium" in o or "saldo premium" in o or "[premium]" in o or "premium" in o:
        return "PREMIUM"
    return "REGULAR"

def _calcular_premium_por_tempo(admissao: dt.date, hoje: dt.date | None = None) -> int:
    """+30 ao completar 5 anos; +30 a cada 2 anos adicionais após os 5."""
    if not admissao:
        return 0
    hoje = hoje or dt.date.today()
    years = hoje.year - admissao.year
    if (hoje.month, hoje.day) < (admissao.month, admissao.day):
        years -= 1
    if years < 5:
        return 0
    blocos = 1 + ((years - 5) // 2)
    return int(blocos * 30)

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

def _norm_status(s: str) -> str:
    """Normaliza status: remove acentos, caixa baixa, reduz espaços."""
    if not s:
        return ""
    try:
        import unicodedata
        t = unicodedata.normalize("NFD", str(s))
        t = "".join(ch for ch in t if unicodedata.category(ch) != "Mn")
    except Exception:
        t = str(s)
    t = t.strip().lower()
    t = re.sub(r"\s+", " ", t)
    return t

STATUS_CANON = {
    "pendente": "PENDENTE",
    "em analise": "EM ANÁLISE",
    "em análise": "EM ANÁLISE",
    "aprovada": "APROVADA",
    "aprovado": "APROVADA",
    "cancelada": "CANCELADA",
    "cancelado": "CANCELADA",
    "reprovado": "REPROVADO",
    "reprovada": "REPROVADO",
    "rejeitada": "REPROVADO",
    "rejeitado": "REPROVADO",
}

STATUS_APROVADA = {"aprovada", "aprovado"}
STATUS_RESERVA = {"pendente", "em analise", "em análise"}

def _canonical_status(s: str) -> str:
    n = _norm_status(s)
    return STATUS_CANON.get(n, (s or "").strip().upper())

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
    email = safe_lower(email)
    for c in _listar_colaboradores_cached():
        if safe_lower(c.get("EMAIL DA EMPRESA")) == email:
            return c
    return None

def _colaborador_regime(email: str) -> str:
    c = _colaborador_por_email(email) or {}
    regime = (c.get("REGIME DE CONTRATAÇÃO") or c.get("REGIME DE CONTRATACAO") or "").strip()
    return regime

def _colaborador_admissao(email: str):
    c = _colaborador_por_email(email) or {}
    # tenta várias chaves
    for k in ("DATA DE ADMISSÃO", "DATA DE ADMISSAO", "DATA ADMISSÃO", "DATA ADMISSAO"):
        if k in c and c.get(k):
            return _parse_date_value(c.get(k))
    return None

def calcular_dias_ferias_real_time(admissao: dt.date, hoje=None) -> int:
    """Dias de férias proporcionais (tempo real) com base na data de admissão."""
    if not admissao:
        return 0
    hoje = hoje or dt.date.today()
    if hoje < admissao:
        return 0
    dias_trabalhados = (hoje - admissao).days
    # 30 dias/ano (proporcional) - arredonda para baixo
    return max(0, int((dias_trabalhados / 365.0) * 30.0))

def get_resumo_ferias(email: str):
    """
    Retorna saldos separados:
      - REGULAR (férias)
      - PREMIUM (licença premium)
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
            regular_base = calcular_dias_ferias_real_time(adm)
        else:
            regular_base = colab.get("DIAS DE DIREITO") or colab.get("DIAS DIREITO") or 0
        try:
            regular_base = int(regular_base or 0)
        except Exception:
            regular_base = 0
    except Exception:
        regular_base = 0

    premium_base = 0
    try:
        adm = _colaborador_admissao(email)
        premium_base = _calcular_premium_por_tempo(adm) if adm else 0
    except Exception:
        premium_base = 0

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

        col_email = _col_id(colsN, "COLABORADOR", "EMAIL", "E-MAIL")
        col_status = _col_id(colsN, "STATUS")
        col_dias = _col_id(colsN, "DIAS")
        col_solic = _col_id(colsN, "SOLICITAÇÃO", "SOLICITACAO")
        col_obs = _col_id(colsN, "OBSERVAÇÕES", "OBSERVACOES", "OBSERVACAO")
        col_tipo = _col_id(colsN, "SALDO TIPO", "TIPO SALDO", "TIPO_DE_SALDO", "TIPO DE SALDO")

        for row in sheet_sol.rows:
            row_email = next((c.value for c in row.cells if c.column_id == (col_email or -1)), None)
            if not row_email or safe_lower(row_email) != email:
                continue

            solicit = next((c.value for c in row.cells if c.column_id == (col_solic or -1)), "") or ""
            status = next((c.value for c in row.cells if c.column_id == (col_status or -1)), "") or ""
            dias = next((c.value for c in row.cells if c.column_id == (col_dias or -1)), 0) or 0
            obs = next((c.value for c in row.cells if c.column_id == (col_obs or -1)), "") or ""
            explicit_tipo = next((c.value for c in row.cells if c.column_id == (col_tipo or -1)), "") or ""

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
                    if "premium" in ns:
                        ajuste_premium += dias
                    else:
                        ajuste_regular += dias
                continue

            # ===== SOLICITAÇÕES DE FÉRIAS =====
            total_solicitacoes += 1
            saldo_tipo = _infer_saldo_tipo(obs, explicit_tipo)

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

    return {
        "regular": {
            "direito": int(regular_direito),
            "usados": int(regular_usados),
            "reservados": int(regular_reservados),
            "saldo": int(regular_saldo),
            "ajustes": int(ajuste_regular),
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


@app.context_processor
def inject_user_context():
    """Dados do usuario logado - MODIFICADO para mostrar EMAIL"""
    user = session.get("user")
    if not user:
        return {}
    
    email = user.get("email") or ""
    grupos = get_user_grupos(email)
    
    # MUDANCA: Sempre mostrar email em vez de nome
    display = email or "Usuario"
    
    role = get_user_role(email)
    role_label = {
        "admin": "ADMIN",
        "DP": "DP",  # MUDOU DE DP
        "gestor": "GESTOR",
        "user": "USUARIO",
    }.get(role, role.upper())
    
    avatar_seed = (display or email or "U").strip()
    avatar = (avatar_seed[:1] or "U").upper()
    
    return dict(
        current_user=user,
        user_email=email,
        user_grupos=grupos,
        user_role=role,
        user_role_label=role_label,
        user_display_name=display,  # Agora e o email
        user_avatar=avatar,
        first=email.split("@")[0],
        last="",
    )

def listar_colaboradores():
    """Lista todos os colaboradores da folha de CADASTRO (3609445264215940)"""
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

        col_email = _col_id(colsN, "COLABORADOR", "EMAIL", "E-MAIL")
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
            email = next((c.value for c in row.cells if c.column_id == (col_email or -1)), None)
            if not email:
                continue
            email = safe_lower(email)

            solicit = next((c.value for c in row.cells if c.column_id == (col_solic or -1)), "") or ""
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
            explicit_tipo = next((c.value for c in row.cells if c.column_id == (col_tipo or -1)), "") or ""
            saldo_tipo = _infer_saldo_tipo(obs, explicit_tipo)

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
                "solicitacao": solicit,
                "saldo_tipo": saldo_tipo,
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

        col_email = _col_id(colsN, "COLABORADOR", "EMAIL", "E-MAIL")
        col_inicio = _col_id(colsN, "DATA INICIO", "DATA INÍCIO", "DATA INICIAL")
        col_fim = _col_id(colsN, "DATA FIM", "DATA FINAL")
        col_dias = _col_id(colsN, "DIAS")
        col_status = _col_id(colsN, "STATUS")
        col_solic = _col_id(colsN, "SOLICITAÇÃO", "SOLICITACAO")
        col_obs = _col_id(colsN, "OBSERVAÇÕES", "OBSERVACOES", "OBSERVACAO")
        col_tipo = _col_id(colsN, "SALDO TIPO", "TIPO SALDO", "TIPO_DE_SALDO", "TIPO DE SALDO")

        dados = []
        for row in sheet_sol.rows:
            row_email = next((c.value for c in row.cells if c.column_id == (col_email or -1)), None)
            if not row_email or safe_lower(row_email) != safe_lower(email):
                continue

            solicit = next((c.value for c in row.cells if c.column_id == (col_solic or -1)), "") or ""
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
      (row_id, colaborador_email, inicio_br, fim_br, dias, status, solicitacao, saldo_tipo, obs, criado_por)

    Observação: usa a coluna CRIADO_POR se existir; caso contrário retorna vazio.
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

        col_email = _col_id(colsN, "COLABORADOR", "EMAIL", "E-MAIL")
        col_inicio = _col_id(colsN, "DATA INICIO", "DATA INÍCIO", "DATA INICIAL")
        col_fim = _col_id(colsN, "DATA FIM", "DATA FINAL")
        col_dias = _col_id(colsN, "DIAS")
        col_status = _col_id(colsN, "STATUS")
        col_solic = _col_id(colsN, "SOLICITAÇÃO", "SOLICITACAO")
        col_obs = _col_id(colsN, "OBSERVAÇÕES", "OBSERVACOES", "OBSERVACAO")
        col_tipo = _col_id(colsN, "SALDO TIPO", "TIPO SALDO", "TIPO_DE_SALDO", "TIPO DE SALDO")
        col_criado_por = _col_id(colsN, "CRIADO_POR", "CRIADO POR", "CRIADO-POR", "CRIADO")

        dados = []
        for row in sheet_sol.rows:
            row_email = next((c.value for c in row.cells if c.column_id == (col_email or -1)), None)
            row_email_n = safe_lower(row_email)
            if not row_email_n or row_email_n not in allowed:
                continue

            solicit = next((c.value for c in row.cells if c.column_id == (col_solic or -1)), "") or ""
            if _is_ajuste(solicit):
                continue

            inicio_raw = next((c.value for c in row.cells if c.column_id == (col_inicio or -1)), "") or ""
            fim_raw = next((c.value for c in row.cells if c.column_id == (col_fim or -1)), "") or ""
            dias = next((c.value for c in row.cells if c.column_id == (col_dias or -1)), 0) or 0
            status = next((c.value for c in row.cells if c.column_id == (col_status or -1)), "") or ""
            obs = next((c.value for c in row.cells if c.column_id == (col_obs or -1)), "") or ""
            explicit_tipo = next((c.value for c in row.cells if c.column_id == (col_tipo or -1)), "") or ""
            criado_por = next((c.value for c in row.cells if c.column_id == (col_criado_por or -1)), "") or ""

            inicio_br = formatar_data_br(inicio_raw)
            fim_br = formatar_data_br(fim_raw)
            saldo_tipo = _infer_saldo_tipo(obs, explicit_tipo)

            dados.append((row.id, row_email_n, inicio_br, fim_br, dias, status, (solicit or ""), saldo_tipo, (obs or ""), safe_lower(criado_por)))

        return dados
    except Exception as e:
        print(f"ERRO em listar_solicitacoes_equipes: {e}")
        return []


def listar_solicitacoes_todas():
    """Lista todas as solicitações (exclui AJUSTES).

    Retorna tuplas:
      (row_id, colaborador_email, inicio_br, fim_br, dias, status, solicitacao, saldo_tipo, obs, criado_por)
    """
    client = get_smartsheet_client()
    if not client:
        return []

    try:
        sheet_sol = _get_sheet_solicitacoes(client)
        cols = get_col_map(sheet_sol)
        colsN = _cols_norm_map(cols)

        col_email = _col_id(colsN, "COLABORADOR", "EMAIL", "E-MAIL")
        col_inicio = _col_id(colsN, "DATA INICIO", "DATA INÍCIO", "DATA INICIAL")
        col_fim = _col_id(colsN, "DATA FIM", "DATA FINAL")
        col_dias = _col_id(colsN, "DIAS")
        col_status = _col_id(colsN, "STATUS")
        col_solic = _col_id(colsN, "SOLICITAÇÃO", "SOLICITACAO")
        col_obs = _col_id(colsN, "OBSERVAÇÕES", "OBSERVACOES", "OBSERVACAO")
        col_tipo = _col_id(colsN, "SALDO TIPO", "TIPO SALDO", "TIPO_DE_SALDO", "TIPO DE SALDO")
        col_criado_por = _col_id(colsN, "CRIADO_POR", "CRIADO POR", "CRIADO-POR", "CRIADO")

        dados = []
        for row in sheet_sol.rows:
            row_email = next((c.value for c in row.cells if c.column_id == (col_email or -1)), None)
            row_email_n = safe_lower(row_email)
            if not row_email_n:
                continue

            solicit = next((c.value for c in row.cells if c.column_id == (col_solic or -1)), "") or ""
            if _is_ajuste(solicit):
                continue

            inicio_raw = next((c.value for c in row.cells if c.column_id == (col_inicio or -1)), "") or ""
            fim_raw = next((c.value for c in row.cells if c.column_id == (col_fim or -1)), "") or ""
            dias = next((c.value for c in row.cells if c.column_id == (col_dias or -1)), 0) or 0
            status = next((c.value for c in row.cells if c.column_id == (col_status or -1)), "") or ""
            obs = next((c.value for c in row.cells if c.column_id == (col_obs or -1)), "") or ""
            explicit_tipo = next((c.value for c in row.cells if c.column_id == (col_tipo or -1)), "") or ""
            criado_por = next((c.value for c in row.cells if c.column_id == (col_criado_por or -1)), "") or ""

            inicio_br = formatar_data_br(inicio_raw)
            fim_br = formatar_data_br(fim_raw)
            saldo_tipo = _infer_saldo_tipo(obs, explicit_tipo)

            dados.append((row.id, row_email_n, inicio_br, fim_br, dias, status, (solicit or ""), saldo_tipo, (obs or ""), safe_lower(criado_por)))

        return dados
    except Exception as e:
        print(f"ERRO em listar_solicitacoes_todas: {e}")
        return []

def _listar_periodos_certariana(email: str, exclude_row_id: int | None = None, include_statuses=None):
    """Lista períodos existentes de Licença Certariana/Premium do colaborador."""
    periodos = []
    if include_statuses is None:
        include_statuses = set(STATUS_APROVADA) | set(STATUS_RESERVA)

    for row_id, row_email, inicio_br, fim_br, dias, status, solicitacao, saldo_tipo, obs, criado_por in listar_solicitacoes_todas():
        if safe_lower(row_email) != safe_lower(email):
            continue
        if exclude_row_id is not None and int(row_id) == int(exclude_row_id):
            continue
        if (saldo_tipo or "").upper() != "PREMIUM":
            continue

        st = _norm_status(status)
        if st not in include_statuses:
            continue

        try:
            dias_int = int(float(dias or 0))
        except Exception:
            dias_int = 0

        periodos.append({
            "row_id": row_id,
            "inicio": inicio_br,
            "fim": fim_br,
            "dias": dias_int,
            "status": status,
            "solicitacao": solicitacao,
        })

    return periodos


def validar_licenca_certariana(email: str, dias_novos: int, exclude_row_id: int | None = None, include_statuses=None):
    """
    Regras da Licença Certariana/Premium:
      - até 3 períodos;
      - mínimo de 10 dias por período;
      - com 2 períodos, não pode sobrar saldo entre 1 e 9 dias;
      - com 3 períodos, o total deve ser exatamente 30 dias.
    """
    if dias_novos < 10:
        return False, "Na Licença Certariana, cada período deve ter no mínimo 10 dias."

    periodos = _listar_periodos_certariana(email, exclude_row_id=exclude_row_id, include_statuses=include_statuses)
    dias_existentes = [int(p["dias"] or 0) for p in periodos]

    todos = dias_existentes + [int(dias_novos)]
    qtd = len(todos)
    total = sum(todos)

    if qtd > 3:
        return False, "Na Licença Certariana, você pode solicitar no máximo 3 períodos."

    if any(d < 10 for d in todos):
        return False, "Na Licença Certariana, cada período deve ter no mínimo 10 dias."

    if total > 30:
        return False, "Na Licença Certariana não permite ultrapassar 30 dias no total."

    if qtd == 2:
        saldo_restante = 30 - total
        if 0 < saldo_restante < 10:
            return False, (
                "Na Licença Certariana, com 2 períodos não é permitido sobrar saldo menor que 10 dias. "
                f"Após esta solicitação restariam {saldo_restante} dia(s)."
            )

    if qtd == 3 and total != 30:
        return False, "Na Licença Certariana, com 3 períodos o total deve ser exatamente 30 dias."

    return True, ""

def periodo_permitido(dt_inicio, dt_fim, requester_email: str | None = None):
    """Valida regras de período de férias.

    Regras principais:
      - Não permite mês vigente (exceto liberação excepcional)
      - Não permite mês seguinte após o dia 21
      - Não permite passado
    """
    hoje = dt.date.today()
    
    if dt_fim < hoje or dt_inicio <= hoje:
        return False, "Nao eh permitido solicitar ou editar ferias no mes vigente ou no passado."
    
    def ym(d): return (d.year, d.month)
    
    ym_hoje = ym(hoje)
    
    if ym(dt_inicio) == ym_hoje or ym(dt_fim) == ym_hoje:
        if requester_email and _same_month_override_allowed(requester_email):
            pass
        else:
            return False, "Nao eh permitido solicitar ou editar ferias no mes vigente."
    
    # MUDANCA: Alterado de 20 para 21
    if 21 <= hoje.day <= 31:  # MUDOU DE 20 PARA 21
        prox_ano = hoje.year + 1 if hoje.month == 12 else hoje.year
        prox_mes = 1 if hoje.month == 12 else hoje.month + 1
        nym_prox = (prox_ano, prox_mes)
        
        if ym(dt_inicio) == nym_prox or ym(dt_fim) == nym_prox:
            return False, "Nao eh permitido solicitar ou editar ferias do mes seguinte apos o dia 21."
    
    return True, ""

# ============================================
# ROTAS WEB
# ============================================

@app.route("/")
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
        return redirect(url_for("painel_admin"))
    if "DP" in grupos:
        return redirect(url_for("painel_dp"))
    return redirect(url_for("ferias"))

@app.route("/ferias")
def ferias():
    user = session.get("user")
    if not user:
        return render_template("base.html", content="login")

    gestor_email = safe_lower(user.get("email") or "")
    if not gestor_email:
        return redirect(url_for("logout"))

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

@app.route("/painel-admin")
def painel_admin():
    user = session.get("user")
    if not user:
        return redirect(url_for("login"))
    
    email = user.get("email")
    if not tem_grupo(email, "Administrador"):
        return "Acesso negado. Você não é administrador.", 403
    
    return render_template("painel_admin.html", user=user, active_page="admin")

@app.route("/painel-dp")
def painel_dp():
    user = session.get("user")
    if not user:
        return redirect(url_for("login"))
    
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

@app.route("/login")
def login():
    state = secrets.token_urlsafe(16)
    session["oauth_state"] = state
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "state": state,
    }
    url = AUTH_URL + "?" + urllib.parse.urlencode(params)
    return redirect(url)

@app.route("/callback")
def callback():
    error = request.args.get("error")
    if error:
        return f"Erro na autorização: {error}"
    
    code = request.args.get("code")
    state = request.args.get("state")
    if not state or state != session.get("oauth_state"):
        return "State inválido. Possível CSRF.", 400
    
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
    }
    
    token_resp = requests.post(TOKEN_URL, data=data)
    if token_resp.status_code != 200:
        return f"Erro ao obter token: {token_resp.status_code} - {token_resp.text}"
    
    token_json = token_resp.json()
    access_token = token_json.get("access_token")
    if not access_token:
        return f"Token não retornado: {token_json}"
    
    session["access_token"] = access_token
    
    headers = {"Authorization": f"Bearer {access_token}"}
    me_resp = requests.get(CURRENT_USER_URL, headers=headers)
    if me_resp.status_code != 200:
        return f"Erro ao obter usuário: {me_resp.status_code} - {me_resp.text}"
    
    user = me_resp.json()
    session["user"] = user
    
    return redirect(url_for("index"))

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))

# ============================================
# API: ADMIN - GERENCIAR GRUPOS
# ============================================

@app.route("/api/admin/listar-usuarios")
def api_admin_listar_usuarios():
    user = session.get("user")
    if not user or not tem_grupo(user.get("email"), "Administrador"):
        return jsonify({"ok": False, "message": "Acesso negado"}), 403

    q = (request.args.get("q") or "").strip().lower()

    try:
        colaboradores = listar_colaboradores()

        # filtra somente Status = Ativo
        colaboradores = [c for c in colaboradores if _is_ativo(c)]

        # se não houver busca, não devolve tudo (evita listar milhares)
        if q:
            def _match(c):
                nome = str(c.get("NOME COMPLETO") or "").lower()
                email = str(c.get("EMAIL DA EMPRESA") or "").lower()
                return q in nome or q in email
            colaboradores = [c for c in colaboradores if _match(c)]
        else:
            colaboradores = []

        # limita retorno
        colaboradores = colaboradores[:10]

        # Adiciona grupos de cada usuário
        for colab in colaboradores:
            email = colab.get("EMAIL DA EMPRESA")
            colab["user_type"] = get_user_type(email)
            colab["grupos"] = get_user_grupos(email)

        return jsonify({"ok": True, "usuarios": colaboradores})
    except Exception as e:
        print(f"ERRO em api_admin_listar_usuarios: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "message": f"Erro ao buscar usuários: {str(e)}"}), 500

@app.route("/api/admin/atualizar-grupos", methods=["POST"])
def api_admin_atualizar_grupos():
    user = session.get("user")
    if not user or not tem_grupo(user.get("email"), "Administrador"):
        return jsonify({"ok": False, "message": "Acesso negado"}), 403

    payload = request.get_json(silent=True) or request.form

    email = (payload.get("email") or "").strip()
    if not email:
        return jsonify({"ok": False, "message": "Email é obrigatório"}), 400

    grupos_in = payload.get("grupos", [])
    grupos = []

    try:
        if isinstance(grupos_in, str):
            grupos = json.loads(grupos_in) if grupos_in.strip() else []
        elif isinstance(grupos_in, list):
            grupos = grupos_in
        else:
            grupos = []
    except Exception:
        return jsonify({"ok": False, "message": "Formato de grupos inválido"}), 400

    # normaliza: só aceita grupos conhecidos
    grupos_validos = []
    for g_ in grupos:
        g_ = str(g_).strip()
        if g_ in ("Administrador", "DP", "USER"):
            grupos_validos.append(g_)

    # converte grupos -> USER TYPE (ADMIN | DP | USER)
    if "Administrador" in grupos_validos:
        user_type_value = "ADMIN"
        grupos_validos = ["Administrador"]
    elif "DP" in grupos_validos:
        user_type_value = "DP"
        grupos_validos = ["DP"]
    else:
        user_type_value = "USER"
        grupos_validos = ["USER"]

    client = get_smartsheet_client()
    if not client:
        return jsonify({"ok": False, "message": "Não autenticado"}), 401

    try:
        sheet = _get_sheet_cadastro(client)
        if not sheet:
            return jsonify({"ok": False, "message": "Folha de cadastro não encontrada"}), 404

        col_email = _col_id_by_name(sheet, "EMAIL DA EMPRESA", "EMAIL")
        col_user_type = _col_id_by_name(sheet, "USER TYPE", "USER_TYPE", "USERTYPE", "TIPO USUARIO", "TIPO DE USUARIO")

        if not col_email:
            return jsonify({"ok": False, "message": "Coluna 'EMAIL DA EMPRESA' não encontrada no cadastro."}), 400
        if not col_user_type:
            return jsonify({"ok": False, "message": "Coluna 'USER TYPE' não encontrada no cadastro. Crie a coluna 'USER TYPE' na planilha 3609445264215940."}), 400

        email_lower = safe_lower(email)
        row_id = None
        for row in sheet.rows:
            row_email = None
            for cell in row.cells:
                if cell.column_id == col_email:
                    row_email = safe_lower(cell.value)
                    break
            if row_email == email_lower:
                row_id = row.id
                break

        if not row_id:
            return jsonify({"ok": False, "message": "Usuário não encontrado na planilha de cadastro."}), 404

        row_update = smartsheet.models.Row()
        row_update.id = row_id
        row_update.cells = [{"column_id": col_user_type, "value": user_type_value}]
        client.Sheets.update_rows(ID_FOLHA_CADASTRO, [row_update])

        # invalida caches para refletir imediatamente
        _invalidate_sheet_cache(ID_FOLHA_CADASTRO)
        try:
            if hasattr(g, "_colaboradores_list_cache"):
                delattr(g, "_colaboradores_list_cache")
            if hasattr(g, "_cadastro_colaboradores"):
                delattr(g, "_cadastro_colaboradores")
            if hasattr(g, "_user_type_map"):
                delattr(g, "_user_type_map")
            if hasattr(g, "_sheet_cadastro"):
                delattr(g, "_sheet_cadastro")
        except Exception:
            pass

        print(f"[ADMIN] USER TYPE atualizado para {email_lower}: {user_type_value}")
        return jsonify({"ok": True, "message": f"Permissão de {email} atualizada com sucesso", "grupos": grupos_validos, "user_type": user_type_value})

    except Exception as e:
        print(f"ERRO em api_admin_atualizar_grupos: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "message": f"Erro ao atualizar permissão: {str(e)}"}), 500


# ============================================
# API: ADMIN - LIBERAÇÃO EXCEPCIONAL (MÊS VIGENTE)
# ============================================

@app.route("/api/admin/same-month", methods=["GET"])
def api_admin_get_same_month():
    user = session.get("user")
    if not user or not tem_grupo(user.get("email"), "Administrador"):
        return jsonify({"ok": False, "message": "Acesso negado"}), 403

    cfg = _load_runtime_settings().get("same_month", {})
    return jsonify({"ok": True, "same_month": cfg})


@app.route("/api/admin/same-month", methods=["POST"])
def api_admin_set_same_month():
    user = session.get("user")
    if not user or not tem_grupo(user.get("email"), "Administrador"):
        return jsonify({"ok": False, "message": "Acesso negado"}), 403

    payload = request.get_json(silent=True) or {}

    enabled = bool(payload.get("enabled", False))
    until_raw = (payload.get("until") or "").strip()
    # valida data (mantém string original no formato ISO)
    until_dt = _parse_iso_date(until_raw)
    until = until_dt.strftime("%Y-%m-%d") if until_dt else ""

    scope_in = payload.get("scope") or {}
    scope = {
        "all": bool(scope_in.get("all", False)),
        "gestores": bool(scope_in.get("gestores", False)),
        "groups": [str(g).strip() for g in (scope_in.get("groups") or []) if str(g).strip()],
        "users": [safe_lower(u) for u in (scope_in.get("users") or []) if safe_lower(u)],
    }

    settings = _load_runtime_settings()
    settings["same_month"] = {
        "enabled": enabled,
        "until": until,
        "scope": scope,
    }
    _save_runtime_settings(settings)

    return jsonify({"ok": True, "same_month": settings["same_month"]})

# ============================================
# API: dp - COLABORADORES (Planilha 360944526 - COLABORADORES (Planilha 3609445264215940)
# ============================================

@app.route("/api/dp/colaboradores")
def api_dp_colaboradores():
    user = session.get("user")
    if not user or not (tem_grupo(user.get("email"), "DP") or tem_grupo(user.get("email"), "Administrador")):
        return jsonify({"ok": False, "message": "Acesso negado"}), 403

    status_filter = (request.args.get("status") or "").upper().strip()

    try:
        colaboradores = listar_colaboradores()

        # Filtra por status se solicitado (aceita coluna Status/STATUS)
        if status_filter == "ATIVO":
            colaboradores = [c for c in colaboradores if _is_ativo(c)]
        elif status_filter == "INATIVO":
            colaboradores = [c for c in colaboradores if not _is_ativo(c)]

        return jsonify({
            "ok": True,
            "colaboradores": colaboradores
        })
    except Exception as e:
        print(f"ERRO em api_dp_colaboradores: {e}")
        return jsonify({"ok": False, "message": str(e)}), 500
@app.route("/api/dp/saldos/<path:email>")
def api_dp_saldos(email):
    user = session.get("user")
    if not user or not (tem_grupo(user.get("email"), "DP") or tem_grupo(user.get("email"), "Administrador")):
        return jsonify({"ok": False, "message": "Acesso negado"}), 403

    email = safe_lower(email or "")
    if not email:
        return jsonify({"ok": False, "message": "Email inválido"}), 400

    try:
        resumo = get_resumo_ferias(email)
        return jsonify({
            "ok": True,
            "email": email,
            "regular": resumo["regular"],
            "premium": resumo["premium"],
        })
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


@app.route("/api/dp/ajustes/lancar", methods=["POST"])
def api_dp_ajustes_lancar():
    user = session.get("user")
    if not user or not (tem_grupo(user.get("email"), "DP") or tem_grupo(user.get("email"), "Administrador")):
        return jsonify({"ok": False, "message": "Acesso negado"}), 403

    client = get_smartsheet_client()
    if not client:
        return jsonify({"ok": False, "message": "Usuário não autenticado"}), 401

    payload = request.get_json(silent=True) or {}
    colab_email = safe_lower(payload.get("colaborador_email") or payload.get("email") or "")
    solicitacao = (payload.get("solicitacao") or "").strip().upper()
    obs_user = (payload.get("observacoes") or "").strip()

    try:
        dias = int(float(payload.get("dias") or 0))
    except Exception:
        dias = 0

    if not colab_email:
        return jsonify({"ok": False, "message": "Colaborador inválido"}), 400
    if solicitacao not in ("AJUSTE FÉRIAS", "AJUSTE PREMIUM"):
        return jsonify({"ok": False, "message": "Solicitação inválida"}), 400
    if dias == 0:
        return jsonify({"ok": False, "message": "Dias deve ser diferente de zero"}), 400

    dp_email = safe_lower(user.get("email") or "")
    obs_final = obs_user
    complemento = f"Ajuste feito pelo DP ({dp_email})"
    if complemento.lower() not in obs_final.lower():
        obs_final = (obs_final + ("\n" if obs_final else "") + complemento).strip()

    hoje = dt.date.today().strftime("%Y-%m-%d")

    try:
        sheet_sol = _get_sheet_solicitacoes(client)
        cols = get_col_map(sheet_sol)
        colsN = _cols_norm_map(cols)

        col_email = _col_id(colsN, "COLABORADOR", "EMAIL", "E-MAIL")
        col_solic = _col_id(colsN, "SOLICITAÇÃO", "SOLICITACAO")
        col_inicio = _col_id(colsN, "DATA INICIO", "DATA INÍCIO", "DATA INICIAL")
        col_fim = _col_id(colsN, "DATA FIM", "DATA FINAL")
        col_dias = _col_id(colsN, "DIAS")
        col_status = _col_id(colsN, "STATUS")
        col_gestor = _col_id(colsN, "GESTOR SOLICITANTE", "GESTOR")
        col_criado = _col_id(colsN, "CRIADO_POR", "CRIADO POR", "Criado_por")
        col_obs = _col_id(colsN, "OBSERVAÇÕES", "OBSERVACOES", "OBSERVACAO")

        new_row = smartsheet.models.Row()
        new_row.to_top = True
        new_row.cells = []

        def add_cell(col_id, value):
            if isinstance(col_id, int) and col_id > 0:
                new_row.cells.append({"column_id": col_id, "value": value})

        add_cell(col_email, colab_email)
        add_cell(col_solic, solicitacao)
        add_cell(col_inicio, hoje)
        add_cell(col_fim, hoje)
        add_cell(col_dias, dias)
        add_cell(col_status, "APROVADA")
        add_cell(col_gestor, dp_email)
        add_cell(col_criado, dp_email)
        add_cell(col_obs, obs_final)

        client.Sheets.add_rows(ID_FOLHA_SOLICITACOES, [new_row])
        _invalidate_sheet_cache(ID_FOLHA_SOLICITACOES)

        resumo = get_resumo_ferias(colab_email)
        return jsonify({
            "ok": True,
            "message": "Ajuste lançado com sucesso.",
            "regular": resumo["regular"],
            "premium": resumo["premium"],
        })

    except Exception as e:
        return jsonify({"ok": False, "message": f"Erro ao lançar ajuste: {e}"}), 500
  
@app.route("/api/dp/colaborador/<email>")
def api_dp_colaborador(email):
    user = session.get("user")
    if not user or not (tem_grupo(user.get("email"), "DP") or tem_grupo(user.get("email"), "Administrador")):
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
# API: dp - GESTORES (relação Gestor -> Subordinados)
# ============================================

@app.route("/api/dp/gestores/relacao", methods=["GET", "POST"])
def api_dp_gestores_relacao():
    user = session.get("user")
    if not user or not (tem_grupo(user.get("email"), "DP") or tem_grupo(user.get("email"), "Administrador")):
        return jsonify({"ok": False, "message": "Acesso negado"}), 403

    if request.method == "GET":
        gestor = _norm_email(request.args.get("gestor") or "")
        if not gestor:
            return jsonify({"ok": True, "gestor": "", "subordinados": []})
        subs = get_subordinados_direto(gestor)
        return jsonify({"ok": True, "gestor": gestor, "subordinados": subs})

    payload = request.get_json(silent=True) or {}
    gestor = _norm_email(payload.get("gestor") or "")
    subordinados = payload.get("subordinados") or payload.get("subordinates") or []
    if isinstance(subordinados, str):
        subordinados = [subordinados]

    if not gestor:
        return jsonify({"ok": False, "message": "Gestor é obrigatório"}), 400

    atualizar_relacao_gestor(gestor, subordinados)
    return jsonify({"ok": True, "message": "Relação atualizada com sucesso."})


@app.route("/api/dp/gestores/superior", methods=["GET", "POST"])
def api_dp_gestor_superior():
    """Lê/atualiza a coluna GESTOR SUPERIOR do colaborador (cadastro)."""
    user = session.get("user")
    if not user or not (tem_grupo(user.get("email"), "DP") or tem_grupo(user.get("email"), "Administrador")):
        return jsonify({"ok": False, "message": "Acesso negado"}), 403

    client = get_smartsheet_client()
    if not client:
        return jsonify({"ok": False, "message": "Usuário não autenticado"}), 401

    sheet = client.Sheets.get_sheet(ID_FOLHA_CADASTRO)
    cols = get_col_map(sheet)
    col_email = cols.get("EMAIL DA EMPRESA") or cols.get("EMAIL")
    col_sup = cols.get("GESTOR SUPERIOR")
    if not col_email or not col_sup:
        return jsonify({"ok": False, "message": "Colunas EMAIL DA EMPRESA e/ou GESTOR SUPERIOR não encontradas."}), 500

    if request.method == "GET":
        colaborador = _norm_email(request.args.get("colaborador") or "")
        if not colaborador:
            return jsonify({"ok": True, "colaborador": "", "gestor_superior": ""})
        for row in sheet.rows:
            row_email = _norm_email(next((c.value for c in row.cells if c.column_id == col_email), ""))
            if row_email == colaborador:
                valor = next((c.value for c in row.cells if c.column_id == col_sup), "") or ""
                return jsonify({"ok": True, "colaborador": colaborador, "gestor_superior": str(valor).strip()})
        return jsonify({"ok": False, "message": "Colaborador não encontrado"}), 404

    payload = request.get_json(silent=True) or {}
    colaborador = _norm_email(payload.get("colaborador") or "")
    valor = (payload.get("gestor_superior") or payload.get("valor") or "").strip()
    if not colaborador:
        return jsonify({"ok": False, "message": "Colaborador é obrigatório"}), 400
    if not valor:
        return jsonify({"ok": False, "message": "Gestor Superior é obrigatório"}), 400

    # normaliza valores especiais
    if safe_lower(valor) in ("dp",):
        valor_out = "DP"
    elif safe_lower(valor) in ("gestor",):
        valor_out = "GESTOR"
    else:
        valor_out = _norm_email(valor)

    # atualiza a linha
    target_row_id = None
    for row in sheet.rows:
        row_email = _norm_email(next((c.value for c in row.cells if c.column_id == col_email), ""))
        if row_email == colaborador:
            target_row_id = row.id
            break

    if not target_row_id:
        return jsonify({"ok": False, "message": "Colaborador não encontrado"}), 404

    try:
        row_update = smartsheet.models.Row()
        row_update.id = target_row_id
        row_update.cells = [{"column_id": col_sup, "value": valor_out}]
        client.Sheets.update_rows(ID_FOLHA_CADASTRO, [row_update])
        try:
            if hasattr(g, "_cadastro_colaboradores"):
                delattr(g, "_cadastro_colaboradores")
        except Exception:
            pass
        return jsonify({"ok": True, "message": "Gestor Superior atualizado com sucesso."})
    except Exception as e:
        return jsonify({"ok": False, "message": f"Erro ao atualizar Gestor Superior: {e}"}), 500

# ============================================
# API: dp - FÉRIAS (Planilha 2890766507528068)
# ============================================

@app.route("/api/dp/ferias-mes")
def api_dp_ferias_mes():
    user = session.get("user")
    if not user or not (tem_grupo(user.get("email"), "DP") or tem_grupo(user.get("email"), "Administrador")):
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

@app.route("/api/dp/atualizar-status-solicitacao", methods=["POST"])
def api_dp_atualizar_status():
    """DP pode alterar status das solicitacoes"""
    user = session.get("user")
    if not user or not (tem_grupo(user.get("email"), "DP") or tem_grupo(user.get("email"), "Administrador")):
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
        client = get_smartsheet_client()
        sheet_sol = _get_sheet_solicitacoes(client)
        cols_sol = get_col_map(sheet_sol)
        
        row_id_int = int(row_id)
        col_status = _col_id_by_name(sheet_sol, "STATUS")

        if not col_status:
            return jsonify({"ok": False, "message": "Coluna STATUS nao encontrada"}), 500
        
        row_update = smartsheet.models.Row()
        row_update.id = row_id_int
        row_update.cells = [{"column_id": col_status, "value": _canonical_status(novo_status_upper)}]
        
        client.Sheets.update_rows(ID_FOLHA_SOLICITACOES, [row_update])
        _invalidate_sheet_cache(ID_FOLHA_SOLICITACOES)
        
        return jsonify({"ok": True, "message": f"Status atualizado para {novo_status_upper}"})
    except Exception as e:
        print(f"ERRO em api_dp_atualizar_status: {e}")
        return jsonify({"ok": False, "message": str(e)}), 500

# ============================================
# API: SOLICITAÇÃO DE FÉRIAS
# ============================================

@app.route("/api/solicitar-ferias", methods=["POST"])
def api_solicitar_ferias():
    user = session.get("user")
    if not user:
        return jsonify({"ok": False, "message": "Não autenticado."}), 401

    gestor_email = safe_lower(user.get("email") or "")
    if not gestor_email:
        return jsonify({"ok": False, "message": "Usuário inválido."}), 400

    role = get_user_role(gestor_email)
    is_dp_or_admin = role in ("DP", "admin")

    # Regra:
    # - Gestores solicitam para sua equipe
    # - DP/Admin podem solicitar para qualquer colaborador ativo
    if not (is_dp_or_admin or is_gestor(gestor_email)):
        return jsonify({"ok": False, "message": "Apenas gestores (ou DP/Admin) podem solicitar férias."}), 403

    colaborador_email = safe_lower(request.form.get("colaborador_email") or request.form.get("colaborador") or "")
    tipo_solicitacao = (request.form.get("tipo_solicitacao") or "").strip()

    data_inicio_str = request.form.get("data_inicio")
    data_fim_str = request.form.get("data_fim")
    observacoes = (request.form.get("observacoes") or "").strip()

    saldo_tipo_req = (request.form.get("saldo_tipo") or "REGULAR").strip().upper()
    if saldo_tipo_req not in ("REGULAR", "PREMIUM"):
        saldo_tipo_req = "REGULAR"

    if not colaborador_email:
        return jsonify({"ok": False, "message": "Selecione o colaborador."}), 400

    if not tipo_solicitacao:
        return jsonify({"ok": False, "message": "Selecione o tipo de solicitação (Venda ou Gozo)."}), 400

    tipo_norm = tipo_solicitacao.strip().lower()
    if tipo_norm in ("usufruir", "usufruto", "gozar", "gozo"):
        tipo_solicitacao_out = "Gozo"
    elif tipo_norm in ("venda", "vender"):
        tipo_solicitacao_out = "Venda"
    else:
        # aceita exatamente o que veio, mas valida mínimo
        if tipo_norm not in ("venda", "gozo"):
            return jsonify({"ok": False, "message": "Tipo inválido. Use Venda ou Gozo."}), 400
        tipo_solicitacao_out = tipo_solicitacao.title()

    # valida se colaborador está no escopo
    if is_dp_or_admin:
        permitidos = set(listar_emails_colaboradores(only_ativos=True))
        if colaborador_email not in permitidos:
            return jsonify({"ok": False, "message": "Colaborador não encontrado (ou não está Ativo no cadastro)."}), 400
    else:
        permitidos = set(get_subordinados(gestor_email))  # gestor não solicita para si nesta tela
        if colaborador_email not in permitidos:
            return jsonify({"ok": False, "message": "Colaborador não pertence à sua equipe (ou não está vinculado ao seu gestor)."}), 403

    if not data_inicio_str or not data_fim_str:
        return jsonify({"ok": False, "message": "Datas obrigatórias."}), 400

    dt_inicio = dt.datetime.strptime(data_inicio_str, "%Y-%m-%d").date()
    dt_fim = dt.datetime.strptime(data_fim_str, "%Y-%m-%d").date()

    if dt_fim < dt_inicio:
        return jsonify({"ok": False, "message": "Data fim não pode ser menor que data início."}), 400

    ok_periodo, msg = periodo_permitido(dt_inicio, dt_fim, requester_email=gestor_email)
    if not ok_periodo:
        return jsonify({"ok": False, "message": msg}), 400

    # Regra adicional (cadastro 3609445264215940):
    # - Se REGIME DE CONTRATAÇÃO == CLT e for a 1ª solicitação, só permitir a partir de 1 ano e 9 meses (21 meses) de empresa.
    try:
        regime = (_colaborador_regime(colaborador_email) or "").strip().upper()
        adm = _colaborador_admissao(colaborador_email)
        if regime == "CLT" and adm:
            resumo_tmp = get_resumo_ferias(colaborador_email)
            if resumo_tmp.get("total_solicitacoes", 0) <= 0:
                liberado_em = _add_months(adm, 21)
                if dt_inicio < liberado_em:
                    return jsonify({
                        "ok": False,
                        "message": f"Para regime CLT, a 1ª solicitação só é permitida a partir de {liberado_em.strftime('%d/%m/%Y')} (1 ano e 9 meses de empresa)."
                    }), 400
    except Exception as _e:
        # se não conseguir validar, não bloqueia (mantém fluxo)
        pass


    try:
        resumo = get_resumo_ferias(colaborador_email)
        dias_novos = (dt_fim - dt_inicio).days + 1
        
        reg_saldo = int(resumo["regular"]["saldo"])
        prem_saldo = int(resumo["premium"]["saldo"])
        
        saldo_tipo_final = saldo_tipo_req
        
        if saldo_tipo_req == "REGULAR":
            if dias_novos <= reg_saldo:
                saldo_tipo_final = "REGULAR"
            elif reg_saldo <= 0 and dias_novos <= prem_saldo:
                saldo_tipo_final = "PREMIUM"
            else:
                return jsonify({"ok": False, "message": f"Saldo insuficiente. Regular: {reg_saldo} dias, Premium: {prem_saldo} dias."}), 400
        else:
            if dias_novos > prem_saldo:
                return jsonify({"ok": False, "message": f"Saldo Premium insuficiente: {prem_saldo} dias."}), 400
            saldo_tipo_final = "PREMIUM"

    except Exception as e:
        return jsonify({"ok": False, "message": f"Erro ao validar saldo de férias: {e}"}), 500

    if saldo_tipo_final == "PREMIUM":
        ok_cert, msg_cert = validar_licenca_certariana(colaborador_email, dias_novos)
        if not ok_cert:
            return jsonify({"ok": False, "message": msg_cert}), 400

    # saldo base usado para o cálculo final
    saldo_base = reg_saldo if saldo_tipo_final == "REGULAR" else prem_saldo

    # grava marcador no texto (sem duplicar)
    marker = f"Saldo: {saldo_tipo_final}"
    if marker.lower() not in (observacoes or "").lower():
        observacoes = (observacoes + ("\n" if observacoes else "") + marker).strip()


    def add_cell(cells, col_id, value):
        if isinstance(col_id, int) and col_id > 0:
            cells.append({"column_id": col_id, "value": value})

    try:
        client = get_smartsheet_client()
        sheet_sol = _get_sheet_solicitacoes(client)
        
        # Colunas robustas
        col_email = _col_id_by_name(sheet_sol, "EMAIL", "COLABORADOR", "E-MAIL")
        col_colab = _col_id_by_name(sheet_sol, "COLABORADOR", "EMAIL")
        col_gestor = _col_id_by_name(sheet_sol, "GESTOR SOLICITANTE", "GESTOR", "GESTOR DIRETO")
        col_criado_por = _col_id_by_name(sheet_sol, "CRIADO_POR", "Criado_por", "CRIADO POR", "CRIADO-POR")
        col_solic = _col_id_by_name(sheet_sol, "SOLICITAÇÃO", "SOLICITACAO")
        col_inicio = _col_id_by_name(sheet_sol, "DATA INICIO", "DATA INÍCIO", "DATA INICIAL")
        col_fim = _col_id_by_name(sheet_sol, "DATA FIM", "DATA FINAL")
        col_dias = _col_id_by_name(sheet_sol, "DIAS")
        col_status = _col_id_by_name(sheet_sol, "STATUS")
        col_obs = _col_id_by_name(sheet_sol, "OBSERVAÇÕES", "OBSERVACOES", "OBSERVAÇÃO", "OBSERVACAO")

        new_row = smartsheet.models.Row()
        new_row.to_top = True
        new_row.cells = []

        # Compatibilidade: grava o email do colaborador nos campos comuns
        add_cell(new_row.cells, col_email, colaborador_email)
        add_cell(new_row.cells, col_colab, colaborador_email)
        add_cell(new_row.cells, col_gestor, gestor_email)
        add_cell(new_row.cells, col_criado_por, gestor_email)
        add_cell(new_row.cells, col_solic, tipo_solicitacao_out)

        add_cell(new_row.cells, col_inicio, data_inicio_str)
        add_cell(new_row.cells, col_fim, data_fim_str)
        add_cell(new_row.cells, col_dias, dias_novos)
        add_cell(new_row.cells, col_status, "PENDENTE")

        # Observações (opcional) -> coluna OBSERVAÇÕES
        add_cell(new_row.cells, col_obs, observacoes)

        client.Sheets.add_rows(ID_FOLHA_SOLICITACOES, [new_row])
        _invalidate_sheet_cache(ID_FOLHA_SOLICITACOES)
    except Exception as e:
        return jsonify({"ok": False, "message": f"Erro ao salvar solicitação: {e}"}), 500

    saldo_atualizado = saldo_base - dias_novos

    return jsonify({
        "ok": True,
        "message": f"Solicitação registrada ({tipo_solicitacao_out}) com {dias_novos} dia(s). Saldo restante: {saldo_atualizado}.",
        "saldo_atualizado": saldo_atualizado
    })

@app.route("/api/editar-solicitacao", methods=["POST"])
def api_editar_solicitacao():
    user = session.get("user")
    if not user:
        return jsonify({"ok": False, "message": "Não autenticado."}), 401
    
    email = user.get("email")
    row_id = request.form.get("row_id")
    data_inicio_str = request.form.get("data_inicio")
    data_fim_str = request.form.get("data_fim")
    
    if not row_id or not data_inicio_str or not data_fim_str:
        return jsonify({"ok": False, "message": "Parâmetros obrigatórios."}), 400
    
    dt_inicio_novo = dt.datetime.strptime(data_inicio_str, "%Y-%m-%d").date()
    dt_fim_novo = dt.datetime.strptime(data_fim_str, "%Y-%m-%d").date()
    
    if dt_fim_novo < dt_inicio_novo:
        return jsonify({"ok": False, "message": "Data fim não pode ser menor que data início."}), 400
    
    ok_periodo, msg = periodo_permitido(dt_inicio_novo, dt_fim_novo, requester_email=email)
    if not ok_periodo:
        return jsonify({"ok": False, "message": msg}), 400
    
    try:
        client = get_smartsheet_client()
        sheet_sol = _get_sheet_solicitacoes(client)
        cols_sol = get_col_map(sheet_sol)
        
        row_id_int = int(row_id)
        row_antiga = next((r for r in sheet_sol.rows if r.id == row_id_int), None)
        
        if not row_antiga:
            return jsonify({"ok": False, "message": "Solicitação não encontrada."}), 404

        row_email_antigo = next(
            (c.value for c in row_antiga.cells if c.column_id == cols_sol.get("COLABORADOR", cols_sol.get("EMAIL", -1))),
            None
        ) or next(
            (c.value for c in row_antiga.cells if c.column_id == cols_sol.get("EMAIL", -1)),
            None
        )
        colaborador_email = safe_lower(row_email_antigo or email)

        status_atual = next(
            (c.value for c in row_antiga.cells if c.column_id == cols_sol.get("STATUS", -1)),
            ""
        )
        
        if safe_lower(status_atual) != "pendente":
            return jsonify({"ok": False, "message": "Só é possível editar solicitações com status Pendente."})

        dias_antigos = next(
            (c.value for c in row_antiga.cells if c.column_id == cols_sol.get("DIAS", -1)),
            0
        ) or 0
        try:
            dias_antigos = int(float(dias_antigos))
        except Exception:
            dias_antigos = 0

        obs_antiga = next(
            (c.value for c in row_antiga.cells if c.column_id == cols_sol.get("OBSERVAÇÕES", cols_sol.get("OBSERVACOES", -1))),
            ""
        ) or ""
        tipo_antigo = next(
            (c.value for c in row_antiga.cells if c.column_id == cols_sol.get("SALDO TIPO", cols_sol.get("TIPO SALDO", -1))),
            ""
        ) or ""
        saldo_tipo_antigo = _infer_saldo_tipo(obs_antiga, tipo_antigo)

        resumo = get_resumo_ferias(colaborador_email)
        dias_novos = (dt_fim_novo - dt_inicio_novo).days + 1

        if saldo_tipo_antigo == "PREMIUM":
            saldo_atual = int(resumo["premium"]["saldo"])
        else:
            saldo_atual = int(resumo["regular"]["saldo"])

        saldo_ajustado = saldo_atual + dias_antigos
        
        if dias_novos > saldo_ajustado:
            return jsonify({
                "ok": False,
                "message": f"Saldo insuficiente após ajuste. Você tem {saldo_ajustado} dia(s) disponível(is)."
            })

        if saldo_tipo_antigo == "PREMIUM":
            ok_cert, msg_cert = validar_licenca_certariana(
                colaborador_email,
                dias_novos,
                exclude_row_id=row_id_int,
                include_statuses={"pendente", "em análise", "aprovada"}
            )
            if not ok_cert:
                return jsonify({"ok": False, "message": msg_cert}), 400
        
        row_update = smartsheet.models.Row()
        row_update.id = row_id_int
        row_update.cells = [
            {"column_id": cols_sol.get("DATA INICIO", -1), "value": data_inicio_str},
            {"column_id": cols_sol.get("DATA FIM", -1), "value": data_fim_str},
            {"column_id": cols_sol.get("DIAS", -1), "value": dias_novos},
        ]
        
        client.Sheets.update_rows(ID_FOLHA_SOLICITACOES, [row_update])
        _invalidate_sheet_cache(ID_FOLHA_SOLICITACOES)
        
        saldo_final = saldo_ajustado - dias_novos
    except Exception as e:
        print(f"ERRO em api_editar_solicitacao: {e}")
        return jsonify({"ok": False, "message": f"Erro ao editar solicitação: {e}"}), 500
    
    return jsonify({
        "ok": True,
        "message": f"Solicitação atualizada para {dias_novos} dia(s). Saldo restante: {saldo_final}.",
        "saldo_atualizado": saldo_final
    })

if __name__ == "__main__":
    # Em produção (Render), use PORT e host 0.0.0.0
    port = int(os.getenv("PORT", "5000"))
    debug = os.getenv("FLASK_DEBUG", "1") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
