-- Validacao V31 - saldo_periodo e saldo negativo/por matricula

-- 1) Totais principais
SELECT 'colaboradores' AS tabela, COUNT(*) FROM app_ferias.colaboradores
UNION ALL
SELECT 'solicitacoes_ferias', COUNT(*) FROM app_ferias.solicitacoes_ferias
UNION ALL
SELECT 'saldo_periodo', COUNT(*) FROM app_ferias.saldo_periodo;

-- 2) Verificar saldo por colaborador/matricula
SELECT
    c.matricula,
    c.nome_completo,
    sp.tipo_saldo,
    SUM(sp.saldo_inicial) AS saldo_inicial,
    SUM(sp.saldo_utilizado) AS saldo_utilizado,
    SUM(sp.saldo_reservado) AS saldo_reservado,
    SUM(sp.saldo_disponivel) AS saldo_disponivel
FROM app_ferias.colaboradores c
JOIN app_ferias.saldo_periodo sp
    ON sp.colaborador_matricula = c.matricula
GROUP BY c.matricula, c.nome_completo, sp.tipo_saldo
ORDER BY c.nome_completo, sp.tipo_saldo;

-- 3) Exemplo: Adriano / MAT00061
SELECT
    c.matricula,
    c.nome_completo,
    sp.periodo_numero,
    sp.tipo_saldo,
    sp.data_inicio,
    sp.data_fim,
    sp.saldo_inicial,
    sp.saldo_utilizado,
    sp.saldo_reservado,
    sp.saldo_disponivel
FROM app_ferias.colaboradores c
JOIN app_ferias.saldo_periodo sp
    ON sp.colaborador_matricula = c.matricula
WHERE c.matricula = 'MAT00061'
ORDER BY sp.tipo_saldo, sp.periodo_numero;

-- 4) Solicitações/ajustes do Adriano e origem por período
SELECT
    id,
    colaborador_matricula,
    colaborador_email,
    solicitacao,
    saldo_tipo,
    dias,
    status,
    periodo_aquisitivo_origem,
    data_inicio,
    metadata
FROM app_ferias.solicitacoes_ferias
WHERE colaborador_matricula = 'MAT00061'
ORDER BY data_inicio, id;

-- 5) Colaboradores com saldo regular negativo
SELECT
    c.matricula,
    c.nome_completo,
    SUM(sp.saldo_disponivel) AS saldo_regular_disponivel
FROM app_ferias.colaboradores c
JOIN app_ferias.saldo_periodo sp
    ON sp.colaborador_matricula = c.matricula
WHERE sp.tipo_saldo = 'REGULAR'
GROUP BY c.matricula, c.nome_completo
HAVING SUM(sp.saldo_disponivel) < 0
ORDER BY saldo_regular_disponivel ASC;
