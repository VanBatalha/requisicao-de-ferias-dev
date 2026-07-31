# V61 - Gestores e histórico no Painel DP

## Tela Gestores

A leitura da hierarquia passou a usar `hierarquia_gestao` como fonte principal, com `colaborador_complemento` apenas como compatibilidade. O mapa de gestores é carregado em uma única consulta ao PostgreSQL, reduzindo consultas repetidas e evitando divergências após a recriação do schema.

Ao salvar uma relação, o app atualiza as duas estruturas ainda existentes no banco:

- `hierarquia_gestao`: vínculo operacional por matrícula;
- `colaborador_complemento`: compatibilidade com telas e sincronização cadastral.

Também foi corrigido o acúmulo de listeners JavaScript quando a tela era recarregada após salvar.

## Histórico somente leitura na aba Ajustes

Ao selecionar um subordinado, o DP visualiza:

- solicitações de férias;
- ajustes de saldo;
- datas;
- quantidade de dias;
- status;
- tipo de saldo;
- período de aplicação;
- observações e responsável pelo registro.

A nova rota `GET /api/dp/historico/<matricula>` é somente leitura. Nenhuma ação de editar ou excluir foi adicionada ao Painel DP.

## Identificação

A matrícula é a chave principal. O e-mail é usado apenas como compatibilidade para localizar históricos antigos que não possuem matrícula preenchida.
