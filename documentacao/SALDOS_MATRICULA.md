# Saldos por matricula

A fonte oficial de saldo e `app_ferias.saldo_periodo`.

A tabela `colaborador_complemento` nao deve armazenar saldo consolidado. Ela fica apenas com permissao, flags e hierarquia operacional.

Consulta consolidada:

```sql
SELECT
    colaborador_matricula,
    tipo_saldo,
    SUM(saldo_inicial) AS saldo_inicial,
    SUM(saldo_utilizado) AS saldo_utilizado,
    SUM(saldo_reservado) AS saldo_reservado,
    SUM(saldo_disponivel) AS saldo_disponivel
FROM app_ferias.saldo_periodo
GROUP BY colaborador_matricula, tipo_saldo;
```

Arquivos relacionados:

- `ferias_app/services/postgres_compat_service.py`
- `ferias_app/services/smartsheet_sync_service.py`
- `ferias_app/services/saldo_service.py`
- `migracao/sql/v43_drop_saldos_colaborador_complemento.sql`
