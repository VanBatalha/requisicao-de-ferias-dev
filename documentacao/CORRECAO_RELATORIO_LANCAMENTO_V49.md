# Correcao do relatorio de lancamentos - V49

## Alteracoes

- Removido integralmente o fallback do Smartsheet na rota `/api/relatorio-lancamento`.
- Permissoes, hierarquia, colaboradores e solicitacoes agora sao consultados somente no PostgreSQL.
- O e-mail e usado apenas para localizar o usuario autenticado; o escopo e todas as relacoes usam matricula.
- Removido `JOIN` com `UPPER()` entre colaboradores e solicitacoes.
- Nomes dos colaboradores sao carregados em uma consulta separada e leve.
- Adicionado `statement_timeout` de 15 segundos para evitar que o Gunicorn encerre o worker sem resposta controlada.
- Removidos diretorios `__pycache__` e arquivos `.pyc/.pyo` do pacote.

## Smartsheet

A geracao do relatorio nao importa nem chama servicos do Smartsheet. A integracao permanece disponivel apenas nas rotinas administrativas de sincronizacao existentes no projeto.
