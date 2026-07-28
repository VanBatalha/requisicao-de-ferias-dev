# Exportação ordenada do PostgreSQL para XLSX

## Por que o pgAdmin pode exportar fora de ordem

Tabelas relacionais não possuem uma ordem natural. Mesmo que a coluna `id` seja crescente, um `SELECT` ou uma exportação sem `ORDER BY` pode devolver os registros em qualquer ordem física.

Renomear ou reorganizar fisicamente a tabela não garante a ordenação de futuras exportações. A garantia só existe quando a consulta de exportação possui `ORDER BY`.

## Exportador incluído na V55

Use o arquivo:

```text
export_database_xlsx.py
```

Ele:

- exporta todas as tabelas do schema configurado;
- ordena cada tabela pela coluna `id`, quando ela existe;
- se não houver `id`, usa a chave primária;
- organiza as tabelas válidas primeiro;
- coloca tabelas `z_` e `z_backup_` por último;
- cria uma aba `INDICE` com a tabela, nome da aba, quantidade de linhas e critério de ordenação.

### No Docker/Contabo

```bash
mkdir -p exports
docker compose exec -T app python export_database_xlsx.py \
  /app/exports/export_app_ferias_$(date +%Y%m%d_%H%M%S).xlsx
```

O arquivo ficará na pasta `exports` do servidor.

### Fora do Docker

Com as mesmas variáveis de ambiente da aplicação:

```bash
python export_database_xlsx.py export_app_ferias.xlsx
```

## Consultas manuais no pgAdmin

Para uma tabela específica, use sempre:

```sql
SELECT *
FROM app_ferias.nome_da_tabela
ORDER BY id ASC;
```

Nas tabelas sem coluna `id`, ordene pelas colunas da chave primária. Exemplo:

```sql
SELECT *
FROM app_ferias.permissoes_usuario
ORDER BY colaborador_id, role;
```
