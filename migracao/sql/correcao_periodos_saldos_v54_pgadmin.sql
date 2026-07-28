-- CORRECAO V54 - PERIODOS DE FERIAS E LICENCA CERTARIANA
-- Data de referencia usada para esta correcao: 28/07/2026.
-- Antes de executar, confirme o schema abaixo. No app oficial, o padrao e app_ferias.
-- O script cria backups, trabalha em uma unica transacao e afeta somente colaboradores ATIVOS.

BEGIN;

SET LOCAL search_path TO app_ferias, public;
SET LOCAL lock_timeout = '10s';
SET LOCAL statement_timeout = '180s';

-- Impede duas correcoes simultaneas.
SELECT pg_advisory_xact_lock(5400728);

CREATE TEMP TABLE v54_cfg (ref_date date NOT NULL) ON COMMIT DROP;
INSERT INTO v54_cfg VALUES (DATE '2026-07-28');

-- Backups logicos. Se o script for executado novamente, os primeiros backups sao preservados.
CREATE TABLE IF NOT EXISTS backup_v54_20260728_saldo_periodo AS TABLE saldo_periodo;
CREATE TABLE IF NOT EXISTS backup_v54_20260728_periodos_aquisitivos AS TABLE periodos_aquisitivos;
CREATE TABLE IF NOT EXISTS backup_v54_20260728_saldos_periodo AS TABLE saldos_periodo;
CREATE TABLE IF NOT EXISTS backup_v54_20260728_solicitacoes_origem AS
SELECT id, colaborador_matricula, saldo_tipo, tipo_ferias, data_inicio,
       dias, dias_solicitados, periodo_aquisitivo_origem, metadata
  FROM solicitacoes_ferias;

-- Colaboradores ativos e quantidade de ciclos efetivamente adquiridos.
CREATE TEMP TABLE v54_active ON COMMIT DROP AS
SELECT c.id AS colaborador_id,
       upper(trim(c.matricula)) AS matricula,
       c.nome_completo,
       c.data_admissao::date AS data_admissao,
       cfg.ref_date,
       GREATEST(EXTRACT(YEAR FROM age(cfg.ref_date, c.data_admissao::date))::int, 0) AS regular_count,
       (c.data_admissao::date + interval '5 years' + interval '1 day')::date AS first_premium_credit,
       CASE
         WHEN (c.data_admissao::date + interval '5 years' + interval '1 day')::date > cfg.ref_date THEN 0
         ELSE 1 + FLOOR((
              EXTRACT(YEAR FROM age(cfg.ref_date, (c.data_admissao::date + interval '5 years' + interval '1 day')::date)) * 12
            + EXTRACT(MONTH FROM age(cfg.ref_date, (c.data_admissao::date + interval '5 years' + interval '1 day')::date))
         ) / 30.0)::int
       END AS premium_count
  FROM colaboradores c
 CROSS JOIN v54_cfg cfg
 WHERE upper(coalesce(c.status, 'ATIVO')) IN ('ATIVO', 'ACTIVE')
   AND c.matricula IS NOT NULL
   AND c.data_admissao IS NOT NULL;

-- Consolida o saldo regular que ja estava visivel no banco.
-- Isso evita perder ajustes historicos da migracao. O total e movido para o ultimo P adquirido.
CREATE TEMP TABLE v54_regular_agg ON COMMIT DROP AS
SELECT a.matricula,
       count(sp.id) AS row_count,
       coalesce(sum(sp.saldo_inicial), 0)::numeric(10,2) AS saldo_inicial,
       coalesce(sum(sp.saldo_utilizado), 0)::numeric(10,2) AS saldo_utilizado,
       coalesce(sum(sp.saldo_reservado), 0)::numeric(10,2) AS saldo_reservado
  FROM v54_active a
  LEFT JOIN saldo_periodo sp
    ON upper(sp.colaborador_matricula) = a.matricula
   AND upper(sp.tipo_saldo) = 'REGULAR'
 GROUP BY a.matricula;

