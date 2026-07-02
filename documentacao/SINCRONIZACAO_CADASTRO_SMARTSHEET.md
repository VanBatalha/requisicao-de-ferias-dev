# Sincronização do cadastro Smartsheet

## Fonte atual

A fonte oficial de cadastro passou a ser a planilha **CADASTRO DE COLABORADORES**:

- Smartsheet ID: `1745799836133252`
- Variável opcional: `ID_FOLHA_CADASTRO_PRINCIPAL=1745799836133252`

A planilha antiga **CONTROLE_DP** (`3609445264215940`) deixou de ser fonte operacional para cadastro, permissões e saldos.

## Colunas lidas da planilha principal

A rotina de sincronização usa principalmente estas colunas:

- `MATRÍCULA`
- `NOME COMPLETO`
- `CARGO`
- `SETOR`
- `REGIME DE CONTRATAÇÃO`
- `UNIDADE`
- `EMPRESA`
- `TELEFONE`
- `STATUS`
- `DATA DE ADMISSÃO`
- `E-MAIL EMPRESA`
- `GESTOR SUPERIOR`
- `GESTOR DIRETO`

## Colunas que não são mais fonte do Smartsheet

Estas informações passaram a ser controladas pelo PostgreSQL:

- `USER TYPE` -> tabela `app_ferias.permissoes_usuario`
- saldos/dias de férias -> tabela `app_ferias.saldo_periodo`
- solicitações/histórico -> tabela `app_ferias.solicitacoes_ferias`

Novo colaborador encontrado na sincronização recebe permissão `USER` somente se ainda não existir nenhuma role em `permissoes_usuario`.
Permissões existentes (`ADMIN`, `DP`, `USER`) não são sobrescritas pela planilha.

## Matrícula como chave oficial

A matrícula é a chave de negócio para colaborador, gestor direto e gestor superior.

Quando as colunas de gestor vierem como contato/e-mail no Smartsheet, a sincronização converte esse contato para a matrícula ativa correspondente da própria planilha. O vínculo gravado no banco continua sendo matrícula, nunca e-mail.

Se o mesmo e-mail apontar para mais de uma matrícula ativa, o vínculo é considerado ambíguo e não é usado para hierarquia.

## Status inválido

Linhas com `STATUS` igual a `#NO MATCH`, `NO MATCH`, `#N/A` ou `N/A` são ignoradas e não entram no banco.

## Formas de execução

Todas usam a mesma rotina central:

- botão do Painel ADMIN: `POST /api/admin/sync-cadastro`
- execução local: `python sync_cadastro_smartsheet.py`
- sincronização automática diária em background

Arquivos relacionados:

- `ferias_app/services/smartsheet_sync_service.py`
- `ferias_app/services/auto_sync_service.py`
- `ferias_app/blueprints/admin_api.py`
- `sync_cadastro_smartsheet.py`
- `templates/painel_admin.html`
