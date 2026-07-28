# Gestão de Férias — documentação da V55

## Alterações principais

- Saldo somente no último período vigente, tanto REGULAR quanto PREMIUM.
- Períodos históricos permanecem visíveis, mas zerados.
- REGULAR deixa de carregar saldo entre ciclos anuais.
- PREMIUM mantém P1 de 30 dias após cinco anos e P2+ de 15 dias a cada 30 meses, sem carregamento.
- Backups PostgreSQL passam a usar prefixo `z_backup_`.
- Exportador XLSX ordena por `id` ou chave primária.
- Pacote preparado para implantação em servidor Contabo com Docker Compose.
- Agendamento diário por cron do próprio servidor.

## Arquivos da V55

- `migracao/sql/correcao_saldos_v55_apenas_periodo_vigente.sql`
- `export_database_xlsx.py`
- `documentacao/SALDOS_SOMENTE_PERIODO_VIGENTE_V55.md`
- `documentacao/EXPORTACAO_BANCO_ORDENADA_V55.md`
- `documentacao/IMPLANTACAO_CONTABO_DOCKER_V55.md`
- `Dockerfile`
- `compose.yaml`
- `.env.example`
- `deploy/Caddyfile`
- `deploy/scripts/periodos_diarios.sh`
- `deploy/scripts/backup_postgres.sh`
- `deploy/scripts/update_app.sh`
