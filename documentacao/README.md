# Documentação do app Gestão de Férias

## Documentos principais

```text
CONFIGURACAO_AMBIENTES.md
  Explica variáveis para banco oficial, banco de teste e Render.

ROTAS_PERFORMANCE.md
  Explica as rotas críticas, especialmente /ferias, e os logs de performance.

SALDOS_MATRICULA.md
  Explica a modelagem atual de saldos usando matrícula e saldo_periodo.

SINCRONIZACAO_MIGRACAO.md
  Explica sincronização manual, migração, recálculo de saldos e scripts relacionados.
```

## Pastas principais

```text
ferias_app/
  Código Python da aplicação Flask.

templates/
  Telas HTML/Jinja.

migracao/
  Scripts e SQLs usados para carga inicial, ajustes e recálculo.

documentacao/historico/
  Documentos antigos preservados apenas para consulta.
```

## Arquivos mais relevantes

```text
app.py
  Entrada do Render/Gunicorn.

sync_cadastro_smartsheet.py
  Script periódico/manual para sincronizar cadastro e solicitações.

ferias_app/blueprints/pages.py
  Rotas de páginas, incluindo /ferias.

ferias_app/blueprints/solicitacoes_api.py
  API de criação de solicitações.

ferias_app/services/postgres_compat_service.py
  Camada PostgreSQL usada pelas telas legadas.

ferias_app/services/smartsheet_sync_service.py
  Rotina de sincronização com Smartsheet.

ferias_app/models.py
  Modelos SQLAlchemy.

templates/ferias.html
  Tela de solicitações de férias.

templates/painel_dp.html
  Painel DP, gestores e ajustes.
```
