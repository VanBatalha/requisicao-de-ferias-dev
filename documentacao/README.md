# Gestao de Ferias - indice tecnico

Versao atual: V45.

## Documentos principais

- `CONFIGURACAO_AMBIENTES.md`: variaveis de ambiente para Render/local e perfis de banco.
- `ROTAS_PERFORMANCE.md`: fluxo da rota `/ferias`, logs `FERIAS_PERF` e consultas leves.
- `SALDOS_MATRICULA.md`: regra oficial de saldos usando `saldo_periodo`.
- `SINCRONIZACAO_MIGRACAO.md`: sincronizacao Smartsheet -> PostgreSQL, importacao local e scripts SQL.
- `HIERARQUIA_MATRICULA.md`: regra oficial de hierarquia por matricula/marcadores DP/GESTOR.

## Arquivos de codigo relacionados

- `ferias_app/blueprints/pages.py`: telas principais, incluindo `/ferias`.
- `ferias_app/services/postgres_compat_service.py`: consultas leves da tela de ferias e filtros por hierarquia.
- `ferias_app/services/smartsheet_sync_service.py`: sincronizacao do Smartsheet, ignorando status `#NO MATCH` e com bloqueio contra execucoes simultaneas.
- `ferias_app/services/auto_sync_service.py`: sincronizacao diaria em background.
- `ferias_app/blueprints/admin_api.py`: endpoint `/api/admin/sync-cadastro` inicia sync em background para nao estourar timeout HTTP.
- `templates/painel_admin.html`: botao de sync inicia a rotina e acompanha pelo status.
- `ferias_app/services/admin_cadastro_service.py`: edicao administrativa de cadastro e hierarquia.
- `ferias_app/blueprints/dp_api.py`: APIs de gestores/ajustes usadas pelo painel DP/Admin.
- `migracao/sql/v44_hierarquia_matricula_sem_email_custom.sql`: ajuste da tabela `hierarquia_gestao`.
