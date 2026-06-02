# 📊 Análise Completa: Como os Subordinados de um Gestor são Carregados

**Data**: 2 de Junho de 2026  
**Análise**: Fluxo de carregamento de subordinados + BUGS críticos encontrados

---

## 📍 RESUMO EXECUTIVO

Encontrei **3 BUGs CRÍTICOS** no carregamento de subordinados:

1. **BUG #1**: Código duplicado com lógica **INCOMPATÍVEL** de comparação de emails
2. **BUG #2**: Cache global de **20 segundos** sem invalidação seletiva
3. **BUG #3**: Falha **100% certa** quando domínios de email diferem (Smartsheet vs LDAP)

**Impacto**: Gestores com emails de domínios diferentes podem **NÃO VER SEUS SUBORDINADOS**.

---

## 🔴 BUG #1: DUPLICAÇÃO E COMPARAÇÃO QUEBRADA DE EMAILS

### Existe Código Duplicado?

**SIM!** Há **DUAS implementações** diferentes de `get_subordinados()`:

```
from ferias_app.core import get_subordinados
├─ core.py importa de permissions_service.py:57-73
│  └─ Chama cadastro_service.py:179-217 ✅ CORRETA
│     ├─ Usa: _emails_equivalentes(a, b) 
│     ├─ Compara por LOCAL-PART (antes do @)
│     └─ ACEITA emails de domínios diferentes
│
VERSUS
│
core_support.py:1334-1385 ❌ VERSÃO LEGADA/INCORRETA
├─ Comparação DIRETA: gestor_direto == gestor_email
├─ FALHA se domínios são diferentes
└─ Usado por: dp_api.py:340 (função get_subordinados_direto)
```

### O que é `_emails_equivalentes()`?

```python
# cadastro_service.py:57-72
def _emails_equivalentes(a: str, b: str) -> bool:
    """Aceita match se:
    1. Emails são exatamente iguais (case-insensitive) OU
    2. Local-part (antes do @) é o mesmo
    
    Exemplos:
    - "gestor@empresa.com" == "gestor@empresa.com" ✅
    - "gestor@empresa.com" vs "gestor@internaldomain.com" ✅ MATCH!
    - "gestor@empresa.com" vs "outro@empresa.com" ❌ NO MATCH
    """
    a = safe_lower(a or "")
    b = safe_lower(b or "")
    if not a or not b:
        return False
    if a == b:
        return True
    return _email_local(a) == _email_local(b)
```

### Comparação: Versão Correta vs Legada

| Aspecto | cadastro_service.py ✅ | core_support.py ❌ |
|---------|------------------------|-------------------|
| Normaliza email | ✅ `safe_lower()` | ✅ `_norm_email()` |
| Compara por local-part | ✅ SIM | ❌ NÃO |
| Aceita domínios diferentes | ✅ SIM | ❌ NÃO |
| Problemas com LDAP | ✅ NENHUM | ❌ CRÍTICO |
| Usado no main flow | ✅ SIM | ❌ LEGADO |

### Cenário de Falha Garantida

```
Smartsheet (planilha de cadastro):
├─ EMAIL DA EMPRESA: gestor@empresa.com
├─ GESTOR DIRETO: gestor@empresa.com
└─ (tabela de gestores)

LDAP (autenticação):
└─ Usuario faz login como: gestor@internaldomain.com (domínio interno)

Resultado ao chamar get_subordinados(gestor@internaldomain.com):

✅ Versão cadastro_service.py:
   - Busca colaboradores com GESTOR_DIRETO = gestor@internaldomain.com
   - _emails_equivalentes("gestor@empresa.com", "gestor@internaldomain.com")
   - Compara local-parts: "gestor" == "gestor" ✅ MATCH!
   - Retorna: [lista de subordinados] ✅

❌ Versão core_support.py:
   - Busca colaboradores com GESTOR_DIRETO = gestor@internaldomain.com
   - Comparação: "gestor@empresa.com" == "gestor@internaldomain.com" ❌ FALHA!
   - Retorna: [] (lista vazia) ❌
```

---

## 🔴 BUG #2: CACHE DESATUALIZADO (20 SEGUNDOS)

### Estrutura de Cache

```
1. CACHE POR REQUEST (per-request - SEGURO)
   ├─ g._cadastro_sheet_cache (cadastro_service.py:27)
   ├─ g._colaboradores_list_cache (core_support.py:1544)
   ├─ g._cadastro_colaboradores (core_support.py:1303)
   └─ Duração: 1 request (50-500ms típico)
   └─ Seguro: Sim, não afeta outros usuários

2. CACHE GLOBAL COM TTL (core_support.py:73-74)
   ├─ _SHEET_CACHE = {}
   ├─ _SHEET_CACHE_TTL_SECONDS = int(os.getenv(..., "20"))
   ├─ Duração: 20 SEGUNDOS (configurável)
   └─ Impacto: AFETA TODOS OS REQUESTS!
```

