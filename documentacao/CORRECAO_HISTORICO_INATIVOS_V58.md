# V58 — Preservação do histórico após inativação

## Regra da implantação

Na execução inicial do SQL, todos os colaboradores que já estão inativos são removidos de `saldo_periodo`. Isso elimina os saldos antigos gerados antes da nova regra.

## Regra após a implantação

Quando um colaborador ativo passar a inativo:

- as linhas que já existirem em `saldo_periodo` permanecem como histórico;
- nenhum trigger apaga essas linhas;
- a edição do cadastro no Painel ADMIN não apaga essas linhas;
- a rotina diária não cria novos períodos para o inativo;
- o PostgreSQL bloqueia a inserção manual de uma nova linha de saldo para um inativo;
- solicitações e ajustes históricos continuam vinculados à matrícula.

Se o colaborador for reativado futuramente, a rotina diária volta a considerar a matrícula e cria somente ciclos já adquiridos conforme as regras REGULAR e PREMIUM.

## Tabelas legadas

`periodos_aquisitivos` e `saldos_periodo` continuam removidas. `saldo_periodo` permanece como fonte única para períodos e saldos.
