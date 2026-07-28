-- CORRECAO V55 - SOMENTE O PERIODO VIGENTE POSSUI SALDO
--
-- Regras aplicadas aos colaboradores ATIVOS:
--   REGULAR: somente o ultimo ciclo anual concluido possui saldo; base de 30 dias.
--   PREMIUM: P1 apos 5 anos possui 30 dias; P2+ a cada 30 meses possui 15 dias.
--   Nenhum saldo e carregado de um periodo antigo para o novo periodo.
--   Linhas historicas P1..PX permanecem para consulta, mas ficam integralmente zeradas.
--
-- O saldo vigente já existente é preservado. A V55 apenas remove qualquer
-- valor das linhas históricas e impede carregamento para os próximos ciclos.
--
-- IMPORTANTE: faca backup do banco antes de executar.

BEGIN;

SET LOCAL search_path TO app_ferias, public;
SET LOCAL TIME ZONE 'America/Fortaleza';
SET LOCAL lock_timeout = '10s';
SET LOCAL statement_timeout = '180s';

-- Impede duas normalizacoes simultaneas.
SELECT pg_advisory_xact_lock(5500728);

CREATE TEMP TABLE v55_cfg (ref_date date NOT NULL) ON COMMIT DROP;
INSERT INTO v55_cfg VALUES (CURRENT_DATE);

-- Coloca backups antigos no final da listagem alfabetica do schema.
-- Ex.: backup_v54... -> z_backup_v54...
DO $$
DECLARE
    r record;
    novo_nome text;
BEGIN
    FOR r IN
        SELECT tablename
          FROM pg_tables
         WHERE schemaname = current_schema()
           AND tablename LIKE 'backup_%'
    LOOP
        novo_nome := left('z_' || r.tablename, 63);
        IF to_regclass(format('%I.%I', current_schema(), novo_nome)) IS NULL THEN
            EXECUTE format(
                'ALTER TABLE %I.%I RENAME TO %I',
                current_schema(), r.tablename, novo_nome
            );
        END IF;
    END LOOP;
END $$;

-- Backups logicos da V55. CREATE TABLE IF NOT EXISTS preserva a primeira copia.
CREATE TABLE IF NOT EXISTS z_backup_v55_saldo_periodo AS TABLE saldo_periodo;
CREATE TABLE IF NOT EXISTS z_backup_v55_periodos_aquisitivos AS TABLE periodos_aquisitivos;
CREATE TABLE IF NOT EXISTS z_backup_v55_solicitacoes_origem AS
SELECT id, colaborador_matricula, saldo_tipo, tipo_ferias, data_inicio,
       dias, dias_solicitados, status, is_ajuste, tipo_solicitacao,
       solicitacao, periodo_aquisitivo_origem, metadata
  FROM solicitacoes_ferias;

-- Cadastro ativo que pode receber ciclos.
CREATE TEMP TABLE v55_active ON COMMIT DROP AS
SELECT c.id AS colaborador_id,
       upper(trim(c.matricula)) AS matricula,
       c.nome_completo,
       c.data_admissao::date AS data_admissao,
       cfg.ref_date
  FROM colaboradores c
 CROSS JOIN v55_cfg cfg
 WHERE upper(coalesce(c.status, 'ATIVO')) IN ('ATIVO', 'ACTIVE')
   AND nullif(trim(c.matricula), '') IS NOT NULL
   AND c.data_admissao IS NOT NULL;

-- Ciclos REGULARES efetivamente adquiridos. O periodo em formacao nao e criado.
CREATE TEMP TABLE v55_regular_cycles ON COMMIT DROP AS
WITH ciclos AS (
    SELECT a.colaborador_id,
           a.matricula,
           gs AS periodo_numero,
           (a.data_admissao + make_interval(years => gs - 1))::date AS data_inicio,
           ((a.data_admissao + make_interval(years => gs))::date - 1) AS data_fim,
           (a.data_admissao + make_interval(years => gs))::date AS credito_em,
           (a.data_admissao + make_interval(years => gs + 1))::date AS proximo_credito_em
      FROM v55_active a
     CROSS JOIN LATERAL generate_series(1, 80) gs
     WHERE (a.data_admissao + make_interval(years => gs))::date <= a.ref_date
)
SELECT c.*,
       c.periodo_numero = max(c.periodo_numero) OVER (PARTITION BY c.matricula) AS is_atual,
       30::numeric(10,2) AS base_credito,
       'REGULAR'::varchar(20) AS tipo_saldo
  FROM ciclos c;

