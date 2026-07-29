# Aplicação da correção V57

## Fonte oficial

A única tabela operacional de períodos e saldos passa a ser:

- `app_ferias.saldo_periodo`

As tabelas abaixo deixam de ser consultadas, atualizadas ou recriadas pelo aplicativo e são removidas pelo SQL:

- `app_ferias.periodos_aquisitivos`
- `app_ferias.saldos_periodo`

A coluna `solicitacoes_ferias.periodo_aquisitivo_origem` permanece. Ela é somente um texto de rastreabilidade da divisão dos dias e não é uma chave estrangeira para `periodos_aquisitivos`.

## Impactos no banco

- Colaboradores `INATIVO`, `INACTIVE`, desligados ou sem data de admissão ficam sem qualquer linha em `saldo_periodo`.
- Um novo colaborador ativo só recebe períodos já adquiridos, calculados pela data de admissão.
- REGULAR: um ciclo é criado apenas no aniversário que encerra os 12 meses do período.
- PREMIUM P1: 30 dias após 5 anos completos, com crédito no dia seguinte.
- PREMIUM P2 em diante: 15 dias a cada 30 meses.
- Períodos ainda em formação ou futuros são apagados e bloqueados por trigger.
- Períodos históricos válidos de ativos podem permanecer para consulta, mas sempre zerados.
- Somente o último período adquirido de cada tipo fica como `is_atual = true` e pode ter saldo.
- Solicitações e ajustes continuam em `solicitacoes_ferias`.
- A FK opcional de `auditoria_saldos.saldo_id` passa a apontar para `saldo_periodo.id`.
- Backups são criados com prefixo `z_backup_v57_` antes da exclusão das tabelas legadas.

## Adequações no aplicativo

- Removidos os modelos SQLAlchemy das tabelas legadas.
- Removidas consultas e gravações nas tabelas legadas durante sincronização e recálculo.
- A sincronização manual do ADMIN elimina saldos de inativos e, no final, normaliza apenas `saldo_periodo`.
- Ao tornar um colaborador inativo no Painel ADMIN, seus saldos são apagados imediatamente.
- A rotina diária e a criação de solicitações verificam os ciclos adquiridos antes de movimentar saldo.
- O banco recebe triggers de proteção para impedir saldo de inativo ou inserção de período futuro, inclusive em alterações manuais pelo pgAdmin.

## Ordem de aplicação

1. Fazer backup externo do PostgreSQL.
2. Publicar o app V57.
3. Confirmar `/healthz` com `"build":"v57"`.
4. Executar **todo** o arquivo `correcao_estrutura_saldos_v57_pgadmin.sql` no Query Tool.
5. Conferir os contadores operacionais e os avisos `ERRO`; todos os avisos de erro devem retornar zero.
6. Confirmar que `periodos_aquisitivos_deve_ser_nulo` e `saldos_periodo_deve_ser_nulo` aparecem como `NULL`.

## Resultado esperado no XLSX analisado

Com referência em 29/07/2026:

- 103 colaboradores ativos;
- 102 ativos com data de admissão;
- 3.058 linhas de saldo pertencentes a inativos devem ser eliminadas;
- 358 ciclos válidos devem permanecer: 310 REGULAR e 48 PREMIUM;
- `MAT00031` deve ficar sem qualquer linha em `saldo_periodo`;
- `MAT00116` deve manter REGULAR P1–P7 e PREMIUM P1, sem período futuro;
- `MAT00194` permanece sem saldo até receber uma data de admissão válida.
