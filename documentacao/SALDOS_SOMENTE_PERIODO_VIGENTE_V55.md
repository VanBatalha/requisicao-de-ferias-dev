# V55 — saldo somente no período vigente

## Regra final

Para colaboradores ativos, as linhas P1 até PX continuam registradas para histórico, porém somente a linha marcada como `is_atual = true` pode possuir valores de saldo.

### Férias regulares

- Um ciclo é criado somente depois de completar 12 meses desde a admissão.
- O período em formação não existe em `saldo_periodo`.
- O período vigente começa com 30 dias.
- Quando o ciclo anual seguinte nasce, o saldo anterior expira e o novo período começa com 30 dias.
- Não existe transferência ou soma de saldo entre P anteriores e o novo P.

### Licença Certariana/Premium

- P1: 30 dias no dia seguinte ao fechamento de cinco anos de empresa.
- P2 em diante: 15 dias a cada 30 meses.
- Ao nascer um novo período Premium, o saldo anterior expira.
- A numeração Premium é independente da numeração Regular.

## Correção inicial do PostgreSQL

Execute no pgAdmin:

```text
migracao/sql/correcao_saldos_v55_apenas_periodo_vigente.sql
```

O script:

1. Renomeia tabelas iniciadas por `backup_` para `z_backup_`.
2. Cria os backups da V55 também com prefixo `z_backup_`.
3. Recria os ciclos adquiridos dos colaboradores ativos.
4. Zera integralmente todas as linhas históricas.
5. Preserva os valores já existentes somente na linha do último ciclo adquirido.
6. Se a linha vigente não existir, cria a base padrão de 30 dias no REGULAR e 30/15 dias no PREMIUM.
7. Remove períodos futuros ainda não adquiridos.
8. Exibe conferências antes do `COMMIT`.

A correção não soma os períodos antigos ao vigente e não reinterpreta os ajustes históricos. Depois da normalização, ajustes e solicitações continuam sendo movimentados pelo próprio aplicativo.

## Execução diária

A aplicação continua verificando os períodos no primeiro acesso do dia e obrigatoriamente antes de movimentar uma solicitação. No servidor de produção, também deve existir um agendamento externo executando:

```bash
python daily_balance_accrual.py
```

A rotina V55 nunca carrega saldo de uma linha histórica para o novo período.