-- Ciclos PREMIUM independentes da numeracao REGULAR.
-- P1: 30 dias no dia seguinte ao fechamento de 5 anos.
-- P2+: 15 dias a cada 30 meses.
CREATE TEMP TABLE v55_premium_cycles ON COMMIT DROP AS
WITH base AS (
    SELECT a.*,
           (a.data_admissao + interval '5 years' + interval '1 day')::date AS primeiro_credito
      FROM v55_active a
), ciclos AS (
    SELECT b.colaborador_id,
           b.matricula,
           gs AS periodo_numero,
           CASE
             WHEN gs = 1 THEN b.data_admissao
             ELSE (b.primeiro_credito + make_interval(months => (gs - 2) * 30))::date
           END AS data_inicio,
           ((b.primeiro_credito + make_interval(months => (gs - 1) * 30))::date - 1) AS data_fim,
           (b.primeiro_credito + make_interval(months => (gs - 1) * 30))::date AS credito_em,
           (b.primeiro_credito + make_interval(months => gs * 30))::date AS proximo_credito_em,
           CASE WHEN gs = 1 THEN 30::numeric(10,2) ELSE 15::numeric(10,2) END AS base_credito
      FROM base b
     CROSS JOIN LATERAL generate_series(1, 40) gs
     WHERE (b.primeiro_credito + make_interval(months => (gs - 1) * 30))::date <= b.ref_date
)
SELECT c.colaborador_id,
       c.matricula,
       c.periodo_numero,
       c.data_inicio,
       c.data_fim,
       c.credito_em,
       c.proximo_credito_em,
       c.periodo_numero = max(c.periodo_numero) OVER (PARTITION BY c.matricula) AS is_atual,
       c.base_credito,
       'PREMIUM'::varchar(20) AS tipo_saldo
  FROM ciclos c;

CREATE TEMP TABLE v55_all_cycles ON COMMIT DROP AS
SELECT * FROM v55_regular_cycles
UNION ALL
SELECT * FROM v55_premium_cycles;

CREATE TEMP TABLE v55_current_cycles ON COMMIT DROP AS
SELECT * FROM v55_all_cycles WHERE is_atual;

-- Captura o valor do P vigente antes de zerar o histórico.
-- A V55 não reinterpreta ajustes antigos nem soma períodos anteriores: preserva
-- somente o valor que já está na linha do último ciclo adquirido. Se a linha
-- vigente ainda não existir, cria a base padrão da regra.
CREATE TEMP TABLE v55_current_snapshot ON COMMIT DROP AS
SELECT cc.colaborador_id,
       cc.matricula,
       cc.periodo_numero,
       cc.tipo_saldo,
       sp.saldo_inicial,
       sp.saldo_utilizado,
       sp.saldo_reservado
  FROM v55_current_cycles cc
  LEFT JOIN LATERAL (
      SELECT s.saldo_inicial, s.saldo_utilizado, s.saldo_reservado
        FROM saldo_periodo s
       WHERE upper(trim(s.colaborador_matricula)) = cc.matricula
         AND upper(trim(s.tipo_saldo)) = cc.tipo_saldo
         AND s.periodo_numero = cc.periodo_numero
       ORDER BY coalesce(s.is_atual, false) DESC, s.id DESC
       LIMIT 1
  ) sp ON true;

