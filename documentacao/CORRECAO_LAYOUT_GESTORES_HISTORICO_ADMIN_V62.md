# V62 - Layout de Gestores e histórico para DP/ADMIN

## Gestores

- O seletor de gestor usa o mesmo padrão visual de autocomplete da tela de Solicitações.
- As sugestões ficam em uma caixa flutuante com rolagem, sem deslocar o restante da página.
- A tela foi dividida em dois painéis responsivos: gestores configurados e equipe do gestor.
- Gestores configurados exibem nome, matrícula, e-mail e quantidade de subordinados.
- A equipe usa caixas de seleção organizadas e pesquisa por nome, matrícula ou e-mail.
- O gestor atualmente selecionado permanece destacado.

## Ajustes e histórico

- O histórico continua somente leitura.
- Perfis DP e ADMIN podem consultar solicitações e ajustes.
- Foi incluído um seletor direto de colaborador, independente da hierarquia de gestores.
- A consulta inclui colaboradores ativos e inativos.
- Ao selecionar um subordinado para ajuste, o mesmo colaborador é carregado automaticamente no histórico.
- Nenhuma rota de edição ou exclusão foi liberada para o Painel DP.

## Permissões

A API do Painel DP reconhece primeiro o `user_type` e os grupos já salvos na sessão. Isso evita que um ADMIN autenticado dependa de uma nova consulta de permissões em cada chamada. A consulta por e-mail permanece como fallback.

## Banco de dados

Esta versão não exige alteração SQL.
