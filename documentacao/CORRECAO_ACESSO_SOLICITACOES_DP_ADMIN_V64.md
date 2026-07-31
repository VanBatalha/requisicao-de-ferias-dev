# V64 — acesso integral à tela Solicitações para DP e ADMIN

## Correção

- ADMIN e DP recebem todos os colaboradores ativos no seletor da tela `/ferias`.
- O vínculo de gestor direto/superior não é usado para limitar esses dois perfis.
- Somente gestores comuns continuam restritos à própria equipe.
- O perfil salvo na sessão durante o login tem prioridade para definir o escopo, evitando redução indevida por uma consulta posterior de permissões.
- Criação, histórico individual e relatórios continuam usando matrícula como identificador.

## Observação

Em modo de simulação de gestor, o ADMIN continua vendo deliberadamente apenas o escopo do gestor simulado. Fora da simulação, o acesso é integral.
