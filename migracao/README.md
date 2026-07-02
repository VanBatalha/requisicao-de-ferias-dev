# Migração

Esta pasta reúne scripts manuais e SQLs de apoio.

## Scripts

- `scripts/recalcular_saldo_periodo.py` - recalcula `saldo_periodo` a partir das solicitações existentes.
- `scripts/import_data.py` - importação legada; usar apenas para cenários de migração controlada.
- `scripts/repair_colaborador_complemento.py` - reparos pontuais de complemento/hierarquia.

## SQLs principais

- `sql/v43_drop_saldos_colaborador_complemento.sql` - remove colunas antigas de saldo em `colaborador_complemento`.
- `sql/v44_hierarquia_matricula_sem_email_custom.sql` - remove estrutura antiga `EMAIL_CUSTOM` e padroniza hierarquia por matrícula.
- `sql/validacao_v44_hierarquia_matricula.sql` - valida estrutura de hierarquia atual.

## Regra atual

Cadastro vem da planilha `1745799836133252` e é gravado por matrícula.
Permissões ficam em `permissoes_usuario` e saldos ficam em `saldo_periodo`.
