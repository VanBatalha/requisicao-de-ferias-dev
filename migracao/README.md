# Pasta migracao

Esta pasta guarda scripts e SQLs de apoio para migração, recálculo e validação.

## scripts/

```text
recalcular_saldo_periodo.py
  Recalcula saldo_periodo e periodo_aquisitivo_origem usando dados do banco.

import_data.py
  Apoio para importações históricas.

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

validacao_*.sql
  Consultas de conferência após migrações.
```

Os scripts desta pasta não fazem parte do carregamento normal do Web Service.
