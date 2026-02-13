# Guia de Implementação (Render) — Etapa 3

## Deploy no Render (sem quebrar o atual)

Para testar sem mexer no que já está rodando:
1. Crie um **novo Web Service** no Render (outro repositório ou outro branch).
2. Configure as **variáveis de ambiente** no novo serviço.
3. Use um **Redirect URI** diferente (o domínio do novo serviço).

### Start Command

Você pode usar qualquer um:

- Compatível com versões anteriores:
```bash
gunicorn app:app --bind 0.0.0.0:$PORT
```

- Recomendado:
```bash
gunicorn wsgi:app --bind 0.0.0.0:$PORT
```

### Variáveis obrigatórias

- `FLASK_SECRET_KEY`
- `SMARTSHEET_CLIENT_ID`
- `SMARTSHEET_CLIENT_SECRET`
- `SMARTSHEET_REDIRECT_URI`  (deve bater com o app OAuth no Smartsheet)
- `ID_FOLHA_CADASTRO`
- `ID_FOLHA_SOLICITACOES`

Opcional:
- `SMARTSHEET_SCOPES` (default: `READ_SHEETS WRITE_SHEETS`)
- `LOG_LEVEL` (default: `INFO`)

## Smartsheet (novo App OAuth)

Se você vai criar **um novo app no Smartsheet** para testar:
- Basta trocar `SMARTSHEET_CLIENT_ID`, `SMARTSHEET_CLIENT_SECRET` e `SMARTSHEET_REDIRECT_URI` no Render.
- **Não precisa mudar mais nada no código**, desde que:
  - o novo Redirect URI esteja cadastrado no Smartsheet
  - o app tenha os mesmos scopes necessários
  - as folhas (IDs) sejam as mesmas (ou novas, se você quiser testar isolado)

Se você quiser testar 100% isolado, use também **novos IDs de folhas** (`ID_FOLHA_CADASTRO` e `ID_FOLHA_SOLICITACOES`) apontando para cópias.