-- O saldo Premium será recalculado pela regra nova. Ajustes antigos ligados
-- à estrutura anual não são transportados automaticamente; somente solicitações
-- não-ajuste do ciclo Premium vigente compõem utilizado/reservado.

-- Periodos regulares concluidos. O P em formacao nao aparece.
CREATE TEMP TABLE v54_regular_cycles ON COMMIT DROP AS
SELECT a.colaborador_id,
       a.matricula,
       gs AS periodo_numero,
       (a.data_admissao + make_interval(years => gs - 1))::date AS data_inicio,
       ((a.data_admissao + make_interval(years => gs))::date - 1) AS data_fim,
       (gs = a.regular_count) AS is_atual
  FROM v54_active a
 CROSS JOIN LATERAL generate_series(1, a.regular_count) gs;

-- Ciclos Premium independentes: P1=30 dias apos 5 anos; seguintes=15 dias a cada 30 meses.
CREATE TEMP TABLE v54_premium_cycles ON COMMIT DROP AS
SELECT a.colaborador_id,
       a.matricula,
       gs AS periodo_numero,
       CASE WHEN gs = 1
            THEN a.data_admissao
            ELSE (a.first_premium_credit + make_interval(months => (gs - 2) * 30))::date
       END AS data_inicio,
       ((a.first_premium_credit + make_interval(months => (gs - 1) * 30))::date - 1) AS data_fim,
       (a.first_premium_credit + make_interval(months => (gs - 1) * 30))::date AS credito_em,
       (a.first_premium_credit + make_interval(months => gs * 30))::date AS proximo_credito_em,
       (gs = a.premium_count) AS is_atual,
       CASE WHEN gs = 1 THEN 30::numeric(10,2) ELSE 15::numeric(10,2) END AS base_credito
  FROM v54_active a
 CROSS JOIN LATERAL generate_series(1, a.premium_count) gs;

-- Movimentos do ciclo Premium vigente. Ajustes aprovados da estrutura antiga
-- são mantidos no histórico, mas não alteram automaticamente a base fixa da
-- regra nova. O ADMIN poderá reaplicar manualmente apenas os ajustes válidos.
CREATE TEMP TABLE v54_premium_usage ON COMMIT DROP AS
SELECT pc.matricula,
       pc.periodo_numero,
       coalesce(sum(CASE
           WHEN upper(translate(coalesce(s.status,''), 'ÁÀÃÂÉÊÍÓÔÕÚÇ', 'AAAAEEIOOOUC')) IN ('APROVADO','APROVADA')
           THEN abs(coalesce(s.dias::numeric, s.dias_solicitados, 0)) ELSE 0 END), 0)::numeric(10,2) AS saldo_utilizado,
       coalesce(sum(CASE
           WHEN upper(translate(coalesce(s.status,''), 'ÁÀÃÂÉÊÍÓÔÕÚÇ', 'AAAAEEIOOOUC')) IN ('PENDENTE','EM ANALISE','ANALISE','RESERVA','RESERVADO','RESERVADA')
           THEN abs(coalesce(s.dias::numeric, s.dias_solicitados, 0)) ELSE 0 END), 0)::numeric(10,2) AS saldo_reservado
  FROM v54_premium_cycles pc
  LEFT JOIN solicitacoes_ferias s
    ON upper(s.colaborador_matricula) = pc.matricula
   AND upper(coalesce(s.saldo_tipo, s.tipo_ferias, 'REGULAR')) = 'PREMIUM'
   AND coalesce(s.is_ajuste, false) = false
   AND upper(coalesce(s.tipo_solicitacao, '')) <> 'AJUSTE'
   AND upper(coalesce(s.solicitacao, '')) NOT LIKE '%AJUSTE%'
   AND s.data_inicio >= pc.credito_em
   AND s.data_inicio < pc.proximo_credito_em
 WHERE pc.is_atual
 GROUP BY pc.matricula, pc.periodo_numero;

