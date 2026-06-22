# App de Requisição de Férias - Migração PostgreSQL ✅

Versão atualizada para usar **PostgreSQL** em vez de Smartsheet como base de dados.

## 🚀 Quick Start

### 1. Variáveis de Ambiente (Render)

Adicione no seu Web Service do Render:

```env
DATABASE_URL=postgresql://user:password@host.render.com:5432/ferias_app
FLASK_ENV=production
FLASK_SECRET_KEY=chave_secreta_muito_longa_aqui
LDAP_URI=ldap://seu_servidor:389
LDAP_BASE_DN=dc=empresa,dc=local
LDAP_BIND_DN=cn=usuario,ou=Users,dc=empresa,dc=local
LDAP_BIND_PASSWORD=senha_ldap
```

### 2. Dependências

```bash
pip install -r requirements.txt
```

Agora inclui:
- `SQLAlchemy>=2.0` - ORM para PostgreSQL
- `psycopg2-binary>=2.9` - Driver PostgreSQL

### 3. Iniciar o App

```bash
python app.py
```

O banco será inicializado automaticamente na primeira execução.

---

## 📊 Estrutura do Banco de Dados

### Tabelas principais:

| Tabela | Descrição | Registros |
|--------|-----------|-----------|
| `colaboradores` | Cadastro base dos colaboradores | 385+ |
| `colaborador_complemento` | Saldos e configs por colaborador | 385+ |
| `solicitacoes` | Todas as solicitações/ajustes | 688+ |
| `admin_configs` | Exceções e regras (admin) | 0 (vazio inicialmente) |
| `auditoria` | Histórico de ações | 0 (vazio inicialmente) |
| `sync_state` | Controle de sincronizações | 3 |

---

## 📥 Importar Dados do Excel

Se você tem dados no arquivo `export_ferias_app.xlsx`:

```bash
python import_data.py \
  "postgresql://user:password@host:5432/ferias_app" \
  export_ferias_app.xlsx
```

**Saída esperada:**
```
✅ 385 colaboradores no banco
✅ 385 complementos no banco
✅ 688 solicitações no banco
✅ 3 sync states no banco
✅ Importação concluída com sucesso!
```

---

## 🔌 Arquitetura - Componentes Principais

### Modelos (`models.py`)
SQLAlchemy models para todas as 7 tabelas:
- Relacionamentos configurados (Colaborador ↔ Complemento ↔ Solicitações)
- Campos JSON para dados flexíveis (flags, metadata, raw_payload)
- Timestamps automáticos (created_at, updated_at)

### Serviço PostgreSQL (`services/postgres_service.py`)
Funções de acesso principal:
- `get_db_session()` - Obtém sessão do banco
- `listar_colaboradores()` - Lista com filtros
- `get_saldos_colaborador(email)` - Saldos por pessoa
- `criar_solicitacao(payload)` - Cria requisição
- `atualizar_solicitacao(id, payload)` - Atualiza requisição
- `listar_solicitacoes()` - Lista com filtros
- `atualizar_saldos_colaborador()` - Recalcula saldos
- `registrar_auditoria()` - Log de auditoria

### Adapter Bridge (`services/db_adapter.py`)
Compatibilidade com código legado:
- `listar_colaboradores_bridge()` - Formato Smartsheet
- `get_resumo_ferias_bridge(email)` - Saldos compatíveis
- `get_subordinados_bridge(gestor)` - Subordinados
- `is_colaborador_ativo_bridge()` - Validação
- Mais...

---

## 🔧 Configuração

### Config.py - Variáveis Suportadas:

```python
settings.database_url         # PostgreSQL URL (OBRIGATÓRIO)
settings.ldap_uri              # URI do LDAP
settings.ldap_base_dn          # Base DN
settings.ldap_bind_dn          # Usuário de bind
settings.ldap_bind_password    # Senha LDAP
# ... mais variáveis LDAP

# Smartsheet (legado - não mais usado)
settings.access_token
settings.id_folha_cadastro
settings.id_folha_solicitacoes
```

---

## 📝 API Endpoints (compatíveis com versão anterior)

### DP - Gestão

```
GET  /api/dp/colaboradores
POST /api/dp/colaboradores
GET  /api/dp/saldos/<email>
GET  /api/dp/solicitacoes
POST /api/dp/solicitacoes
```

### Solicitações

```
POST /api/solicitacoes
GET  /api/solicitacoes/<id>
PUT  /api/solicitacoes/<id>
DELETE /api/solicitacoes/<id>
```

Todos continuam funcionando como antes! ✅

---

## 🔄 Fluxo de Dados

```
┌─────────────────────────┐
│  Frontend (Templates)   │
└───────────┬─────────────┘
            │ JSON/Form
            ▼
┌─────────────────────────┐
│  Blueprint (Routes)     │
│  (dp_api, solicitacoes) │
└───────────┬─────────────┘
            │ Query/Command
            ▼
┌─────────────────────────┐
│  Adapter Bridge         │
│  (db_adapter.py)        │
└───────────┬─────────────┘
            │ ORM Calls
            ▼
┌─────────────────────────┐
│ PostgreSQL Service      │
│ (postgres_service.py)   │
└───────────┬─────────────┘
            │ SQLAlchemy
            ▼
┌─────────────────────────┐
│   PostgreSQL (Render)   │
│   Database              │
└─────────────────────────┘
```

---

## 🚨 Troubleshooting

### "DATABASE_URL not configured"
❌ Variável não adicionada no Render
✅ Solução: Vá em **Settings** → **Environment** e adicione `DATABASE_URL`

### "could not translate host name"
❌ URL inválida
✅ Solução: Use a URL **Internal** fornecida pelo Render

### "table does not exist"
❌ Banco não foi inicializado
✅ Solução: Certifique-se de que o app rodou uma vez (cria tables automaticamente)

### "no rows matched"
❌ Dados não foram importados
✅ Solução: Execute `python import_data.py ...` com o arquivo Excel

---

## 📄 Arquivos Novos / Alterados

### Novos:
- `models.py` - Modelos SQLAlchemy
- `services/postgres_service.py` - Serviço PostgreSQL
- `services/db_adapter.py` - Adapter para compatibilidade
- `import_data.py` - Script para importar Excel
- `MIGRACAO_POSTGRESQL.md` - Instruções detalhadas
- `.env.example` - Template de variáveis

### Alterados:
- `config.py` - Adicionado `DATABASE_URL`
- `requirements.txt` - Adicionado SQLAlchemy e psycopg2
- `__init__.py` - Inicializa banco na startup

---

## 🔐 Segurança

✅ Senhas nunca em código (usar variáveis de ambiente)  
✅ Conexões SSL/TLS para PostgreSQL  
✅ Sanitização de inputs via ORM  
✅ Auditoria automática de ações  

---

## 📞 Suporte

Para questões sobre:
- **PostgreSQL**: Veja documentação do Render
- **SQLAlchemy**: https://docs.sqlalchemy.org
- **App**: Reporte na issue do repositório

---

**Status:** ✅ Pronto para produção em Render
**Versão:** 2.0 (PostgreSQL)
**Data:** 2026-06-01
