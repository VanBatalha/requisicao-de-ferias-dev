# V63 - Correção de saldos, relatórios e seletores

## Importação da correção

O arquivo `importar_correcao_saldo_periodo_v63_pgadmin.sql` foi gerado a partir de `correção(1).xlsx` e substitui, em uma única transação:

- `saldo_periodo`: 358 registros;
- `auditoria_saldos`: reiniciada vazia porque seus IDs antigos referenciam os saldos substituídos.

A aba `solicitacoes_ferias` foi comparada com a carga anterior e não apresentou alterações; por isso, a tabela permanece intacta. O SQL cria tabelas `z_backup_import_v63_*` antes do `TRUNCATE` e valida referências, duplicidades e contagens antes do `COMMIT`.

## Tela Solicitações

- A tela abre sem selecionar automaticamente o primeiro colaborador.
- Saldos, histórico e formulário permanecem zerados até uma seleção explícita.
- O relatório permite:
  - DP/ADMIN: todos ou somente o colaborador selecionado;
  - gestor: sua equipe, incluindo o próprio gestor, ou somente o colaborador selecionado.
- Mês e ano continuam sendo utilizados no relatório e no XLSX.

## Painel DP - Férias

- Incluído seletor pesquisável de gestor.
- O relatório considera a equipe direta/superior e inclui o próprio gestor.
- Pode ser visualizado na tela e baixado em XLSX.
- O download reutiliza o mesmo escopo, mês e ano do resultado exibido.

## Painel DP - Colaboradores

- Busca por matrícula, nome ou e-mail com caixa de sugestões pesquisável.
- O relatório de saldos agora é XLSX com abas `Resumo` e `Saldos`.
- O arquivo contém título, filtros, indicadores, resumo por colaborador, grupos REGULAR/PREMIUM, filtros de coluna, congelamento de cabeçalho e totais.

## Identificação

Todos os novos escopos de relatório utilizam matrícula como identificador operacional.