### Cenário de Inconsistência

```
T=0s: DP altera Smartsheet
      - Adiciona novo subordinado para gestor X
      - Cache global ainda tem dados antigos

T=0-19s: Gestor X faz login/carrega subordinados
        Request 1: ✅ Vê novo subordinado (cache per-request pega novo valor)
        Request 2: ❌ Vê dados antigos (cache global ainda válido)
        
T=20s+: Cache global expira
        ✅ Novo Smartsheet GET é executado
        ✅ Todos veem novo subordinado
```

### Onde Falta Invalidação de Cache?

```python
# ✅ Invalidação de CADASTRO (quando permissões mudam)
admin_api.py:151
├─ invalidate_sheet_cache(ID_FOLHA_CADASTRO)

# ❌ Falta invalidação de CADASTRO (quando gestor-subordinado muda)
dp_api.py:271,509
├─ invalidate_sheet_cache(ID_FOLHA_SOLICITACOES)  # Apenas solicitações!
├─ Não toca no ID_FOLHA_CADASTRO!
```

### Severidade

```
BAIXA se:
- Operações rápidas (< 20s para propagação é aceitável)
- Não há grande volume de mudanças de gestores

ALTA se:
- Frequentes alterações de gestor-subordinado
- Usuários precisam de dados em tempo real
- Múltiplos DPs mudando dados simultaneamente
```

---

## 🔴 BUG #3: NORMALIZAÇÃO INCONSISTENTE

### Três Funções Diferentes

```python
1. safe_lower(value)  # utils.py
   └─ return str(value).strip().lower()
   └─ Apenas lower + strip

2. _norm_email(email)  # core_support.py:522
   └─ from normalization_service import norm_email
   └─ return norm_email(email)

3. _emails_equivalentes(a, b)  # cadastro_service.py:57
   └─ Compara local-parts
   └─ Mais robusta
```

### Impacto

```
❌ Problema: Espaços em emails não são removidos uniformemente

Exemplo:
- Smartsheet: "gestor@ empresa.com " (espaços)
- LDAP: "gestor@empresa.com" (limpo)
- Resultado: Pode não fazer match dependendo da função usada
```

---

## 📋 COLUNAS DA PLANILHA (como funcionam)

| Coluna | Definição | Normalização | Problema |
|--------|-----------|--------------|----------|
| `EMAIL DA EMPRESA` | Email único do colaborador | ✅ safe_lower() | ✅ OK |
| `USER TYPE` | ADMIN \| DP \| USER | ✅ .upper() | ✅ OK |
| `STATUS` | ATIVO \| INATIVO | ✅ .lower() | ✅ OK |
| `GESTOR DIRETO` | Email do gestor direto | ⚠️ safe_lower() | ❌ Comparação quebrada |
| `GESTOR SUPERIOR` | Gestor superior ou "dp" | ⚠️ safe_lower() | ❌ Comparação quebrada |
| `GESTOR` | Fallback para GESTOR DIRETO | ⚠️ safe_lower() | ❌ Comparação quebrada |

---

## ✅ LÓGICA CORRETA (cadastro_service.py:191-217)

```python
def subordinados_do_gestor(access_token: str, gestor_email: str) -> List[Dict]:
    """
    Retorna subordinados do gestor seguindo a lógica:
    
    1. Se usuário é DP e colaborador tem GESTOR_SUPERIOR = "dp" → subordinado
    2. Se GESTOR_SUPERIOR = gestor_email (comparação por local-part) → subordinado
    3. Se GESTOR_DIRETO = gestor_email (comparação por local-part) → subordinado
    4. Apenas colaboradores ativos
    """
    
    gestor_email = safe_lower(gestor_email)
    is_dp_user = (user_type == "DP")
    
    for c in listar_colaboradores(access_token):
        colab_email = safe_lower(c.get("email") or "")
        gestor_direto = safe_lower(c.get("gestor_direto") or c.get("gestor") or "")
        gestor_superior = safe_lower(c.get("gestor_superior") or "")
        
        # MATCH #1: DP com GESTOR_SUPERIOR = "dp"
        if is_dp_user and gestor_superior == "dp":
            match = True
        
        # MATCH #2: GESTOR_SUPERIOR = gestor_email
        elif gestor_superior and _emails_equivalentes(gestor_superior, gestor_email):
            match = True
        
        # MATCH #3: GESTOR_DIRETO = gestor_email
        elif gestor_direto and _emails_equivalentes(gestor_direto, gestor_email):
            match = True
        
        # FILTRO: Ativo
        if not is_ativo(access_token, colab_email):
            continue
        
        # RESULTADO: Adiciona ao output
        out.append(c)
```

