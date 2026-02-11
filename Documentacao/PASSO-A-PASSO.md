# Passo a passo das funcionalidades

## Login / Sessão
- `/login` inicia OAuth (Smartsheet)
- `/callback` troca code por token e salva usuário em sessão
- `/logout` limpa sessão

Implementação:
- `ferias_app/services/auth_service.py`

## Permissões
- Admin / DP / Gestor / Usuário
- Baseado na planilha de cadastro (coluna `USER TYPE`) e relação de gestor (coluna `GESTOR`)

Implementação:
- `ferias_app/services/cadastro_service.py`
- `ferias_app/services/permissions_service.py`

## Solicitações
- Inserções na folha de solicitações
- Regras e validações centrais em serviço

Implementação:
- `ferias_app/services/solicitacoes_service.py`
