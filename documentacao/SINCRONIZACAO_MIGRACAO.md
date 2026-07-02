# Sincronização e migração

## Scripts principais

```text
sync_cadastro_smartsheet.py
  Sincroniza cadastro, permissões, hierarquia e, quando habilitado, solicitações.

migracao/scripts/recalcular_saldo_periodo.py
  Recria/recalcula saldos na tabela saldo_periodo usando o banco atual.

migracao/scripts/import_data.py
  Script auxiliar de importação histórica.

migracao/scripts/repair_colaborador_complemento.py
  Script auxiliar para reparos pontuais em complemento.
```

## Variáveis usadas na sincronização manual

```env
INCLUDE_SOLICITACOES=true
RECALCULATE_SALDOS=true
SYNC_REFERENCE_DATE=2026-07-01
```

Essas variáveis são recomendadas para execução manual/local, não para o Web Service do Render.

## Web Service do Render

No Render, mantenha apenas variáveis de conexão e funcionamento do app:

```env
DB_TARGET=oficial
PG_HOST=75.119.139.205
PG_PORT=5532
PG_DB=db_appsheet
PG_USER=...
PG_PASSWORD=...
PG_SSLMODE=prefer
DB_SCHEMA=app_ferias
APP_TIMEZONE=America/Fortaleza
SMARTSHEET_ACCESS_TOKEN=...
ID_FOLHA_CADASTRO=3609445264215940
ID_FOLHA_SOLICITACOES=2890766507528068
```

## Observação

O app web não deve rodar migrações pesadas no startup. Migrações e recálculos devem ser feitos por scripts manuais ou rotinas controladas.