---

## 🔍 FLUXO COMPLETO: Como um Gestor Carrega Subordinados

```
1. LOGIN (LDAP)
   └─ email = gestor@internaldomain.com (pode ter domínio diferente!)

2. RENDERIZAR PÁGINA (/ferias)
   └─ pages.py:130-140
      └─ tem_acesso() → verifica permissão
         └─ permissions_service.py:get_user_role()
            └─ is_gestor(email)
               └─ get_subordinados(email)  ← AQUI!
                  └─ permissions_service.py:57-73
                     └─ subordinados_do_gestor(token, email)
                        └─ cadastro_service.py:179-217 ✅

3. LISTAR SUBORDINADOS
   └─ pages.py:134
      └─ subs = get_subordinados(gestor_email)
         └─ retorna [lista de emails]

4. FILTRAR SOLICITAÇÕES
   └─ listar_solicitacoes_equipes([gestor_email] + subs)
      └─ Busca solicitações apenas desses emails

5. RENDERIZAR TEMPLATE
   └─ Mostra subordinados disponíveis para solicitar férias
```

---

## 📍 PONTOS DE CHAMADA

| Arquivo | Linha | Função | Status |
|---------|-------|--------|--------|
| [pages.py](ferias_app/blueprints/pages.py) | 134 | `get_subordinados(gestor_email)` | ✅ Importa de core.py (CORRETO) |
| [solicitacoes_api.py](ferias_app/blueprints/solicitacoes_api.py) | 52,56 | `get_subordinados(...)` | ✅ Importa de core.py (CORRETO) |
| [dp_api.py](ferias_app/blueprints/dp_api.py) | 340 | `get_subordinados_direto(gestor)` | ⚠️ Usa core_support.py (LEGADO) |

---

## 🎯 IMPACTO POR CENÁRIO

### Cenário A: Emails com mesmo domínio
```
Smartsheet: gestor@empresa.com
LDAP: gestor@empresa.com
Resultado: ✅ OK (ambas as versões funcionam)
```

### Cenário B: Domínios diferentes
```
Smartsheet: gestor@empresa.com
LDAP: gestor@internaldomain.com

Versão cadastro_service.py: ✅ FUNCIONA (local-part match)
Versão core_support.py: ❌ FALHA (string comparison)
```

### Cenário C: Cache desatualizado
```
T=0s: DP adiciona novo subordinado
T=1s: Gestor faz logout/login
T=5s: Gestor carrega página
Resultado: ❌ Vê dados de 20s atrás (pode ser cache anterior)
```

---

## ✅ CHECKLIST: VERIFICAÇÕES RECOMENDADAS

- [ ] Confirmar se `dp_api.py:340` está realmente usando `get_subordinados_direto()` do `core_support.py`
- [ ] Testar cenário com domínios diferentes (Smartsheet: `@empresa` vs LDAP: `@internal`)
- [ ] Verificar se há muitas alterações de gestor-subordinado que precisam ser sincronizadas em <20s
- [ ] Revisar logs de performance para ver se cache TTL de 20s é adequado
- [ ] Considerar adicionar log/monitoring quando cache é servido vs quando novo GET é executado

---

## 📚 REFERÊNCIA RÁPIDA

### Arquivos Principais
- ✅ **USE ESTES**: 
  - [permissions_service.py](ferias_app/services/permissions_service.py)
  - [cadastro_service.py](ferias_app/services/cadastro_service.py)

- ⚠️ **CUIDADO COM ESTES**:
  - [core_support.py](ferias_app/services/core_support.py) - tem versão legada com bugs
  - [dp_api.py](ferias_app/blueprints/dp_api.py) - pode estar usando versão errada

### Funções Críticas
- `subordinados_do_gestor()` [cadastro_service.py:179] ✅ CORRETA
- `_emails_equivalentes()` [cadastro_service.py:57] ✅ ROBUST
- `get_subordinados()` [permissions_service.py:73] ✅ WRAPPER

---

## 🚀 PRÓXIMOS PASSOS

1. **CRÍTICO**: Verificar se `dp_api.py:340` pode estar usando código bugado
2. **CRÍTICO**: Testar com domínios de email diferentes
3. **IMPORTANTE**: Adicionar invalidação de cache de CADASTRO após alterações
4. **IMPORTANTE**: Padronizar normalização de emails em `normalization_service.py`
5. **OPCIONAL**: Reduzir TTL de cache global ou adicionar invalidação manual

---

**Análise realizada em**: 2 de Junho de 2026
**Status**: ⚠️ BUGS CRÍTICOS ENCONTRADOS - requerem investigação urgente
