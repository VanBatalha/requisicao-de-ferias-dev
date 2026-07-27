# Correção do Relatório de Lançamento — V51

## Causa tratada

O Web Service ainda iniciava o agendador automático de sincronização do Smartsheet em cada processo do Gunicorn. Em ambiente com mais de um worker, isso podia criar sincronizações paralelas e disputar conexões PostgreSQL com a rota do relatório. O `pool_timeout` anterior também era de 30 segundos, igual ao timeout comum do Gunicorn, fazendo o worker ser encerrado antes de uma mensagem controlada.

## Alterações

- sincronização automática removida do startup e do `before_request`;
- Smartsheet permanece apenas nas ações explícitas da aba ADMIN;
- matrícula da sessão usada como identidade do relatório;
- nenhuma busca por e-mail na geração do relatório;
- DP e ADMIN não consultam a tabela de hierarquia;
- gestores consultam equipe por matrícula, incluindo histórico de colaboradores inativos;
- consulta de solicitações por faixa de datas e relacionamento por matrícula;
- `statement_timeout` de 12 segundos e `lock_timeout` de 3 segundos aplicados antes das consultas do relatório;
- pool PostgreSQL reduzido e `pool_timeout`/`connect_timeout` de 5 segundos;
- logs com código da execução e duração de cada etapa;
- front-end preparado para mostrar erro HTTP e código de diagnóstico;
- detalhes das solicitações disponíveis ao expandir cada colaborador.

## Logs esperados

Uma execução bem-sucedida registra mensagens semelhantes a:

- `RELATORIO[abc123] conexão obtida em ...`
- `RELATORIO[abc123] permissões em ...`
- `RELATORIO[abc123] hierarquia em ...` (somente gestor)
- `RELATORIO[abc123] solicitações em ...`
- `RELATORIO[abc123] concluído em ...`

Se houver falha, a interface exibe o mesmo código presente no log.
