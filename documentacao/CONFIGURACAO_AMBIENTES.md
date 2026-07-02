# Configuração de ambientes e bancos

## Arquivos relacionados

- `ferias_app/config.py`: resolve `DB_TARGET`, `DATABASE_URL`, `PG_*` e `TEST_*`.
- `ferias_app/services/postgres_service.py`: cria engine/sessão e inicializa conexão.
- `app.py`: ponto de entrada do Gunicorn no Render.

## Produção / banco oficial

Use no Render Web Service:

```env
DB_TARGET=oficial
PG_HOST=75.119.139.205
PG_PORT=5532
PG_DB=db_appsheet
PG_USER=SEU_USUARIO
PG_PASSWORD=SUA_SENHA
PG_SSLMODE=prefer
DB_SCHEMA=app_ferias
APP_TIMEZONE=America/Fortaleza
SMARTSHEET_ACCESS_TOKEN=SEU_TOKEN
ID_FOLHA_CADASTRO=3609445264215940
ID_FOLHA_SOLICITACOES=2890766507528068
```

O Web Service não precisa destas variáveis de carga manual:

```env
INCLUDE_SOLICITACOES=true
RECALCULATE_SALDOS=true
SYNC_REFERENCE_DATE=2026-06-24
```

Use essas variáveis apenas no ambiente local ou em um job específico de sincronização.

## Banco de teste via URL

```env
DB_TARGET=teste_url
TEST_DATABASE_URL=postgresql://usuario:senha@host:5432/banco?sslmode=require
DB_SCHEMA=app_ferias
```

## Banco de teste em outro schema do mesmo servidor

```env
DB_TARGET=oficial
PG_HOST=75.119.139.205
PG_PORT=5532
PG_DB=db_appsheet
PG_USER=SEU_USUARIO
PG_PASSWORD=SUA_SENHA
PG_SSLMODE=prefer
DB_SCHEMA=app_ferias_teste
```

## Start command recomendado no Render

```bash
gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --timeout 180 --access-logfile - --error-logfile - --log-level info
```
