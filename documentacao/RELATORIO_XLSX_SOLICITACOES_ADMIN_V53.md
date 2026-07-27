# V53 — Exportação XLSX e manutenção de solicitações

## Relatório de solicitações

- A tela continua exibindo o relatório por mês e ano.
- Depois de uma geração bem-sucedida, o botão **Baixar XLSX** é habilitado.
- O arquivo usa preferencialmente o mesmo resultado armazenado no cache da visualização, evitando repetir imediatamente a consulta PostgreSQL.
- O XLSX possui as abas **Resumo** e **Solicitações**.
- As regras de acesso permanecem:
  - ADMIN e DP: todos os colaboradores, sem subordinação;
  - gestor: somente a própria equipe.

## Painel ADMIN

A manutenção por colaborador passa a exibir também as solicitações normais, separadas dos ajustes. O ADMIN pode editar ou excluir uma solicitação.

Ao alterar tipo de saldo, quantidade de dias ou status, o sistema estorna o efeito anterior e aplica o novo efeito nas linhas P1 até PX na mesma transação. Ao excluir, o efeito da solicitação é estornado antes da remoção.

Registros históricos sem matrícula podem ser localizados pelo e-mail apenas quando não possuem `colaborador_id` nem `colaborador_matricula`; ao serem salvos, passam a ser vinculados ao colaborador por matrícula.

Todas as alterações e exclusões são gravadas na tabela de auditoria.
