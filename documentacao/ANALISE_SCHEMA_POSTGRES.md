# Diagnóstico - schema PostgreSQL

Os prints mostram que o usuário Vanderson existe em `ferias_app.colaboradores` e que `ferias_app.colaborador_complemento.user_type = ADMIN`.

Se a interface ainda mostra `USUARIO`, o problema mais provável é que a aplicação Flask/SQLAlchemy esteja consultando o schema padrão da conexão, geralmente `public`, enquanto os dados reais estão no schema `ferias_app`.

Ajuste aplicado nesta V3:

- O `postgres_service.py` agora define `search_path` para `ferias_app, public` em toda conexão.
- O schema pode ser customizado pela variável `DB_SCHEMA`; se ela não existir, usa `ferias_app`.
- `import_data.py` e `repair_colaborador_complemento.py` também foram ajustados para usar o mesmo schema.

Variável opcional no Render:

```text
DB_SCHEMA=ferias_app
```

Depois do deploy, faça logout/login para recalcular o contexto da sessão.
