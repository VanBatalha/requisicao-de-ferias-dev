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
