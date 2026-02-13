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

# USER TYPE: refresh rápido para refletir mudanças de permissão no Smartsheet
USER_TYPE_SOFT_REFRESH_COOLDOWN = int(os.getenv("USER_TYPE_SOFT_REFRESH_COOLDOWN", "5"))
USER_TYPE_SOFT_REFRESH_LAST = 0.0

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

        # Também invalida o cache por-request (Flask.g) quando existir.
        # Isso é importante quando fazemos escrita e, no mesmo request,
        # recalculamos saldos lendo novamente a planilha.
        try:
            if sheet_id in (None, ID_FOLHA_SOLICITACOES):
                if hasattr(g, "_sheet_solicitacoes"):
                    delattr(g, "_sheet_solicitacoes")
        except Exception:
            pass
    except Exception:
        pass


def get_smartsheet_client(force_user_token: bool = False):
    """Cria cliente Smartsheet.

    Prioriza token de serviço (env) para que permissões do app não dependam do OAuth do usuário.
    Mantém OAuth como fallback (ex.: ambiente local sem token de serviço).
    """
    service_token = (
        os.getenv("SMARTSHEET_SERVICE_TOKEN")
        or os.getenv("SMARTSHEET_API_TOKEN")
        or os.getenv("SMARTSHEET_ACCESS_TOKEN")
    )
    if service_token and not force_user_token:
        return smartsheet.Smartsheet(service_token)

    access_token = session.get("access_token")
    if access_token:
        return smartsheet.Smartsheet(access_token)
    return None

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
      


def ensure_primary_cell(sheet, row, value):
    """Garante que a coluna primária do Smartsheet receba um valor.
    Se a planilha tiver como coluna primária algo diferente (ex.: antes era EMAIL),
    o Smartsheet pode rejeitar a inserção ou criar linhas confusas.
    """
    try:
        if not sheet or not getattr(sheet, "columns", None) or not row:
            return
        primary = next((c for c in sheet.columns if getattr(c, "primary", False)), None)
        if not primary or not getattr(primary, "id", None):
            return
        pid = primary.id
        # se já existe célula para a coluna primária, não faz nada
        for cell in (row.cells or []):
            if isinstance(cell, dict) and cell.get("column_id") == pid:
                return
            try:
                if getattr(cell, "column_id", None) == pid:
                    return
            except Exception:
                pass
        # insere no começo para ficar visível mesmo com to_top
        row.cells = ([{"column_id": pid, "value": (value or "").strip() or "-"}] + (row.cells or []))
    except Exception:
        return


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
    """Normaliza títulos/labels para comparação de colunas.

    - remove acentos
    - trata _, hífen e pontuação como separadores
    - lower + colapsa espaços
    """
    s = "" if s is None else str(s)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    # separadores comuns em títulos de coluna
    s = re.sub(r"[\_\-]+", " ", s)
    # remove pontuação (mantém letras/números/espaço)
    s = re.sub(r"[^0-9a-zA-Z\s]+", " ", s)
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
    """Define se a linha é REGULAR ou PREMIUM (Licença Certariana).

    Mantém compatibilidade com registros antigos ("Premium").
    """
    exp = (_norm_title(explicit) if explicit else "")
    if exp in (
        "premium", "licenca premium",
        "licenca certariana", "licença certariana", "certariana",
        "licenca certareana", "licença certareana",
    ):
        return "PREMIUM"
    if exp in ("regular", "ferias", "férias", "ferias regulares", "férias regulares"):
        return "REGULAR"

    o = _norm_title(obs or "")
    if (
        "saldo: premium" in o or "saldo premium" in o or "[premium]" in o or "premium" in o
        or "saldo: certariana" in o or "saldo certariana" in o or "[certariana]" in o or "certariana" in o
    ):
        return "PREMIUM"
    return "REGULAR"

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
def _listar_segmentos_premium(email: str, win_start: dt.date, win_end: dt.date, exclude_row_id: int | None = None):
    """Lista segmentos (dias) já lançados/pendentes de LICENÇA CERTARIANA (PREMIUM) dentro da janela atual."""
    sheet = _get_sheet_solicitacoes()
    col_email = col_id_by_name(sheet, "COLABORADOR", "EMAIL", "EMAIL DO COLABORADOR", "EMAIL DA EMPRESA")
    col_saldo = col_id_by_name(sheet, "SALDO TIPO", "SALDO", "TIPO SALDO")
    col_dias = col_id_by_name(sheet, "DIAS", "DIAS (GOZO)", "DIAS GOZO")
    col_status = col_id_by_name(sheet, "STATUS")
    col_ini = col_id_by_name(sheet, "DATA INICIO", "DATA INÍCIO", "INICIO", "INÍCIO")
    col_sol = col_id_by_name(sheet, "SOLICITAÇÃO", "SOLICITACAO", "TIPO", "TIPO SOLICITACAO")

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
        if saldo_n != "premium":
            continue

        sol = str(_cell_value(row, col_sol) or "")
        if "certar" not in _norm(sol):
            continue
        if "ajuste" in _norm(sol):
            continue

        st = _canonical_status(str(_cell_value(row, col_status) or ""))
        stn = _norm_status(st)
        if stn not in STATUS_APROVADA and stn not in STATUS_RESERVA:
            continue

        dt_ini = _parse_date(_cell_value(row, col_ini))
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

            # ===== SOLICITAÇÕES DE FÉRIAS =====
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

            dados.append((row.id, row_email_n, inicio_br, fim_br, dias, status, (solicit or ""), saldo_tipo, (obs or "")))

        return dados
    except Exception as e:
        print(f"ERRO em listar_solicitacoes_todas: {e}")
        return []

def periodo_permitido(dt_inicio, dt_fim, requester_email: str | None = None):
    """Valida regras de período de férias.

    Regras (aplicadas APENAS para USER):
      - Não permite mês vigente (exceto liberação excepcional)
      - Não permite mês seguinte após o dia 21
      - Não permite passado / dia vigente

    Observação:
      - DP e Administrador podem solicitar/registrar em qualquer data.
    """
    hoje = dt.date.today()

    # DP/Admin podem tudo (sem travas de data)
    if requester_email:
        try:
            if tem_grupo(requester_email, "DP") or tem_grupo(requester_email, "Administrador"):
                return True, ""
        except Exception:
            pass

    # USER: bloqueios
    if dt_fim < hoje or dt_inicio <= hoje:
        return False, "Nao eh permitido solicitar ou editar ferias no mes vigente ou no passado."

    def ym(d):
        return (d.year, d.month)

    ym_hoje = ym(hoje)

    if ym(dt_inicio) == ym_hoje or ym(dt_fim) == ym_hoje:
        if requester_email and _same_month_override_allowed(requester_email):
            pass
        else:
            return False, "Nao eh permitido solicitar ou editar ferias no mes vigente."

    # Após o dia 21, bloqueia mês seguinte (USER)
    if 21 <= hoje.day <= 31:
        prox_ano = hoje.year + 1 if hoje.month == 12 else hoje.year
        prox_mes = 1 if hoje.month == 12 else hoje.month + 1
        nym_prox = (prox_ano, prox_mes)

        if ym(dt_inicio) == nym_prox or ym(dt_fim) == nym_prox:
            return False, "Nao eh permitido solicitar ou editar ferias do mes seguinte apos o dia 21."

    return True, ""

# ============================================
# ROTAS WEB
# ============================================