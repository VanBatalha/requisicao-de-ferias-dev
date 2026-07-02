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

## Colunas oficiais de saldo

A tabela `saldo_periodo` guarda o saldo vivo por matrícula, período aquisitivo e tipo de saldo:

```text
colaborador_matricula
tipo_saldo
periodo_numero
data_inicio
data_fim
saldo_inicial
saldo_utilizado
saldo_reservado
saldo_disponivel
```

## Como consultar saldo consolidado

Para consolidar o saldo de uma matrícula, some os campos abaixo por `colaborador_matricula` e `tipo_saldo`:

```sql
SELECT
    colaborador_matricula,
    tipo_saldo,
    SUM(saldo_inicial) AS saldo_inicial,
    SUM(saldo_utilizado) AS saldo_utilizado,
    SUM(saldo_reservado) AS saldo_reservado,
    SUM(saldo_disponivel) AS saldo_disponivel
FROM app_ferias.saldo_periodo
WHERE colaborador_matricula = 'MAT00000'
GROUP BY colaborador_matricula, tipo_saldo;
```

## Como o saldo é exibido no app

Na tela `/ferias`, o app chama:

```text
get_resumo_ferias_por_matricula_postgres(matricula)
```

Arquivo relacionado:

```text
ferias_app/services/postgres_compat_service.py
```

## Tabelas relacionadas

```text
colaboradores
  Cadastro principal. Usado para matrícula, nome, e-mail informativo e status.

colaborador_complemento
  Usado somente para user_type, ativo_no_app, gestor_direto, gestor_superior e flags.
  Não guarda mais saldos consolidados.

solicitacoes_ferias
  Histórico oficial dos eventos: gozo, venda, ajuste e status.

saldo_periodo
  Saldo oficial por matrícula, período aquisitivo e tipo de saldo.
```

## Colunas removidas da tabela colaborador_complemento na V43

As colunas abaixo eram cache/duplicação e foram descontinuadas:

```text
saldo_regular_direito
saldo_regular_usado
saldo_regular_reservado
saldo_regular_disponivel
saldo_premium_direito
saldo_premium_usado
saldo_premium_reservado
saldo_premium_disponivel
total_solicitacoes
periodo_aquisitivo_atual
```

Script relacionado:

```text
migracao/sql/v43_drop_saldos_colaborador_complemento.sql
migracao/sql/validacao_v43_colaborador_complemento_sem_saldos.sql
```

## Regras importantes

- Colaboradores inativos não devem aparecer em listas operacionais de solicitação.
- E-mail é apenas informação visual e compatibilidade de login.
- Seleção, saldo, histórico e criação de solicitação devem usar matrícula.
- Ajustes negativos podem deixar `saldo_disponivel` negativo.
