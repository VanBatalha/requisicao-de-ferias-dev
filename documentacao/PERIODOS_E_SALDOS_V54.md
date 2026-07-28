# V54 - Períodos e saldos

## Regras implementadas

### Férias regulares

- Um novo `P` somente passa a existir quando o ciclo anual está concluído.
- O período ainda em formação não é criado em `saldo_periodo` e não recebe crédito antecipado.
- O saldo regular é cumulativo. Na virada, o novo período recebe 30 dias e herda o estado consolidado do período anterior: saldo inicial, utilizado e reservado.
- Depois da virada, somente o novo período permanece com valores; as linhas históricas continuam visíveis, porém zeradas.
- Se o serviço ficar sem executar por mais de um aniversário, ele acrescenta 30 dias para cada ciclo anual concluído e ainda não registrado.
- Exemplo MAT00116: em 28/07/2026 existem P1 a P7. A faixa 11/02/2026 a 10/02/2027 somente gera P8 em 11/02/2027.

### Licença Certariana/Premium

- A numeração é independente das férias regulares: P1, P2, P3...
- P1: 30 dias no dia seguinte ao fechamento de cinco anos de empresa.
- P2 em diante: 15 dias a cada 30 meses.
- O saldo disponível do ciclo anterior expira quando nasce o próximo ciclo; não há transferência para o novo P.
- Na correção inicial, o saldo inicial Premium é reconstruído pela base fixa da regra. Utilizado e reservado são obtidos das solicitações não-ajuste pertencentes ao ciclo vigente.
- Ajustes Premium antigos, criados quando o banco ainda usava períodos anuais, ficam disponíveis no histórico para conferência, mas não são reaplicados automaticamente. Eles recebem a marca `v54_premium_adjustment_ignored` e aparecem sinalizados no Painel ADMIN. Ao editar e reaplicar validamente um desses ajustes, a marca é removida.
- Exemplo MAT00116: em 28/07/2026 existe somente o P1 Premium, com 30 dias de base e 15 utilizados. O P2 nasce em 12/08/2026 com 15 dias, e o restante do P1 expira.

### Uso no aplicativo

- As telas e os resumos somam somente a linha `is_atual = true`.
- As linhas P1 a PX continuam disponíveis no Painel ADMIN para histórico.
- Solicitações e ajustes movimentam somente o período vigente já adquirido.
- Ao marcar outra linha como vigente no Painel ADMIN, as linhas antigas do mesmo tipo são zeradas.
- Antes de reservar ou movimentar saldo, o app confirma de forma síncrona se a virada diária já foi processada.

## Execução diária

A data é calculada no fuso configurado em `APP_TIMEZONE`; o padrão é `America/Fortaleza`, evitando criação antecipada por causa do UTC do Render.

O Web Service também executa uma verificação assíncrona no primeiro acesso do dia. Ela usa somente PostgreSQL e uma trava transacional. Workers adicionais usam `try-lock` e encerram a tentativa sem ficar enfileirados; operações que movimentam saldo aguardam a virada terminar antes de continuar.

Para garantir a execução mesmo em dias sem acesso ao sistema, configure um **Render Cron Job** diário com:

```bash
python daily_balance_accrual.py
```

Cron sugerido: `10 3 * * *`. No agendamento UTC do Render, isso corresponde a 00:10 em Fortaleza. Mantenha:

```text
APP_TIMEZONE=America/Fortaleza
```

Também existe o botão **Verificar períodos agora** no Painel ADMIN.

## Correção inicial do banco

Execute uma única vez no pgAdmin:

`migracao/sql/correcao_periodos_saldos_v54_pgadmin.sql`

O script:

- cria tabelas de backup;
- afeta somente colaboradores ativos com matrícula e data de admissão;
- remove períodos futuros;
- consolida todo o saldo regular existente no último período anual adquirido;
- recria a numeração Premium independente;
- aplica a base Premium de 30 dias no P1 e 15 dias nos ciclos seguintes;
- calcula utilizado e reservado Premium pelas solicitações não-ajuste do ciclo vigente;
- deixa valores somente nas linhas vigentes;
- remapeia o período de origem das solicitações e ajustes;
- marca ajustes Premium legados como histórico não aplicado ao saldo V54;
- registra auditoria e apresenta consultas de conferência antes do `COMMIT`.

A data de referência está fixada em `2026-07-28` para coincidir com a planilha de conferência. Não altere essa data se a intenção for reproduzir exatamente o arquivo `export_app_ferias_v54_corrigido.xlsx`.

## Observação sobre os exemplos de data

Os exemplos enviados possuem uma pequena divergência: um deles menciona criar o crédito no dia seguinte ao aniversário, enquanto o caso completo da MAT00116 determina que a faixa encerrada em 10/02/2027 gere P8 em 11/02/2027. A V54 segue o caso completo da MAT00116: o período termina no dia anterior ao aniversário e o crédito regular nasce no aniversário. Assim, uma admissão em 28/07/2025 gera P1 em 28/07/2026.
