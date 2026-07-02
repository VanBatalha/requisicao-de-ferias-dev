-- V43 - Validação após remover colunas obsoletas de colaborador_complemento.

-- 1) Deve retornar zero linhas.
SELECT column_name
  FROM information_schema.columns
 WHERE table_schema = 'app_ferias'
   AND table_name = 'colaborador_complemento'
   AND column_name IN (
       'saldo_regular_direito',
       'saldo_regular_usado',
       'saldo_regular_reservado',
       'saldo_regular_disponivel',
       'saldo_premium_direito',
       'saldo_premium_usado',
       'saldo_premium_reservado',
       'saldo_premium_disponivel',
       'total_solicitacoes',
       'periodo_aquisitivo_atual'
   )
 ORDER BY column_name;

-- 2) Confere se a tabela oficial de saldos possui dados.
SELECT
    COUNT(*) AS total_registros_saldo_periodo,
    COUNT(DISTINCT colaborador_matricula) AS total_colaboradores_com_saldo
FROM app_ferias.saldo_periodo;

-- 3) Exemplo de saldo consolidado por matrícula/tipo direto da fonte oficial.
SELECT
    colaborador_matricula,
    tipo_saldo,
    SUM(saldo_inicial) AS saldo_inicial,
    SUM(saldo_utilizado) AS saldo_utilizado,
    SUM(saldo_reservado) AS saldo_reservado,
    SUM(saldo_disponivel) AS saldo_disponivel
FROM app_ferias.saldo_periodo
GROUP BY colaborador_matricula, tipo_saldo
ORDER BY colaborador_matricula, tipo_saldo
LIMIT 30;
