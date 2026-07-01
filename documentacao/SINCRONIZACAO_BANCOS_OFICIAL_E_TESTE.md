# Sincronização manual: banco oficial e bancos de teste

Este documento orienta como apontar o app e os scripts de sincronização para o banco oficial ou para um banco de teste.

A partir da V33, o app não precisa depender sempre de `DATABASE_URL`. A variável `DB_TARGET` define qual conjunto de conexão será usado.

## 1. Como o app escolhe o banco

Ordem recomendada:

| `DB_TARGET` | Uso | Variáveis usadas |
|---|---|---|
| `oficial` | Banco oficial PostgreSQL/pgAdmin | `PG_HOST`, `PG_PORT`, `PG_DB`, `PG_USER`, `PG_PASSWORD`, `PG_SSLMODE` |
| `teste_url` | Banco de teste via URL, exemplo Render Internal/External Database URL | `TEST_DATABASE_URL` |
| `teste_pg` | Banco de teste com host/porta/usuário/senha separados | `TEST_PG_HOST`, `TEST_PG_PORT`, `TEST_PG_DB`, `TEST_PG_USER`, `TEST_PG_PASSWORD`, `TEST_PG_SSLMODE` |
| `database_url` | Comportamento legado | `DATABASE_URL` |

Se `DB_TARGET` ficar vazio, o app mantém compatibilidade com versões antigas: usa `DATABASE_URL` se existir; se não existir, tenta usar `PG_*`.

## 2. `.env` para sincronizar no banco oficial

Use este modelo na raiz do projeto:

```env
DB_TARGET=oficial

PG_HOST=75.119.139.205
PG_PORT=5532
PG_DB=db_appsheet
PG_USER=SEU_USUARIO_DO_POSTGRES
PG_PASSWORD=SUA_SENHA_DO_POSTGRES
# Use prefer quando o servidor aceitar com ou sem SSL. Use require se o servidor exigir SSL.
PG_SSLMODE=prefer

DB_SCHEMA=app_ferias
APP_TIMEZONE=America/Fortaleza

SMARTSHEET_ACCESS_TOKEN=SEU_TOKEN_DO_SMARTSHEET
ID_FOLHA_CADASTRO=3609445264215940
ID_FOLHA_SOLICITACOES=2890766507528068
INCLUDE_SOLICITACOES=true
RECALCULATE_SALDOS=true
SYNC_REFERENCE_DATE=2026-06-24
```

Observações:

- `PG_USER` e `PG_PASSWORD` precisam ser preenchidos com o usuário real do PostgreSQL.
- `DB_SCHEMA=app_ferias` cria/usa o schema oficial do app dentro do banco `db_appsheet`.
- Se quiser fazer um teste no mesmo servidor oficial, mas separado dos dados reais, use outro schema, por exemplo `DB_SCHEMA=app_ferias_teste`.

## 3. Primeira sincronização manual no banco oficial

No PowerShell, dentro da pasta do app:

```powershell
.\.venv\Scripts\Activate.ps1
python sync_cadastro_smartsheet.py
```

Esse comando faz:

1. cria o schema e as tabelas, se ainda não existirem;
2. sincroniza colaboradores;
3. sincroniza permissões e hierarquia;
4. importa solicitações/ajustes da folha `2890766507528068`, se `INCLUDE_SOLICITACOES=true`;
5. recalcula saldos e períodos, se `RECALCULATE_SALDOS=true`.

Ao final, o resultado esperado é algo semelhante a:

```text
Sincronização concluída.
{'ok': True, ...}
```

Depois da sincronização, valide no pgAdmin com os scripts de validação que estão em:

```text
migracao/sql
```

Principais validações:

```text
validacao_v29_saldo_periodo.sql
validacao_v30_gestores_matricula.sql
validacao_v31_saldos_autocomplete.sql
```

## 4. Recalcular apenas saldos no banco atual

Quando as solicitações já foram importadas e você só quer recriar `saldo_periodo` e preencher `solicitacoes_ferias.periodo_aquisitivo_origem`, rode:

```powershell
python migracao/scripts/recalcular_saldo_periodo.py
```

Esse script usa o banco definido no `.env`, respeitando `DB_TARGET` e `DB_SCHEMA`.

## 5. Usar banco de teste via URL do Render

Para usar um banco de teste do Render, coloque no `.env`:

