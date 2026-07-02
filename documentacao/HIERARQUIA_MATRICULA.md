# Hierarquia por matricula

A matricula e a chave oficial para relacoes entre colaborador, gestor direto e gestor superior. E-mail pode aparecer apenas como informacao visual/cache, nunca como chave para decidir permissao ou visibilidade.

## Tabela principal

`app_ferias.hierarquia_gestao` mantem:

- `colaborador_matricula`: colaborador dono da regra.
- `gestor_direto_id`: cache interno opcional do gestor direto.
- `gestor_direto_matricula`: matricula do gestor direto.
- `gestor_direto_email`: cache visual derivado da matricula.
- `gestor_superior_id`: cache interno opcional do gestor superior, quando for uma matricula real.
- `gestor_superior_matricula`: matricula do gestor superior ou marcador `DP`/`GESTOR`.
- `gestor_superior_email`: cache visual derivado da matricula do gestor superior.

## Marcadores

- `GESTOR`: o colaborador fica visivel para o gestor direto e ADMIN.
- `DP`: o colaborador fica visivel para usuarios do grupo DP e ADMIN.
- `MAT00000`: o colaborador fica visivel tambem para a matricula informada como gestor superior.

## Colunas removidas

`gestor_superior_tipo` e `gestor_superior_email_custom` eram um modelo antigo para separar tipos de valores, mas causavam confusao. A V44 remove essa dependencia.

## Arquivos relacionados

- `ferias_app/models.py`
- `ferias_app/services/smartsheet_sync_service.py`
- `ferias_app/services/postgres_compat_service.py`
- `ferias_app/services/admin_cadastro_service.py`
- `ferias_app/blueprints/dp_api.py`
- `migracao/sql/v44_hierarquia_matricula_sem_email_custom.sql`
