# V65 - Reconciliação de saldos e solicitações

## Fontes da reconciliação

- `export_app_ferias (14).xlsx`: fotografia do banco atual.
- `FOLHA_SOLICITACOES (2).xlsx`: histórico final de solicitações anterior à reestruturação.
- `FÉRIAS - CONTROLE GERAL atual 12.08.xlsx`: saldo líquido de referência em 12/08/2026.

## Mudança funcional necessária no app

A V64 ainda tratava somente a linha `is_atual = true` como saldo consumível. Isso é incompatível com colaboradores que possuem saldo REGULAR acumulado em mais de um período adquirido. Exemplo: 60 dias com P8=30 e P9=30.

A V65 passa a:

- somar todas as linhas REGULAR adquiridas ao exibir saldo;
- reservar/consumir REGULAR do período mais antigo que ainda possui saldo;
- usar o `periodo_aquisitivo_origem` para estornos no período real;
- preservar saldos REGULAR históricos na rotina diária;
- criar um novo período REGULAR com 30 dias somente quando o ciclo anual for adquirido;
- manter PREMIUM restrito ao ciclo vigente, conforme a regra de expiração já adotada.

## Regra de importação

O Controle Geral é tratado como saldo líquido autoritativo. Para REGULAR, o valor é distribuído dos períodos mais recentes para os anteriores, com no máximo 30 dias por período. Para PREMIUM, o valor líquido é aplicado somente no ciclo Premium vigente.

Os `AJUSTE FÉRIAS` e `AJUSTE CERTARIANA` antigos da FOLHA_SOLICITACOES não são reimportados, porque o saldo líquido já incorpora o resultado histórico deles. Reaplicá-los duplicaria seu efeito.

As solicitações GOZO/VENDA ausentes são inseridas como histórico, e três solicitações existentes têm o status final reconciliado. A carga de solicitações não debita novamente os saldos porque o Controle Geral já representa o saldo final da data-base.
