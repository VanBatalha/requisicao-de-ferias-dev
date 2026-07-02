# Sincronização e migração

## Cadastro

A sincronização de cadastro busca a planilha `1745799836133252` e atualiza o PostgreSQL por matrícula.

Executar localmente:

```bash
python sync_cadastro_smartsheet.py
```

Variáveis principais:

```env
SMARTSHEET_ACCESS_TOKEN=...
ID_FOLHA_CADASTRO_PRINCIPAL=1745799836133252
DB_TARGET=oficial
DB_SCHEMA=app_ferias
APP_TIMEZONE=America/Fortaleza
```

## Painel ADMIN

O botão de sincronização do Painel ADMIN dispara a rotina em background e acompanha o status em `app_ferias.sync_state`.

A rotina do botão não recalcula saldos por padrão e não importa solicitações, salvo se isso for pedido explicitamente por payload interno.

## Sincronização automática

O serviço `auto_sync_service.py` verifica diariamente no fuso `America/Fortaleza`:

- após 12h, se ainda não houve sucesso no dia, dispara sincronização em background;
- se o dia anterior ficou sem sync, o primeiro acesso ao app dispara sincronização em background;
- a navegação do usuário não fica bloqueada.

## Saldos

Saldos não são atualizados a partir da planilha de cadastro. A fonte oficial é `app_ferias.saldo_periodo`.

Para recalcular saldos a partir das solicitações existentes:

```bash
python migracao/scripts/recalcular_saldo_periodo.py
```
