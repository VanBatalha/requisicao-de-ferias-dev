# Saldos por matrícula

## Fonte oficial

A fonte oficial de saldo é:

```text
app_ferias.saldo_periodo
```

A chave operacional é:

```text
colaborador_matricula
```

E não o e-mail.

## Como o saldo é exibido

Na tela `/ferias`, o app chama:

```text
get_resumo_ferias_por_matricula_postgres(matricula)
```

Essa função soma, por matrícula e `tipo_saldo`:

```text
saldo_inicial
saldo_utilizado
saldo_reservado
saldo_disponivel
```

## Tabelas relacionadas

```text
colaboradores
  Cadastro principal. Usado para matrícula, nome, e-mail informativo e status.

colaborador_complemento
  Usado para user_type, ativo_no_app, gestor_direto e gestor_superior.

solicitacoes_ferias
  Histórico oficial dos eventos: gozo, venda, ajuste e status.

saldo_periodo
  Saldo vivo por matrícula, período aquisitivo e tipo de saldo.
```

## Regras importantes

- Colaboradores inativos não devem aparecer em listas operacionais de solicitação.
- E-mail é apenas informação visual e compatibilidade de login.
- Seleção, saldo, histórico e criação de solicitação devem usar matrícula.
- Ajustes negativos podem deixar `saldo_disponivel` negativo.
