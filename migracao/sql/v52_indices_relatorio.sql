-- Índices opcionais para o relatório e a manutenção por matrícula.
-- Execute manualmente no pgAdmin fora de uma transação explícita.

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_solicitacoes_ferias_relatorio
    ON app_ferias.solicitacoes_ferias (is_ajuste, data_inicio, colaborador_matricula);

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_hierarquia_gestor_direto_matricula
    ON app_ferias.hierarquia_gestao (gestor_direto_matricula, colaborador_matricula);

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_hierarquia_gestor_superior_matricula
    ON app_ferias.hierarquia_gestao (gestor_superior_matricula, colaborador_matricula);

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_saldo_periodo_matricula_tipo_periodo
    ON app_ferias.saldo_periodo (colaborador_matricula, tipo_saldo, periodo_numero);