-- Marca os ajustes Premium legados da estrutura anual como somente histórico.
-- Eles não compõem o saldo Premium V54. A tela ADMIN os exibirá para revisão e,
-- ao editar/reaplicar um ajuste válido, o app removerá esta marca automaticamente.
UPDATE solicitacoes_ferias s
   SET metadata = coalesce(s.metadata, '{}'::jsonb) || jsonb_build_object(
         'v54_premium_adjustment_ignored', true,
         'v54_reason', 'Ajuste Premium legado da estrutura anual; preservado no histórico e não reaplicado automaticamente ao saldo V54.',
         'v54_marked_at', current_timestamp
       ),
       updated_at = current_timestamp
  FROM v54_active a
 WHERE upper(trim(s.colaborador_matricula)) = a.matricula
   AND upper(coalesce(s.saldo_tipo, s.tipo_ferias, 'REGULAR')) = 'PREMIUM'
   AND (
        coalesce(s.is_ajuste, false) = true
        OR upper(coalesce(s.tipo_solicitacao, '')) = 'AJUSTE'
        OR upper(coalesce(s.solicitacao, '')) LIKE '%AJUSTE%'
   );

-- Resultado esperado para saldo_periodo.
CREATE TEMP TABLE v54_expected_saldos ON COMMIT DROP AS
SELECT rc.colaborador_id,
       rc.matricula AS colaborador_matricula,
       rc.periodo_numero,
       rc.data_inicio,
       rc.data_fim,
       rc.is_atual,
       'REGULAR'::varchar(20) AS tipo_saldo,
       CASE
         WHEN NOT rc.is_atual THEN 0::numeric(10,2)
         WHEN ra.row_count = 0 THEN (30 * rc.periodo_numero)::numeric(10,2)
         ELSE ra.saldo_inicial
       END AS saldo_inicial,
       CASE WHEN rc.is_atual THEN ra.saldo_utilizado ELSE 0::numeric(10,2) END AS saldo_utilizado,
       CASE WHEN rc.is_atual THEN ra.saldo_reservado ELSE 0::numeric(10,2) END AS saldo_reservado,
       CASE
         WHEN NOT rc.is_atual THEN 0::numeric(10,2)
         WHEN ra.row_count = 0 THEN (30 * rc.periodo_numero)::numeric(10,2)
         ELSE ra.saldo_inicial - ra.saldo_utilizado - ra.saldo_reservado
       END AS saldo_disponivel
  FROM v54_regular_cycles rc
  JOIN v54_regular_agg ra ON ra.matricula = rc.matricula

UNION ALL

SELECT pc.colaborador_id,
       pc.matricula,
       pc.periodo_numero,
       pc.data_inicio,
       pc.data_fim,
       pc.is_atual,
       'PREMIUM'::varchar(20),
       CASE WHEN pc.is_atual THEN pc.base_credito ELSE 0::numeric(10,2) END AS saldo_inicial,
       CASE WHEN pc.is_atual THEN coalesce(pu.saldo_utilizado,0) ELSE 0::numeric(10,2) END AS saldo_utilizado,
       CASE WHEN pc.is_atual THEN coalesce(pu.saldo_reservado,0) ELSE 0::numeric(10,2) END AS saldo_reservado,
       CASE WHEN pc.is_atual
            THEN pc.base_credito - coalesce(pu.saldo_utilizado,0) - coalesce(pu.saldo_reservado,0)
            ELSE 0::numeric(10,2) END AS saldo_disponivel
  FROM v54_premium_cycles pc
  LEFT JOIN v54_premium_usage pu
    ON pu.matricula = pc.matricula
   AND pu.periodo_numero = pc.periodo_numero;

-- Atualiza ou cria os saldos esperados, preservando IDs quando a chave ja existe.
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
  FROM v54_expected_saldos
