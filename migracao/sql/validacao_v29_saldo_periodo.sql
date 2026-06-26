-- Validação V29 - saldo_periodo e periodo_aquisitivo_origem

SELECT 'colaboradores' AS item, COUNT(*) AS qtd FROM app_ferias.colaboradores
UNION ALL SELECT 'solicitacoes_ferias', COUNT(*) FROM app_ferias.solicitacoes_ferias
UNION ALL SELECT 'solicitacoes_com_periodo_origem', COUNT(*) FROM app_ferias.solicitacoes_ferias WHERE periodo_aquisitivo_origem IS NOT NULL AND periodo_aquisitivo_origem <> ''
UNION ALL SELECT 'saldo_periodo', COUNT(*) FROM app_ferias.saldo_periodo
UNION ALL SELECT 'saldo_periodo_regular', COUNT(*) FROM app_ferias.saldo_periodo WHERE tipo_saldo = 'REGULAR'
UNION ALL SELECT 'saldo_periodo_premium', COUNT(*) FROM app_ferias.saldo_periodo WHERE tipo_saldo = 'PREMIUM';

SELECT
    colaborador_matricula,
    tipo_saldo,
    COUNT(*) AS periodos,
    SUM(saldo_inicial) AS saldo_inicial,
    SUM(saldo_utilizado) AS saldo_utilizado,
    SUM(saldo_reservado) AS saldo_reservado,
    SUM(saldo_disponivel) AS saldo_disponivel
FROM app_ferias.saldo_periodo
GROUP BY colaborador_matricula, tipo_saldo
ORDER BY colaborador_matricula, tipo_saldo
LIMIT 50;

SELECT
    c.matricula,
    c.email,
    c.nome_completo,
    v.regular_direito,
    v.regular_usado,
    v.regular_reservado,
    v.regular_disponivel,
    v.premium_direito,
    v.premium_usado,
    v.premium_reservado,
    v.premium_disponivel
FROM app_ferias.colaboradores c
LEFT JOIN app_ferias.vw_saldo_colaborador v
    ON v.colaborador_matricula = c.matricula
WHERE lower(c.email) IN ('vanderson.batalha@certare.com.br')
   OR c.matricula IN ('MAT00027', 'MAT00832')
ORDER BY c.matricula;

SELECT
    id,
    colaborador_matricula,
    colaborador_email,
    solicitacao,
    saldo_tipo,
    status,
    dias_solicitados,
    periodo_aquisitivo_origem
FROM app_ferias.solicitacoes_ferias
WHERE periodo_aquisitivo_origem IS NOT NULL
  AND periodo_aquisitivo_origem <> ''
ORDER BY data_inicio DESC, id DESC
LIMIT 100;
