# Sincronização de Cadastro Smartsheet -> PostgreSQL

A versão V6 permite atualizar a base de cadastro do PostgreSQL a partir da planilha Smartsheet `3609445264215940`.

## Variáveis necessárias no Render

```text
DATABASE_URL=<url do postgres>
DB_SCHEMA=ferias_app
SMARTSHEET_ACCESS_TOKEN=<token do Smartsheet>
ID_FOLHA_CADASTRO=3609445264215940
```

## Pelo Painel Admin

Acesse **Painel Admin > Sincronização com Smartsheet** e clique em **Sincronizar cadastro agora**.

O botão atualiza:

- `ferias_app.colaboradores`
- `ferias_app.colaborador_complemento`
- `ferias_app.sync_state`

Também recalcula os saldos com base nas solicitações já existentes no PostgreSQL.

## Por Render Cron Job

Crie um Cron Job no Render apontando para o mesmo repositório e use o comando:

```bash
python sync_cadastro_smartsheet.py
```

Sugestão de periodicidade: a cada 1 hora ou 1 vez ao dia, dependendo da frequência de alterações na planilha.

## Observação importante

Edições manuais feitas diretamente no Painel Admin podem ser sobrescritas pela próxima sincronização se o mesmo campo vier diferente na planilha Smartsheet.

## Observações da V8

- A sincronização agora localiza colaboradores nesta ordem: origem do Smartsheet (`origem_sheet_id` + `origem_row_id`), matrícula e, por último, e-mail.
- Isso evita duplicidade quando um e-mail é alterado no Smartsheet, pois a linha de origem continua sendo a mesma.
- A coluna `matricula` em `colaboradores` é usada como ID externo/código de cadastro vindo da coluna `MATRÍCULA` da planilha. O campo `id` continua sendo a chave técnica interna do PostgreSQL, usada por relacionamentos e solicitações.
- Em caso de conflito entre origem/matrícula/e-mail, a sincronização preserva os dados existentes para evitar violar restrições únicas e registra o conflito no resumo da sincronização.


## Modo V9: inclusão segura por matrícula

A sincronização de cadastro passou a operar em modo **insert-only** usando a coluna `MATRÍCULA` do Smartsheet como ID externo oficial.

- Se a matrícula já existir em `ferias_app.colaboradores.matricula`, a linha é considerada já importada e nenhum dado cadastral/complementar é sobrescrito.
- Se a matrícula não existir no PostgreSQL, um novo colaborador é criado com os dados iniciais vindos do Smartsheet.
- Se existir um cadastro legado pelo mesmo e-mail ou pela mesma origem, mas sem matrícula, a sincronização pode preencher somente a matrícula para criar o vínculo, preservando os demais campos.
- Linhas sem matrícula são ignoradas para evitar cadastros sem ID externo.
- O recálculo de saldos não é executado por padrão durante a sincronização de cadastro.
