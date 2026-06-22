# Requisi-o-de-F-rias
App de requisição de férias


## Migração V12 - Banco app_ferias por matrícula

Esta versão usa `app_ferias` como schema padrão. A matrícula (`colaboradores.matricula`) é o identificador de negócio usado em novas solicitações, períodos, saldos e auditorias. Execute `script_app_ferias_matricula_v12.sql` no pgAdmin antes do deploy.
