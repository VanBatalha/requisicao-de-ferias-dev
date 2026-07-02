# Migracao

## Scripts SQL atuais

- `sql/v43_drop_saldos_colaborador_complemento.sql`: remove saldos antigos de `colaborador_complemento`.
- `sql/validacao_v43_colaborador_complemento_sem_saldos.sql`: valida a remocao dos saldos antigos.
- `sql/v44_pre_add_gestor_superior_email.sql`: pre-deploy seguro para adicionar a coluna nova antes de subir a V44.
- `sql/v44_hierarquia_matricula_sem_email_custom.sql`: remove `gestor_superior_tipo`/`gestor_superior_email_custom` e padroniza hierarquia por matricula/marcadores.
- `sql/validacao_v44_hierarquia_matricula.sql`: valida a estrutura V44.

## Scripts Python

- `scripts/import_data.py`: importacao a partir de exportacao XLSX.
- `scripts/recalcular_saldo_periodo.py`: recalculo manual de `saldo_periodo`.
- `scripts/repair_colaborador_complemento.py`: reparo legado, manter apenas para historico operacional.
