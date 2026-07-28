# Correção forçada de períodos e saldos — V56

## Motivo da V56

Versões anteriores do recálculo podiam recriar uma linha `PREMIUM` para cada período anual e voltar a preencher saldos históricos. Por isso, executar somente um SQL enquanto um build antigo permanecia publicado podia fazer o banco retornar à estrutura anterior.

## Ordem obrigatória de implantação

1. Publique o app V56.
2. Abra `/healthz` e confirme que o campo `build` retorna `v56`.
3. Reinicie o serviço do Render.
4. Execute o arquivo inteiro `migracao/sql/correcao_periodos_saldos_v56_forcada.sql` no pgAdmin.
5. Confira os resultados de validação. Todos devem retornar zero:
   - `historicos_com_saldo`;
   - `premium_indevido`;
   - `mais_de_um_periodo_atual`.
6. Confira a MAT00116 no resultado final.

## Regra REGULAR

- O período em formação não é criado.
- P1 até o último período anual adquirido permanecem para histórico.
- Somente o último P adquirido contém saldo.
- O saldo inicial, utilizado e reservado de todas as linhas antigas é consolidado no último P.
- Na virada anual seguinte, o novo P recebe mais 30 dias e herda o saldo consolidado.

## Regra PREMIUM

- P1 nasce no dia seguinte ao fechamento de cinco anos e recebe 30 dias.
- P2 em diante nasce a cada 30 meses e recebe 15 dias.
- O saldo Premium anterior expira quando nasce um novo ciclo.
- Períodos Premium anuais indevidos são excluídos.
- Ajustes Premium legados permanecem no histórico, mas não são reaplicados automaticamente.

## Se o SQL interromper

Execute `migracao/sql/diagnostico_periodos_saldos_v56.sql` e envie todos os resultados. A V56 interrompe a transação quando detecta banco/schema incorreto ou qualquer inconsistência final, evitando uma execução aparentemente bem-sucedida sem mudanças.
