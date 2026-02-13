# Arquitetura (Etapa 3)

Nesta etapa, o projeto foi reorganizado para separar responsabilidades em **serviços** e **utilitários**, mantendo **compatibilidade** com o código legado.

## Pastas principais

- `ferias_app/`
  - `__init__.py`  
    App Factory (`create_app`), registra blueprint e injeta contexto para templates.
  - `config.py`  
    Leitura centralizada de variáveis de ambiente (Settings).
  - `logging_config.py`  
    Logging para stdout (ideal para Render).
  - `utils.py`  
    Helpers puros (parse de data, normalização, etc.).
  - `services/`  
    Camada “regra de negócio + integração”
    - `auth_service.py` (OAuth Smartsheet, sessão, contexto de template)
    - `smartsheet_service.py` (SDK + HTTP helpers)
    - `cadastro_service.py` (leitura da folha de cadastro, colaboradores, gestor, status, user type)
    - `permissions_service.py` (regras de permissão/role usando cadastro)
    - `solicitacoes_service.py` (criação de solicitações, validações centrais)
  - `legacy/`
    - `core_legacy.py` (código original consolidado, mantido como fallback)
  - `core.py`  
    **Compat layer**: reexporta funções novas e também o legado (para não quebrar imports antigos).

- `ferias_app/blueprints/`  
  Rotas agrupadas por contexto, mas **com o mesmo Blueprint `ferias`** para não quebrar `url_for('ferias.*')`.

- `templates/`  
  Telas HTML/Jinja (sem mudanças de estrutura).

## Fluxo resumido

1. Usuário acessa `/login`
2. OAuth Smartsheet → `/callback` salva token + usuário em sessão
3. Rotas verificam permissões via `permissions_service`
4. Inserções/leituras no Smartsheet passam por `services/*`