-- Resultado final: histórico zerado e somente o P vigente com saldo.
CREATE TEMP TABLE v55_expected_saldos ON COMMIT DROP AS
SELECT c.colaborador_id,
       c.matricula AS colaborador_matricula,
       c.periodo_numero,
       c.data_inicio,
       c.data_fim,
       c.is_atual,
       c.tipo_saldo,
       CASE WHEN c.is_atual
            THEN coalesce(s.saldo_inicial, c.base_credito)
            ELSE 0::numeric(10,2)
       END::numeric(10,2) AS saldo_inicial,
       CASE WHEN c.is_atual
            THEN coalesce(s.saldo_utilizado, 0)
            ELSE 0::numeric(10,2)
       END::numeric(10,2) AS saldo_utilizado,
       CASE WHEN c.is_atual
            THEN coalesce(s.saldo_reservado, 0)
            ELSE 0::numeric(10,2)
       END::numeric(10,2) AS saldo_reservado,
       CASE WHEN c.is_atual
            THEN coalesce(s.saldo_inicial, c.base_credito)
                 - coalesce(s.saldo_utilizado, 0)
                 - coalesce(s.saldo_reservado, 0)
            ELSE 0::numeric(10,2)
       END::numeric(10,2) AS saldo_disponivel
  FROM v55_all_cycles c
  LEFT JOIN v55_current_snapshot s
    ON s.matricula = c.matricula
   AND s.tipo_saldo = c.tipo_saldo
   AND s.periodo_numero = c.periodo_numero;

-- Zera explicitamente tudo dos ativos antes do upsert. Isso garante que nenhum
-- valor antigo permaneça por diferenca de caixa, espacos ou execucao anterior.
UPDATE saldo_periodo sp
   SET is_atual = false,
       saldo_inicial = 0,
       saldo_utilizado = 0,
       saldo_reservado = 0,
       saldo_disponivel = 0,
       ultima_alteracao = current_timestamp,
       updated_at = current_timestamp
  FROM v55_active a
 WHERE upper(trim(sp.colaborador_matricula)) = a.matricula;

INSERT INTO saldo_periodo (
    colaborador_id, colaborador_matricula, periodo_numero,
    data_inicio, data_fim, is_atual, tipo_saldo,
    saldo_inicial, saldo_utilizado, saldo_reservado, saldo_disponivel,
    ultima_alteracao, created_at, updated_at
)
SELECT colaborador_id, colaborador_matricula, periodo_numero,
       data_inicio, data_fim, is_atual, tipo_saldo,
       saldo_inicial, saldo_utilizado, saldo_reservado, saldo_disponivel,
       current_timestamp, current_timestamp, current_timestamp
  FROM v55_expected_saldos
ON CONFLICT (colaborador_matricula, periodo_numero, tipo_saldo)
DO UPDATE SET
    colaborador_id = EXCLUDED.colaborador_id,
    data_inicio = EXCLUDED.data_inicio,
    data_fim = EXCLUDED.data_fim,
    is_atual = EXCLUDED.is_atual,
    tipo_saldo = EXCLUDED.tipo_saldo,
    saldo_inicial = EXCLUDED.saldo_inicial,
    saldo_utilizado = EXCLUDED.saldo_utilizado,
    saldo_reservado = EXCLUDED.saldo_reservado,
    saldo_disponivel = EXCLUDED.saldo_disponivel,
    ultima_alteracao = current_timestamp,
    updated_at = current_timestamp;

-- Remove somente linhas de ativos que representam ciclos ainda nao adquiridos
-- ou que deixaram de existir pelas novas regras.
DELETE FROM saldo_periodo sp
 USING v55_active a
 WHERE upper(trim(sp.colaborador_matricula)) = a.matricula
   AND NOT EXISTS (
       SELECT 1
         FROM v55_expected_saldos e
        WHERE e.colaborador_matricula = a.matricula
          AND e.periodo_numero = sp.periodo_numero
          AND e.tipo_saldo = upper(trim(sp.tipo_saldo))
   );

-- periodos_aquisitivos armazena apenas os ciclos REGULARES concluidos.
INSERT INTO periodos_aquisitivos (
    colaborador_id, colaborador_matricula, periodo_numero,
    data_inicio, data_fim, is_atual
)
SELECT colaborador_id, matricula, periodo_numero, data_inicio, data_fim, is_atual
  FROM v55_regular_cycles
ON CONFLICT (colaborador_matricula, periodo_numero)
DO UPDATE SET
    colaborador_id = EXCLUDED.colaborador_id,
    data_inicio = EXCLUDED.data_inicio,
    data_fim = EXCLUDED.data_fim,
    is_atual = EXCLUDED.is_atual;

DELETE FROM periodos_aquisitivos pa
 USING v55_active a
 WHERE upper(trim(pa.colaborador_matricula)) = a.matricula
   AND NOT EXISTS (
       SELECT 1
         FROM v55_regular_cycles rc
        WHERE rc.matricula = a.matricula
          AND rc.periodo_numero = pa.periodo_numero
   );

