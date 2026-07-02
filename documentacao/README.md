# Gestao de Ferias - indice tecnico

Versao atual: V44.

## Documentos principais

- `CONFIGURACAO_AMBIENTES.md`: variaveis de ambiente para Render/local e perfis de banco.
- `ROTAS_PERFORMANCE.md`: fluxo da rota `/ferias`, logs `FERIAS_PERF` e consultas leves.
- `SALDOS_MATRICULA.md`: regra oficial de saldos usando `saldo_periodo`.
- `SINCRONIZACAO_MIGRACAO.md`: sincronizacao Smartsheet -> PostgreSQL, importacao local e scripts SQL.
- `HIERARQUIA_MATRICULA.md`: regra oficial de hierarquia por matricula/marcadores DP/GESTOR.

## Arquivos de codigo relacionados

- `ferias_app/blueprints/pages.py`: telas principais, incluindo `/ferias`.
- `ferias_app/services/postgres_compat_service.py`: consultas leves da tela de ferias e filtros por hierarquia.
- `ferias_app/services/smartsheet_sync_service.py`: sincronizacao do Smartsheet, ignorando status `#NO MATCH`.
- `ferias_app/services/auto_sync_service.py`: sincronizacao diaria em background.
- `ferias_app/services/admin_cadastro_service.py`: edicao administrativa de cadastro e hierarquia.
- `ferias_app/blueprints/dp_api.py`: APIs de gestores/ajustes usadas pelo painel DP/Admin.
- `migracao/sql/v44_hierarquia_matricula_sem_email_custom.sql`: ajuste da tabela `hierarquia_gestao`.