ON CONFLICT (colaborador_matricula, periodo_numero, tipo_saldo)
DO UPDATE SET
    colaborador_id = EXCLUDED.colaborador_id,
    data_inicio = EXCLUDED.data_inicio,
    data_fim = EXCLUDED.data_fim,
    is_atual = EXCLUDED.is_atual,
    saldo_inicial = EXCLUDED.saldo_inicial,
    saldo_utilizado = EXCLUDED.saldo_utilizado,
    saldo_reservado = EXCLUDED.saldo_reservado,
    saldo_disponivel = EXCLUDED.saldo_disponivel,
    ultima_alteracao = current_timestamp,
    updated_at = current_timestamp;

-- Exclui somente linhas dos ativos que nao pertencem mais a um ciclo adquirido.
DELETE FROM saldo_periodo sp
 USING v54_active a
 WHERE upper(sp.colaborador_matricula) = a.matricula
   AND NOT EXISTS (
       SELECT 1
         FROM v54_expected_saldos e
        WHERE e.colaborador_matricula = a.matricula
          AND e.periodo_numero = sp.periodo_numero
          AND e.tipo_saldo = upper(sp.tipo_saldo)
   );

-- periodos_aquisitivos guarda apenas os ciclos REGULARES concluidos.
INSERT INTO periodos_aquisitivos (
    colaborador_id, colaborador_matricula, periodo_numero,
    data_inicio, data_fim, is_atual
)
SELECT colaborador_id, matricula, periodo_numero, data_inicio, data_fim, is_atual
  FROM v54_regular_cycles
ON CONFLICT (colaborador_matricula, periodo_numero)
DO UPDATE SET
    colaborador_id = EXCLUDED.colaborador_id,
    data_inicio = EXCLUDED.data_inicio,
    data_fim = EXCLUDED.data_fim,
    is_atual = EXCLUDED.is_atual;

DELETE FROM periodos_aquisitivos pa
 USING v54_active a
 WHERE upper(pa.colaborador_matricula) = a.matricula
   AND NOT EXISTS (
       SELECT 1
         FROM v54_regular_cycles rc
        WHERE rc.matricula = a.matricula
          AND rc.periodo_numero = pa.periodo_numero
   );

-- Remapeia a origem das solicitacoes regulares para o P valido na data do evento.
WITH mapped AS (
    SELECT s.id,
           rc.periodo_numero,
           abs(coalesce(s.dias::numeric, s.dias_solicitados, 0)) AS qtd
      FROM solicitacoes_ferias s
      JOIN v54_active a ON upper(s.colaborador_matricula) = a.matricula
      JOIN v54_regular_cycles rc ON rc.matricula = a.matricula
     WHERE upper(coalesce(s.saldo_tipo, s.tipo_ferias, 'REGULAR')) = 'REGULAR'
       AND s.data_inicio >= (a.data_admissao + make_interval(years => rc.periodo_numero))::date
       AND s.data_inicio <  (a.data_admissao + make_interval(years => rc.periodo_numero + 1))::date
)
UPDATE solicitacoes_ferias s
   SET periodo_aquisitivo_origem = 'P' || m.periodo_numero || ':' ||
       trim(trailing '.' from trim(trailing '0' from to_char(m.qtd, 'FM999999990.00'))),
       updated_at = current_timestamp
  FROM mapped m
 WHERE s.id = m.id;

-- Remapeia a origem Premium para P1/P2/... independente da numeracao regular.
WITH premium_windows AS (
    SELECT pc.matricula,
           pc.periodo_numero,
           (a.first_premium_credit + make_interval(months => (pc.periodo_numero - 1) * 30))::date AS valid_from,
           (a.first_premium_credit + make_interval(months => pc.periodo_numero * 30))::date AS valid_until
      FROM v54_premium_cycles pc
      JOIN v54_active a ON a.matricula = pc.matricula
), mapped AS (
    SELECT s.id,
           pw.periodo_numero,
           abs(coalesce(s.dias::numeric, s.dias_solicitados, 0)) AS qtd
      FROM solicitacoes_ferias s
      JOIN v54_active a ON upper(s.colaborador_matricula) = a.matricula
      JOIN premium_windows pw ON pw.matricula = a.matricula
     WHERE upper(coalesce(s.saldo_tipo, s.tipo_ferias, 'REGULAR')) = 'PREMIUM'
       AND s.data_inicio >= pw.valid_from
       AND s.data_inicio <  pw.valid_until
)
UPDATE solicitacoes_ferias s
   SET periodo_aquisitivo_origem = 'P' || m.periodo_numero || ':' ||
       trim(trailing '.' from trim(trailing '0' from to_char(m.qtd, 'FM999999990.00'))),
       updated_at = current_timestamp
  FROM mapped m
 WHERE s.id = m.id;

