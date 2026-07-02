# Rotas e performance

## Objetivo

A rota `/ferias` deve carregar rapidamente usando somente dados necessários do PostgreSQL. A tela não deve consultar Smartsheet nem executar cálculos pesados a cada troca de colaborador.

## Fluxo esperado ao selecionar colaborador

Ao escolher um colaborador na tela de solicitações, o front-end deve navegar para:

```text
GET /ferias?matricula=MAT00000
```

A matrícula é a chave operacional. O parâmetro antigo `colaborador=email` não deve ser usado para seleção.

## Consultas principais da rota `/ferias`

### 1. Lista de colaboradores do escopo

Arquivo relacionado:

```text
ferias_app/blueprints/pages.py
ferias_app/services/postgres_compat_service.py
```

Função principal:

```text
listar_colaboradores_opcoes_ferias_postgres(usuario_email, role)
```

A função busca apenas colunas leves:

```text
matricula
nome_completo
email
status
gestor_direto
gestor_superior
ativo_no_app
```

Regras de escopo:

- `ADMIN`: vê todos os colaboradores ativos.
- `DP`: vê colaboradores com `gestor_direto = DP` ou `gestor_superior = DP`, além de vínculos diretos à matrícula do DP se existirem.
- `GESTOR`: vê colaboradores onde sua matrícula aparece em `gestor_direto` ou `gestor_superior`.
- Quando `gestor_superior = GESTOR`, o responsável operacional é o gestor informado em `gestor_direto`.

### 2. Resumo de saldo

Arquivo relacionado:

```text
ferias_app/services/postgres_compat_service.py
```

Função principal:

```text
get_resumo_ferias_por_matricula_postgres(matricula)
```

A função lê direto da tabela oficial:

```text
app_ferias.saldo_periodo
```

E soma por matrícula e tipo de saldo:

```text
saldo_inicial
saldo_utilizado
saldo_reservado
saldo_disponivel
```

### 3. Histórico do colaborador

Arquivo relacionado:

```text
ferias_app/services/postgres_compat_service.py
```

Função principal:

```text
listar_solicitacoes_matricula_postgres(matricula)
```

A função lista apenas solicitações da matrícula selecionada e limita o histórico para evitar carregar tudo na troca de colaborador.

## Log de performance

A rota `/ferias` registra no Render:

```text
FERIAS_PERF matricula=MAT00000 opcoes=123 escopo=GESTOR list=0.050s resumo=0.020s hist=0.030s total=0.120s
```

Use esse log para localizar gargalos:

- `list`: tempo para listar colaboradores do escopo.
- `resumo`: tempo para buscar saldo em `saldo_periodo`.
- `hist`: tempo para buscar histórico da matrícula.
- `total`: tempo total da rota.
