# Instruções de Migração para PostgreSQL

## 1. Configuração no Render

### Criar um serviço PostgreSQL no Render:
1. Acesse https://render.com
2. Vá para **Dashboard** → **New +** → **PostgreSQL**
3. Configure:
   - **Name**: `ferias-app-db`
   - **Database**: `ferias_app`
   - **User**: `usuario` (escolha um nome)
   - **Plan**: Escolha o plano (Starter é suficiente para testes)

### Copiar credenciais:
Após criar, você receberá uma **Internal Database URL** e **External Database URL**.

- **Internal URL** (use esta no app): `postgresql://usuario:senha@host:5432/ferias_app`
- **External URL**: para acesso remoto (ferramentas like DBeaver)

---

## 2. Configurar Variáveis de Ambiente no Render

### No seu Web Service (onde roda o app):

Vá em **Settings** → **Environment Variables** e adicione:

```
DATABASE_URL=postgresql://usuario:senha@host.render.com:5432/ferias_app
FLASK_ENV=production
FLASK_SECRET_KEY=sua_chave_secreta_aqui
LDAP_URI=ldap://seu_servidor_ldap:389
LDAP_BASE_DN=dc=empresa,dc=local
LDAP_BIND_DN=cn=usuario,ou=Users,dc=empresa,dc=local
LDAP_BIND_PASSWORD=senha_ldap
```

---

## 3. Migrar dados do Excel (primeira vez)

Se você quer importar os dados do arquivo `export_ferias_app.xlsx`:

### Opção A: Script Python para importar (executar localmente ou no Render)

```python
import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Configure a URL do banco
DATABASE_URL = "postgresql://usuario:senha@host:5432/ferias_app"
engine = create_engine(DATABASE_URL)

# Ler arquivo Excel
excel_file = "export_ferias_app.xlsx"
xls = pd.ExcelFile(excel_file)

# Importar colaboradores
df_colab = pd.read_excel(excel_file, sheet_name="colaboradores")
df_colab.to_sql("colaboradores", engine, if_exists="append", index=False)

# Importar complementos
df_compl = pd.read_excel(excel_file, sheet_name="colaborador_complemento")
df_compl.to_sql("colaborador_complemento", engine, if_exists="append", index=False)

# Importar solicitações
df_sol = pd.read_excel(excel_file, sheet_name="solicitacoes")
df_sol.to_sql("solicitacoes", engine, if_exists="append", index=False)

print("✅ Dados importados com sucesso!")
```

### Opção B: Carregar dados via psql (linha de comando)

```bash
# Conectar ao banco Render
psql "postgresql://usuario:senha@host.render.com:5432/ferias_app"

# Depois copiar os dados do CSV (se exportar para CSV antes)
\COPY colaboradores FROM 'colaboradores.csv' WITH (FORMAT csv, HEADER true);
```

---

## 4. Estrutura do Banco

As tabelas criadas automaticamente:

- **colaboradores**: Dados base de cada colaborador (385 registros)
- **colaborador_complemento**: Saldos e configs (385 registros)
- **solicitacoes**: Todas as solicitações e ajustes (688 registros)
- **admin_configs**: Exceções/regras (vazio inicialmente)
- **auditoria**: Histórico de ações (vazio inicialmente)
- **sync_state**: Controle de sincronizações (3 registros)

---

## 5. Testar a Conexão

Após deployar no Render, visite: `https://seu-app.render.com/api/dp/colaboradores`

Se receber um JSON com lista de colaboradores, a conexão está ok! ✅

---

## 6. Variáveis de Ambiente Resumo

| Variável | Obrigatório | Exemplo |
|----------|------------|---------|
| `DATABASE_URL` | ✅ Sim | `postgresql://user:pass@host:5432/db` |
| `FLASK_ENV` | ❌ Não | `production` |
| `FLASK_SECRET_KEY` | ✅ Sim | Qualquer string longa |
| `LDAP_URI` | ❌ Não | `ldap://servidor:389` |
| `LDAP_BASE_DN` | ❌ Não | `dc=empresa,dc=local` |

---

## 7. Troubleshooting

### Erro: "DATABASE_URL not configured"
- Verifique se `DATABASE_URL` foi adicionada nas variáveis de ambiente do Render
- Reinicie o app: **Manual Deploy**

### Erro: "could not translate host name"
- Verifique se está usando a URL **Internal** (mesmo servidor) ou **External** (acesso remoto)
- No Render, use a URL **Internal** para melhor performance

### Dados não aparecem
- Os dados precisam ser importados via script
- Veja **Opção A** ou **Opção B** acima

---

## 8. Próximas Etapas

Após verificar que tudo está funcionando:

1. ✅ Copiar a URL do banco do Render
2. ✅ Adicionar `DATABASE_URL` no Render
3. ✅ Fazer deploy do app atualizado
4. ✅ Importar dados do Excel (se necessário)
5. ✅ Testar endpoints da API