-- Registro de auditoria da correcao.
INSERT INTO auditoria (
    actor_email, action, entity_type, entity_id,
    before_data, after_data, context, created_at
)
SELECT 'pgadmin-v54',
       'NORMALIZAR_CICLOS_SALDOS_V54',
       'saldo_periodo',
       0,
       NULL,
       jsonb_build_object(
           'reference_date', cfg.ref_date,
           'active_collaborators', (SELECT count(*) FROM v54_active),
           'expected_balance_rows', (SELECT count(*) FROM v54_expected_saldos),
           'regular_rule', 'somente periodos anuais concluidos; total anterior consolidado no ultimo P',
           'premium_rule', 'P1 30 dias apos 5 anos; P2+ 15 dias a cada 30 meses; saldo anterior e ajustes anuais antigos expiram'
       ),
       jsonb_build_object('script', 'correcao_periodos_saldos_v54_pgadmin.sql'),
       current_timestamp
  FROM v54_cfg cfg;

-- Conferencias antes do COMMIT.
SELECT 'ativos processados' AS item, count(*)::text AS valor FROM v54_active
UNION ALL
SELECT 'linhas saldo esperadas', count(*)::text FROM v54_expected_saldos
UNION ALL
SELECT 'periodos regulares esperados', count(*)::text FROM v54_regular_cycles
UNION ALL
SELECT 'periodos premium esperados', count(*)::text FROM v54_premium_cycles;

-- Exemplo esperado para MAT00116 na data de referencia:
SELECT colaborador_matricula, periodo_numero, data_inicio, data_fim, is_atual,
       tipo_saldo, saldo_inicial, saldo_utilizado, saldo_reservado, saldo_disponivel
  FROM saldo_periodo
 WHERE upper(colaborador_matricula) = 'MAT00116'
 ORDER BY tipo_saldo, periodo_numero;

COMMIT;

-- ROLLBACK MANUAL, se necessario depois da execucao:
-- BEGIN;
-- SET LOCAL search_path TO app_ferias, public;
-- DELETE FROM saldos_periodo;
-- DELETE FROM periodos_aquisitivos;
-- INSERT INTO periodos_aquisitivos SELECT * FROM backup_v54_20260728_periodos_aquisitivos;
-- INSERT INTO saldos_periodo SELECT * FROM backup_v54_20260728_saldos_periodo;
-- DELETE FROM saldo_periodo;
-- INSERT INTO saldo_periodo SELECT * FROM backup_v54_20260728_saldo_periodo;
-- UPDATE solicitacoes_ferias s
--    SET periodo_aquisitivo_origem = b.periodo_aquisitivo_origem,
--        metadata = b.metadata
--   FROM backup_v54_20260728_solicitacoes_origem b
--  WHERE s.id = b.id;
-- SELECT setval(pg_get_serial_sequence('saldo_periodo','id'), coalesce((SELECT max(id) FROM saldo_periodo),1), true);
-- SELECT setval(pg_get_serial_sequence('periodos_aquisitivos','id'), coalesce((SELECT max(id) FROM periodos_aquisitivos),1), true);
-- SELECT setval(pg_get_serial_sequence('saldos_periodo','id'), coalesce((SELECT max(id) FROM saldos_periodo),1), true);
-- COMMIT;
