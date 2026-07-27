# V52 — Relatório resiliente e manutenção administrativa de saldos

## Relatório da tela Solicitações

A rota `/api/relatorio-lancamento` trabalha somente com PostgreSQL e usa a matrícula salva na sessão.

- `ADMIN` e `DP`: todas as solicitações não classificadas como ajuste, sem filtro de subordinação.
- `USER/gestor`: solicitações dos colaboradores vinculados à matrícula do gestor em `hierarquia_gestao`.
- mês e ano são filtrados diretamente por `data_inicio` no PostgreSQL.
- o relatório abre uma conexão curta e separada do pool SQLAlchemy.
- cada consulta possui `statement_timeout`, `lock_timeout` e um limite rígido no processo antes do timeout do Gunicorn.
- o último resultado bem-sucedido fica em cache local por 10 minutos. Se o banco oscilar, o app devolve o último relatório do mesmo usuário/período com um aviso na tela.

Variáveis opcionais:

- `RELATORIO_CACHE_TTL_SECONDS`: validade do cache, padrão `600`.
- `RELATORIO_QUERY_TIMEOUT_SECONDS`: limite rígido de cada SQL, padrão `9`.
- `RELATORIO_CACHE_DIR`: diretório do cache, padrão `/tmp/ferias_app_relatorios`.

## Manutenção por colaborador no Painel ADMIN

Na área **Editar cadastro no PostgreSQL**, o administrador pode pesquisar por matrícula, nome ou e-mail e visualizar:

- todas as linhas P1 até PX de férias regulares;
- todas as linhas P1 até PX de Licença Certariana;
- saldo inicial, utilizado, reservado e disponível;
- período atual;
- todos os ajustes registrados em `solicitacoes_ferias`.

O administrador pode editar ou excluir linhas de saldo e ajustes. Todas as operações são auditadas na tabela `auditoria`.

### Regras de segurança dos ajustes

- edição apenas de data ou observação não movimenta o saldo novamente;
- alteração de tipo, dias, período ou status estorna o efeito anterior e aplica o novo efeito na mesma transação;
- mudar um ajuste aprovado para cancelado/reprovado/pendente estorna seu efeito;
- aprovar um ajuste ainda não aprovado aplica seu efeito;
- um crédito já consumido ou reservado não pode ser excluído automaticamente; primeiro deve ser corrigida a respectiva linha de saldo.

## Smartsheet

A comunicação legada com Smartsheet fica desativada por padrão. A única rotina ativa é a sincronização explícita da aba ADMIN, implementada em `smartsheet_sync_service.py`.

A variável `SMARTSHEET_LEGACY_ACCESS_ENABLED` não deve ser habilitada no Render. Ela existe somente para compatibilidade emergencial com código antigo.
