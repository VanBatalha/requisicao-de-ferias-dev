# ✅ Checklist de Implementação - PostgreSQL

## 📋 Pré-requisitos

- [ ] Banco PostgreSQL criado no Render (ou local)
- [ ] URL do banco copiada (Internal URL para Render)
- [ ] Credenciais em mãos (usuário, senha, host, porta)

---

## 🛠️ Passos de Implementação

### Passo 1: Preparar o Render

- [ ] Criar novo serviço **PostgreSQL** no Render
  - Ir em https://render.com → Dashboard → **New +** → **PostgreSQL**
  - Nome: `ferias-app-db`
  - Database: `ferias_app`
- [ ] Copiar **Internal Database URL**
  - Formato: `postgresql://usuario:senha@host:5432/ferias_app`

### Passo 2: Configurar Variáveis de Ambiente

No seu Web Service do Render:

- [ ] Adicionar variável `DATABASE_URL`
  - Valor: Cole a URL do banco
- [ ] Adicionar variável `FLASK_SECRET_KEY`
  - Valor: Qualquer string longa (ex: `abc123def456...`)
- [ ] Adicionar variáveis LDAP (se usar):
  - `LDAP_URI`
  - `LDAP_BASE_DN`
  - `LDAP_BIND_DN`
  - `LDAP_BIND_PASSWORD`

### Passo 3: Deploy da Aplicação

- [ ] Fazer push do código atualizado para o repositório
  - Arquivos novos: `models.py`, `postgres_service.py`, `db_adapter.py`
  - Arquivos alterados: `config.py`, `requirements.txt`, `__init__.py`
- [ ] Trigger manual deploy no Render
  - Dashboard → Web Service → **Manual Deploy**
- [ ] Aguardar build completar (5-10 minutos)
  - Verif

icar logs: tudo ok? ✅

### Passo 4: Verificar Conexão

- [ ] Acessar: `https://seu-app.render.com/api/dp/colaboradores`
- [ ] Resposta esperada:
  ```json
  {
    "ok": true,
    "colaboradores": []
  }
  ```
- [ ] Se receber erro: verificar logs do Render

### Passo 5: Importar Dados

- [ ] Baixar arquivo `import_data.py` do repositório
- [ ] Executar localmente:
  ```bash
  python import_data.py \
    "postgresql://usuario:senha@host.render.com:5432/ferias_app" \
    export_ferias_app.xlsx
  ```
- [ ] Verif resultado:
  ```
  ✅ 385 colaboradores no banco
  ✅ 385 complementos
  ✅ 688 solicitações
  ```

### Passo 6: Testar Endpoints Principais

- [ ] `GET /api/dp/colaboradores` → Retorna lista
- [ ] `GET /api/dp/saldos/email@empresa.com.br` → Retorna saldos
- [ ] `GET /api/dp/solicitacoes` → Retorna lista
- [ ] Fazer login via LDAP (se configurado)
- [ ] Criar nova solicitação

---

## 📋 Arquivos da Migração

### Novos Arquivos Criados:

```
requisicao-de-ferias-dev-main/
├── ferias_app/
│   ├── models.py                    ✨ NEW - Modelos SQLAlchemy
│   └── services/
│       ├── postgres_service.py      ✨ NEW - Serviço PostgreSQL
│       └── db_adapter.py            ✨ NEW - Adapter para compatibilidade
│
├── import_data.py                   ✨ NEW - Script de importação
├── .env.example                     ✨ NEW - Template de variáveis
├── MIGRACAO_POSTGRESQL.md           ✨ NEW - Instruções detalhadas
└── README_POSTGRESQL.md             ✨ NEW - README
```

### Arquivos Modificados:

```
requisicao-de-ferias-dev-main/
├── ferias_app/
│   ├── config.py                    🔄 UPDATED - Adicionado DATABASE_URL
│   ├── __init__.py                  🔄 UPDATED - Init PostgreSQL
│   └── requirements.txt             🔄 UPDATED - SQLAlchemy + psycopg2
```

---

## 🚀 Variáveis de Ambiente Mínimas

```bash
# OBRIGATÓRIO
DATABASE_URL=postgresql://usuario:senha@host:5432/ferias_app
FLASK_SECRET_KEY=chave_muito_longa_aqui

# OPCIONAL (mas recomendado)
FLASK_ENV=production
LDAP_URI=ldap://seu_servidor:389
LDAP_BASE_DN=dc=empresa,dc=local
LDAP_BIND_DN=cn=usuario,ou=Users,dc=empresa,dc=local
LDAP_BIND_PASSWORD=senha_ldap
```

---

## 🔍 Validação

Após completar todos os passos:

- [ ] App iniciado sem erros
- [ ] Banco conectado (verificar logs)
- [ ] Tabelas criadas automaticamente
- [ ] Dados importados (385 colabs, 688 solicitações)
- [ ] Endpoints retornam dados
- [ ] Login funciona
- [ ] Pode criar/atualizar solicitações
- [ ] Saldos são exibidos corretamente

---

## ⚠️ Problemas Comuns

| Problema | Solução |
|----------|---------|
| "DATABASE_URL not configured" | Adicionar variável no Render + redeploy |
| "could not translate host name" | Usar URL **Internal** do Render |
| "table does not exist" | Rodar app uma vez (cria tables) |
| "no rows matched" | Executar `import_data.py` |
| 401 Unauthorized | Verificar LDAP ou autenticação |
| Saldos zerados | Importar dados com `import_data.py` |

---

## 📞 Próximos Passos

1. ✅ Implementar de acordo com checklist acima
2. ✅ Testar todas as funcionalidades
3. ✅ Monitorar logs no Render por 24h
4. ✅ Fazer backup do banco PostgreSQL
5. ✅ Remover Smartsheet credentials do app (opcional, deixar como fallback)

---

## 🎉 Conclusão

Ao completar este checklist, seu app estará:
- ✅ Usando PostgreSQL como base de dados
- ✅ Independente do Smartsheet
- ✅ Pronto para produção
- ✅ Com dados migrados
- ✅ Com auditoria funcional

**Status Final: MIGRAÇÃO COMPLETA** ✅

Data: 2026-06-01
Versão: 2.0