```env
DB_TARGET=teste_url
TEST_DATABASE_URL=postgresql://usuario:senha@host-interno-ou-externo:5432/database
DB_SCHEMA=app_ferias
APP_TIMEZONE=America/Fortaleza

SMARTSHEET_ACCESS_TOKEN=SEU_TOKEN_DO_SMARTSHEET
ID_FOLHA_CADASTRO=3609445264215940
ID_FOLHA_SOLICITACOES=2890766507528068
INCLUDE_SOLICITACOES=true
RECALCULATE_SALDOS=true
SYNC_REFERENCE_DATE=2026-06-24
```

Para Render, normalmente:

- no serviço hospedado dentro do Render, prefira a **Internal Database URL**;
- na máquina local, se for acessar de fora do Render, use a **External Database URL** com `sslmode=require`, se necessário.

## 6. Usar banco de teste com host/porta separados

Se o banco de teste tiver host, porta e usuário próprios:

```env
DB_TARGET=teste_pg

TEST_PG_HOST=IP_OU_HOST_DO_TESTE
TEST_PG_PORT=5532
TEST_PG_DB=db_appsheet_teste
TEST_PG_USER=USUARIO_TESTE
TEST_PG_PASSWORD=SENHA_TESTE
TEST_PG_SSLMODE=prefer

DB_SCHEMA=app_ferias
APP_TIMEZONE=America/Fortaleza

SMARTSHEET_ACCESS_TOKEN=SEU_TOKEN_DO_SMARTSHEET
ID_FOLHA_CADASTRO=3609445264215940
ID_FOLHA_SOLICITACOES=2890766507528068
INCLUDE_SOLICITACOES=true
RECALCULATE_SALDOS=true
SYNC_REFERENCE_DATE=2026-06-24
```

## 7. Usar o mesmo banco, mas em schema de teste

Essa opção é útil quando o servidor e database são os mesmos do oficial, mas você quer isolar os dados em outro schema:

```env
DB_TARGET=oficial
PG_HOST=75.119.139.205
PG_PORT=5532
PG_DB=db_appsheet
PG_USER=SEU_USUARIO_DO_POSTGRES
PG_PASSWORD=SUA_SENHA_DO_POSTGRES
PG_SSLMODE=prefer

DB_SCHEMA=app_ferias_teste
```

Depois rode:

```powershell
python sync_cadastro_smartsheet.py
```

O app criará as tabelas dentro de `app_ferias_teste`, sem mexer no schema `app_ferias`.

## 8. Fazer o app de teste apontar para teste ou oficial

O app e os scripts usam a mesma regra de conexão. Portanto, no Render ou no ambiente local:

### App de produção apontando para oficial

Configure as variáveis do serviço de produção assim:

```env
DB_TARGET=oficial
PG_HOST=75.119.139.205
PG_PORT=5532
PG_DB=db_appsheet
PG_USER=SEU_USUARIO_DO_POSTGRES
PG_PASSWORD=SUA_SENHA_DO_POSTGRES
PG_SSLMODE=prefer
DB_SCHEMA=app_ferias
```

### App de teste apontando para banco teste do Render

Configure as variáveis do serviço de teste assim:

```env
DB_TARGET=teste_url
TEST_DATABASE_URL=postgresql://usuario:senha@host-interno-render:5432/database
DB_SCHEMA=app_ferias
```

### App de teste apontando para o banco oficial

É possível, mas use com cuidado. Configure:

```env
DB_TARGET=oficial
PG_HOST=75.119.139.205
PG_PORT=5532
PG_DB=db_appsheet
PG_USER=SEU_USUARIO_DO_POSTGRES
PG_PASSWORD=SUA_SENHA_DO_POSTGRES
PG_SSLMODE=prefer
DB_SCHEMA=app_ferias
```

Recomendação: para testes de layout, use banco de teste. Aponte o app de teste para o oficial apenas quando precisar validar comportamento com dados reais.

## 9. Variáveis antigas

`DATABASE_URL` ainda funciona, mas agora é melhor usar `DB_TARGET` para evitar sincronizar acidentalmente no banco errado.

Para evitar confusão em testes locais, você pode limpar variáveis antigas no PowerShell:

```powershell
Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue
Remove-Item Env:TEST_DATABASE_URL -ErrorAction SilentlyContinue
Remove-Item Env:DB_TARGET -ErrorAction SilentlyContinue
```

Depois mantenha a configuração desejada apenas no arquivo `.env`.