-- Atualiza o estado para que a V55 execute sua propria verificacao diaria.
DELETE FROM sync_state WHERE sync_name = 'ciclos_saldos_v55';

INSERT INTO auditoria (
    actor_email, action, entity_type, entity_id,
    before_data, after_data, context, created_at
)
SELECT 'pgadmin-v55',
       'NORMALIZAR_SALDO_SOMENTE_PERIODO_VIGENTE_V55',
       'saldo_periodo',
       0,
       NULL,
       jsonb_build_object(
           'reference_date', cfg.ref_date,
           'active_collaborators', (SELECT count(*) FROM v55_active),
           'expected_balance_rows', (SELECT count(*) FROM v55_expected_saldos),
           'rule_regular', '30 dias somente no ultimo periodo anual adquirido; sem carregamento do P anterior',
           'rule_premium', 'P1 30 dias apos 5 anos; P2+ 15 dias a cada 30 meses; sem carregamento do P anterior'
       ),
       jsonb_build_object('script', 'correcao_saldos_v55_apenas_periodo_vigente.sql'),
       current_timestamp
  FROM v55_cfg cfg;

-- Conferencias. Todos os resultados de erro devem ser zero.
SELECT 'ativos processados' AS item, count(*)::text AS valor FROM v55_active
UNION ALL
SELECT 'linhas de saldo esperadas', count(*)::text FROM v55_expected_saldos
UNION ALL
SELECT 'historicos com valor diferente de zero (ERRO)', count(*)::text
  FROM saldo_periodo sp
  JOIN v55_active a ON upper(trim(sp.colaborador_matricula)) = a.matricula
 WHERE coalesce(sp.is_atual, false) = false
   AND (
       coalesce(sp.saldo_inicial, 0) <> 0
       OR coalesce(sp.saldo_utilizado, 0) <> 0
       OR coalesce(sp.saldo_reservado, 0) <> 0
       OR coalesce(sp.saldo_disponivel, 0) <> 0
   )
UNION ALL
SELECT 'matricula/tipo com mais de um vigente (ERRO)', count(*)::text
  FROM (
      SELECT sp.colaborador_matricula, upper(trim(sp.tipo_saldo)) AS tipo
        FROM saldo_periodo sp
        JOIN v55_active a ON upper(trim(sp.colaborador_matricula)) = a.matricula
       WHERE coalesce(sp.is_atual, false) = true
       GROUP BY sp.colaborador_matricula, upper(trim(sp.tipo_saldo))
      HAVING count(*) > 1
  ) d
UNION ALL
SELECT 'saldos vigentes negativos (REVISAR)', count(*)::text
  FROM saldo_periodo sp
  JOIN v55_active a ON upper(trim(sp.colaborador_matricula)) = a.matricula
 WHERE coalesce(sp.is_atual, false) = true
   AND coalesce(sp.saldo_disponivel, 0) < 0;

-- Conferencia do exemplo informado.
SELECT id, colaborador_matricula, periodo_numero, data_inicio, data_fim,
       is_atual, tipo_saldo, saldo_inicial, saldo_utilizado,
       saldo_reservado, saldo_disponivel
  FROM saldo_periodo
 WHERE upper(trim(colaborador_matricula)) = 'MAT00116'
 ORDER BY tipo_saldo, periodo_numero, id;

COMMIT;

-- ROLLBACK MANUAL DA V55, SE NECESSARIO:
-- BEGIN;
-- SET LOCAL search_path TO app_ferias, public;
-- DELETE FROM saldo_periodo;
-- INSERT INTO saldo_periodo SELECT * FROM z_backup_v55_saldo_periodo;
-- DELETE FROM periodos_aquisitivos;
-- INSERT INTO periodos_aquisitivos SELECT * FROM z_backup_v55_periodos_aquisitivos;
-- SELECT setval(pg_get_serial_sequence('saldo_periodo','id'), coalesce((SELECT max(id) FROM saldo_periodo),1), true);
-- SELECT setval(pg_get_serial_sequence('periodos_aquisitivos','id'), coalesce((SELECT max(id) FROM periodos_aquisitivos),1), true);
-- COMMIT;
