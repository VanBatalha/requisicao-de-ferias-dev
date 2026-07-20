# Correção do relatório de lançamento - v48

## Sintoma
Ao gerar o relatório na tela Solicitações, o worker Gunicorn era encerrado com `SystemExit` e depois `SIGKILL`.

## Causa corrigida
- Resolução de colaboradores fazia uma consulta individual para cada integrante do escopo.
- A consulta carregava objetos ORM completos e só depois filtrava mês/ano em Python.
- Em DP/Admin, o escopo geral ampliava o tempo e a memória usados pela requisição.

## Alterações
- Conversão de e-mails/matrículas do escopo em uma única consulta ao banco.
- Relatório PostgreSQL gerado por uma consulta única e leve.
- Filtro de mês e ano aplicado diretamente no PostgreSQL.
- Seleção apenas das colunas necessárias ao relatório.
- Agrupamento exibido por nome e matrícula, sem depender do e-mail.
- Fallback legado mantido para instalações sem PostgreSQL.
