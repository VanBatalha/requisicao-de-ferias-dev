# V58 - Preservacao do historico apos inativacao

## Regra definitiva

A implantacao inicial remove de `saldo_periodo` todos os colaboradores que ja estiverem inativos naquele momento. Essa limpeza serve para eliminar os saldos antigos criados antes da nova regra.

Depois da implantacao, quando um colaborador ativo passar a inativo:

- as linhas ja existentes em `saldo_periodo` permanecem;
- nenhuma sincronizacao, rotina diaria ou edicao do cadastro apaga essas linhas;
- nenhum novo periodo REGULAR ou PREMIUM e criado enquanto ele estiver inativo;
- uma insercao manual de nova linha de saldo para inativo e bloqueada pelo PostgreSQL;
- as solicitacoes, ajustes e saldos existentes continuam disponiveis como historico.

## Impactos no aplicativo

Foram removidos tres caminhos que apagavam saldos depois da inativacao:

1. trigger do PostgreSQL ligado a alteracao de `colaboradores.status`;
2. exclusao feita na edicao do colaborador pelo Painel ADMIN;
3. exclusao feita ao final da sincronizacao cadastral do Smartsheet.

A rotina diaria agora seleciona somente colaboradores ativos para criar ou normalizar ciclos. Ela ignora inativos sem excluir o historico deles.

## Tabelas oficiais

- `saldo_periodo`: unica fonte de periodos e saldos;
- `solicitacoes_ferias`: historico de solicitacoes e ajustes;
- `periodos_aquisitivos`: removida;
- `saldos_periodo`: removida.

## Qual SQL executar

### A V57 ja foi aplicada

Execute somente:

`patch_v58_preservar_historico_inativos.sql`

Esse arquivo nao exclui nem recria saldos. Ele apenas remove o trigger de limpeza e substitui a validacao por uma que preserva o historico.

### A limpeza estrutural ainda nao foi aplicada

Execute uma unica vez:

`correcao_estrutura_saldos_v58_pgadmin.sql`

Esse arquivo remove os inativos que ja existem na data da implantacao, corrige ciclos dos ativos e instala as protecoes V58. Nao deve ser executado novamente no futuro, pois uma nova execucao trataria os inativos daquele momento como parte da limpeza inicial.

## Reativacao

Se um colaborador for reativado, a rotina diaria volta a processar a matricula e cria somente os ciclos legalmente adquiridos ate a data da reativacao. O historico preservado continua associado a mesma matricula.
