# Sincronizacao e migracao

## Sincronizacao Smartsheet -> PostgreSQL

Fonte principal: `ferias_app/services/smartsheet_sync_service.py`.

Regras da V44:

- Matricula e a chave oficial.
- E-mail nao cria vinculo de hierarquia.
- Linhas do Smartsheet com `STATUS = #NO MATCH` sao ignoradas e nao entram no banco.
- Saldos nao ficam em `colaborador_complemento`; a fonte oficial e `saldo_periodo`.

## Formas de executar

1. Painel ADMIN: botao `Sincronizar cadastro, permissoes e hierarquia`.
2. Local/manual:

```powershell
python sync_cadastro_smartsheet.py
```

3. Automatico: `ferias_app/services/auto_sync_service.py`.

A sincronizacao automatica verifica o fuso `APP_TIMEZONE`, padrao `America/Fortaleza`. Depois de 12h, se ainda nao houve sincronizacao com sucesso no dia, ela dispara em background. Se o dia anterior foi perdido, o primeiro acesso ao app tambem dispara em background.

## SQL da V44

- `migracao/sql/v44_hierarquia_matricula_sem_email_custom.sql`
- `migracao/sql/validacao_v44_hierarquia_matricula.sql`
