# Migração V12 - app_ferias por matrícula

## Objetivo
Esta versão usa o schema `app_ferias` e passa a tratar `colaboradores.matricula` como o identificador de negócio do colaborador. Novas solicitações, períodos, saldos e auditorias passam a gravar também os campos de matrícula.

## Ordem recomendada
1. Faça backup do banco no Render/pgAdmin.
2. Execute no pgAdmin o arquivo `script_app_ferias_matricula_v12.sql`.
3. Rode as consultas de conferência ao final do script.
4. Configure no Render:
   - `DB_SCHEMA=app_ferias`
   - `DATABASE_URL=<sua URL PostgreSQL>`
5. Suba esta versão do app.
6. Faça logout/login e teste primeiro com um colaborador conhecido.

## Conferência rápida
```sql
SELECT COUNT(*) FROM app_ferias.colaboradores;
SELECT COUNT(*) FROM app_ferias.solicitacoes_ferias;
SELECT tipo_saldo,
       COUNT(*) AS linhas,
       SUM(dias_direito - dias_usados - dias_reservados) AS saldo_disponivel
FROM app_ferias.saldos_periodo
GROUP BY tipo_saldo;
```

## Observação
A tabela `colaborador_complemento` foi mantida como cache de compatibilidade para telas/serviços estáveis. A base principal de saldo passa a ser `periodos_aquisitivos` + `saldos_periodo`.
