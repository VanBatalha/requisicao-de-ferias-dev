# Correções realizadas

## Ajuste complementar: USER TYPE como fonte oficial de permissão

Foi corrigida a regressão em que o perfil do usuário podia ser resolvido incorretamente após a refatoração LDAP.

Regra final aplicada:

- O LDAP autentica e identifica o usuário.
- O perfil funcional da aplicação vem exclusivamente da planilha `3609445264215940 CONTROLE_DP`, coluna `USER TYPE`.
- `USER TYPE = ADMIN` ou `Administrador` => grupo `Administrador` / role `admin`.
- `USER TYPE = DP`, `RH` ou equivalentes => grupo `DP` / role `DP`.
- Demais valores => `USER`.

## Causa provável do problema ADMIN virando DP

Na versão corrigida anterior, a busca "exata" do usuário no cadastro passou a usar comparação tolerante de e-mail. Essa comparação também aceitava apenas a parte antes do `@`.

Se a planilha tivesse mais de uma linha com local-part parecido, por exemplo:

- `acesso.01@dominio-a` como DP
- `acesso.01@certare.com.br` como ADMIN

A primeira linha encontrada poderia ser usada indevidamente, fazendo o usuário aparecer como DP.

## Correção técnica

Arquivo alterado: `ferias_app/services/cadastro_service.py`

- A primeira etapa de busca voltou a exigir igualdade real do e-mail normalizado.
- O fallback por local-part só roda depois que não há match exato.
- Quando há múltiplos matches por local-part, a seleção continua determinística:
  1. prefere mesmo domínio;
  2. se ainda houver ambiguidade, prefere maior privilégio: ADMIN > DP > USER.
- A normalização do `USER TYPE` agora aceita sinônimos como `Administrador`, `ADM`, `RH`, `Recursos Humanos` etc.

Arquivo alterado: `ferias_app/services/permissions_service.py`

- `tem_grupo(email, "DP")` agora só retorna verdadeiro quando `USER TYPE = DP`.
- ADMIN continua podendo acessar rotas de DP quando a rota pede explicitamente `DP ou Administrador`, mas não é mais classificado como pertencente ao grupo DP.

## Pontos mantidos da correção anterior

- Simulação de gestor disponível somente para ADMIN.
- Solicitações bloqueadas em modo de simulação.
- Gestor pode consultar o próprio saldo, mas não solicitar férias para si mesmo.
- Relatório CSV por colaborador e/ou mês.
- Confirmação adicional após gravação da solicitação no Smartsheet.

## Ajuste complementar v3: prioridade para cadastro ATIVO

Foi corrigido o cenário em que o mesmo usuário possui mais de uma linha na planilha `3609445264215940 CONTROLE_DP`, sendo uma matrícula/cadastro antigo com `STATUS = INATIVO` e outra referência ativa.

Regra final aplicada na resolução do usuário:

1. procura e-mail exato com `STATUS` ativo;
2. se não encontrar, procura usuário/local-part equivalente com `STATUS` ativo;
3. se só houver registros inativos, mantém a referência apenas para diagnóstico, mas não concede `USER TYPE` privilegiado;
4. `USER TYPE` de linha inativa é ignorado e tratado como `USER` para evitar que um cadastro antigo continue dando acesso a DP/ADMIN.

Arquivos alterados:

- `ferias_app/services/cadastro_service.py`

Principais funções ajustadas/adicionadas:

- `normalizar_status`
- `is_status_ativo_value`
- `_row_is_active`
- `_pick_best_user_row`
- `get_user_row`
- `get_user_row_by_identifiers`
- `get_user_type`
- `is_ativo`

Com isso, o LDAP continua apenas autenticando/identificando o usuário, mas a aplicação resolve o cadastro correto no Smartsheet priorizando a linha ativa antes de ler `USER TYPE`.
