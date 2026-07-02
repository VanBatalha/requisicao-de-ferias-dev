# Pasta migracao

Esta pasta guarda scripts e SQLs de apoio para migração, recálculo, validação e limpeza da base.

## scripts/

```text
recalcular_saldo_periodo.py
  Recalcula saldo_periodo e periodo_aquisitivo_origem usando dados do banco.

import_data.py
  Apoio para importações históricas. Desde a V43, não importa saldos consolidados para colaborador_complemento.

repair_colaborador_complemento.py
  Apoio para correções pontuais em colaborador_complemento.
```

## sql/

```text
migracao_v29_saldo_periodo.sql
  Criação/ajuste da tabela saldo_periodo e periodo_aquisitivo_origem.

migracao_v30_gestores_matricula.sql
  Criação/ajuste dos campos gestor_direto e gestor_superior.

migracao_v31_observacoes.sql
  Ajustes complementares de observações/validações.

v43_drop_saldos_colaborador_complemento.sql
  Remove os campos obsoletos de saldos/total da tabela colaborador_complemento.

validacao_v43_colaborador_complemento_sem_saldos.sql
  Valida a remoção das colunas e confere a tabela oficial saldo_periodo.

validacao_*.sql
  Consultas de conferência após migrações.
```

Os scripts desta pasta não fazem parte do carregamento normal do Web Service no Render.
