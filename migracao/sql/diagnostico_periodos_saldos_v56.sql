-- DIAGNOSTICO V56 - NAO ALTERA DADOS
-- Execute inteiro e envie os resultados caso a correcao V56 apresente erro.

WITH schemas AS (
    SELECT n.nspname AS schema_name
      FROM pg_namespace n
     WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
       AND to_regclass(format('%I.%I', n.nspname, 'colaboradores')) IS NOT NULL
       AND to_regclass(format('%I.%I', n.nspname, 'saldo_periodo')) IS NOT NULL
       AND to_regclass(format('%I.%I', n.nspname, 'periodos_aquisitivos')) IS NOT NULL
)
SELECT current_database() AS banco,
       current_user AS usuario,
       current_setting('search_path') AS search_path,
       string_agg(schema_name, ', ' ORDER BY schema_name) AS schemas_do_app
  FROM schemas;

SELECT table_schema,
       table_name
  FROM information_schema.tables
 WHERE table_name IN ('colaboradores', 'saldo_periodo', 'periodos_aquisitivos', 'solicitacoes_ferias', 'sync_state')
 ORDER BY table_schema, table_name;

-- Ajuste app_ferias abaixo apenas se o primeiro resultado mostrar outro schema.
SELECT 'ativos' AS item, count(*) AS quantidade
  FROM app_ferias.colaboradores
 WHERE upper(trim(coalesce(status, ''))) IN ('ATIVO', 'ACTIVE')
UNION ALL
SELECT 'saldo_periodo_total', count(*) FROM app_ferias.saldo_periodo
UNION ALL
SELECT 'premium_total', count(*) FROM app_ferias.saldo_periodo WHERE upper(trim(tipo_saldo)) = 'PREMIUM'
UNION ALL
SELECT 'historicos_com_saldo', count(*)
  FROM app_ferias.saldo_periodo
 WHERE coalesce(is_atual, false) = false
   AND (
       coalesce(saldo_inicial, 0) <> 0
       OR coalesce(saldo_utilizado, 0) <> 0
       OR coalesce(saldo_reservado, 0) <> 0
       OR coalesce(saldo_disponivel, 0) <> 0
   );

SELECT id,
       colaborador_matricula,
       periodo_numero,
       data_inicio,
       data_fim,
       is_atual,
       tipo_saldo,
       saldo_inicial,
       saldo_utilizado,
       saldo_reservado,
       saldo_disponivel,
       updated_at
  FROM app_ferias.saldo_periodo
 WHERE upper(trim(colaborador_matricula)) = 'MAT00116'
 ORDER BY tipo_saldo, periodo_numero, id;

SELECT sync_name,
       last_success_at,
       last_status,
       last_error,
       extra,
       updated_at
  FROM app_ferias.sync_state
 ORDER BY updated_at DESC NULLS LAST;

SELECT id,
       actor_email,
       action,
       created_at,
       after_data,
       context
  FROM app_ferias.auditoria
 WHERE action LIKE '%V54%'
    OR action LIKE '%V55%'
    OR action LIKE '%V56%'
 ORDER BY id DESC
 LIMIT 20;
