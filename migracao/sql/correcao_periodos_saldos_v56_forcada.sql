-- ============================================================================
-- V56 - CORRECAO FORCADA DE PERIODOS E SALDOS
-- ============================================================================
-- OBJETIVO
--   1. Detectar automaticamente o schema que contem as tabelas do app.
--   2. Recriar, para colaboradores ATIVOS, saldo_periodo e periodos_aquisitivos.
--   3. REGULAR:
--        - manter P1 ate o ultimo ciclo anual efetivamente adquirido;
--        - zerar todo o historico;
--        - concentrar no ultimo P adquirido a soma atual de todos os saldos
--          REGULARES existentes (inicial, utilizado e reservado).
--   4. PREMIUM:
--        - P1: 30 dias, creditado no dia seguinte ao fechamento de 5 anos;
--        - P2+: 15 dias a cada 30 meses;
--        - excluir todos os P PREMIUM anuais/indevidos;
--        - manter somente os ciclos PREMIUM realmente adquiridos;
--        - zerar ciclos PREMIUM historicos;
--        - recalcular utilizado/reservado do ciclo atual apenas por solicitacoes
--          nao-ajuste dentro da janela vigente.
--   5. Falhar de forma explicita se estiver no banco/schema errado ou se a
--      validacao final encontrar qualquer linha indevida.
--
-- IMPORTANTE
--   - Publique primeiro o app V56. Builds anteriores podem recriar os periodos
--     anuais PREMIUM ao sincronizar/recalcular.
--   - Execute o arquivo inteiro no Query Tool do pgAdmin, sem selecionar apenas
--     uma parte.
--   - O script cria backups com prefixo z_backup_ antes das alteracoes.
-- ============================================================================

BEGIN;

SET LOCAL lock_timeout = '15s';
SET LOCAL statement_timeout = '300s';
SET LOCAL idle_in_transaction_session_timeout = '300s';

-- Evita duas correcoes simultaneas.
SELECT pg_advisory_xact_lock(5600728);

-- Detecta schemas que possuem simultaneamente as tres tabelas essenciais.
CREATE TEMP TABLE v56_schema_detectado (
    schema_name text PRIMARY KEY
) ON COMMIT DROP;

INSERT INTO v56_schema_detectado (schema_name)
SELECT n.nspname
  FROM pg_namespace n
 WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
   AND to_regclass(format('%I.%I', n.nspname, 'colaboradores')) IS NOT NULL
   AND to_regclass(format('%I.%I', n.nspname, 'saldo_periodo')) IS NOT NULL
   AND to_regclass(format('%I.%I', n.nspname, 'periodos_aquisitivos')) IS NOT NULL;

DO $$
DECLARE
    quantidade integer;
    schemas_encontrados text;
BEGIN
    SELECT count(*), string_agg(schema_name, ', ' ORDER BY schema_name)
      INTO quantidade, schemas_encontrados
      FROM v56_schema_detectado;

    IF quantidade = 0 THEN
        RAISE EXCEPTION
            'V56: nenhuma estrutura do app foi encontrada no banco %. Confirme a conexao do pgAdmin.',
            current_database();
    ELSIF quantidade > 1 THEN
        RAISE EXCEPTION
            'V56: foram encontrados varios schemas candidatos no banco %: %. Execute no banco correto ou remova a ambiguidade.',
            current_database(), schemas_encontrados;
    END IF;
END $$;

-- Define o schema detectado para todas as instrucoes seguintes.
SELECT set_config(
    'search_path',
    quote_ident(schema_name) || ',public',
    true
)
FROM v56_schema_detectado;

-- Identificacao visivel no painel de resultados.
SELECT current_database() AS banco_em_execucao,
       current_schema() AS schema_em_execucao,
       current_user AS usuario_em_execucao,
       CURRENT_DATE AS data_referencia;

-- Interrompe caso alguma coluna essencial nao exista.
DO $$
DECLARE
    coluna text;
BEGIN
    FOREACH coluna IN ARRAY ARRAY[
        'colaborador_matricula', 'periodo_numero', 'tipo_saldo',
        'saldo_inicial', 'saldo_utilizado', 'saldo_reservado',
        'saldo_disponivel', 'is_atual'
    ]
    LOOP
        IF NOT EXISTS (
            SELECT 1
              FROM information_schema.columns
             WHERE table_schema = current_schema()
               AND table_name = 'saldo_periodo'
               AND column_name = coluna
        ) THEN
            RAISE EXCEPTION 'V56: coluna ausente em %.saldo_periodo: %', current_schema(), coluna;
        END IF;
    END LOOP;
END $$;

-- Renomeia backups antigos para o final da listagem alfabetica.
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
         ORDER BY tablename
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

-- Backups da primeira execucao da V56.
CREATE TABLE IF NOT EXISTS z_backup_v56_20260728_saldo_periodo
AS TABLE saldo_periodo;

CREATE TABLE IF NOT EXISTS z_backup_v56_20260728_periodos_aquisitivos
AS TABLE periodos_aquisitivos;

CREATE TABLE IF NOT EXISTS z_backup_v56_20260728_solicitacoes_ferias
AS TABLE solicitacoes_ferias;

CREATE TEMP TABLE v56_cfg (
    ref_date date NOT NULL
) ON COMMIT DROP;

INSERT INTO v56_cfg VALUES (CURRENT_DATE);

-- Colaboradores ativos aptos a receber ciclos.
CREATE TEMP TABLE v56_ativos ON COMMIT DROP AS
SELECT c.id AS colaborador_id,
       upper(trim(c.matricula)) AS matricula,
       c.nome_completo,
       c.data_admissao::date AS data_admissao,
       cfg.ref_date
  FROM colaboradores c
 CROSS JOIN v56_cfg cfg
 WHERE upper(trim(coalesce(c.status, ''))) IN ('ATIVO', 'ACTIVE')
   AND nullif(trim(c.matricula), '') IS NOT NULL
   AND c.data_admissao IS NOT NULL;

DO $$
DECLARE
    quantidade integer;
BEGIN
    SELECT count(*) INTO quantidade FROM v56_ativos;
    IF quantidade = 0 THEN
        RAISE EXCEPTION
            'V56: nenhum colaborador ATIVO com matricula e data de admissao foi encontrado em %.colaboradores. Nada foi alterado.',
            current_schema();
    END IF;
END $$;

-- Fotografia anterior, usada para concentrar o saldo REGULAR real no P vigente.
CREATE TEMP TABLE v56_regular_agregado ON COMMIT DROP AS
SELECT a.colaborador_id,
       a.matricula,
       count(sp.id) AS quantidade_linhas,
       coalesce(sum(sp.saldo_inicial), 0)::numeric(12,2) AS saldo_inicial,
       coalesce(sum(sp.saldo_utilizado), 0)::numeric(12,2) AS saldo_utilizado,
       coalesce(sum(sp.saldo_reservado), 0)::numeric(12,2) AS saldo_reservado
  FROM v56_ativos a
  LEFT JOIN saldo_periodo sp
    ON upper(trim(sp.colaborador_matricula)) = a.matricula
   AND upper(trim(coalesce(sp.tipo_saldo, ''))) = 'REGULAR'
 GROUP BY a.colaborador_id, a.matricula;

-- Ciclos REGULARES adquiridos. O periodo em formacao nao e criado.
CREATE TEMP TABLE v56_regular_cycles ON COMMIT DROP AS
WITH ciclos AS (
    SELECT a.colaborador_id,
           a.matricula,
           gs AS periodo_numero,
           (a.data_admissao + make_interval(years => gs - 1))::date AS data_inicio,
           ((a.data_admissao + make_interval(years => gs))::date - 1) AS data_fim,
           (a.data_admissao + make_interval(years => gs))::date AS credito_em
      FROM v56_ativos a
     CROSS JOIN LATERAL generate_series(1, 100) gs
     WHERE (a.data_admissao + make_interval(years => gs))::date <= a.ref_date
)
SELECT c.*,
       c.periodo_numero = max(c.periodo_numero) OVER (
           PARTITION BY c.matricula
       ) AS is_atual
  FROM ciclos c;

-- Ciclos PREMIUM independentes da numeracao REGULAR.
-- P1: fechamento de 5 anos + 1 dia = 30 dias.
-- P2+: a cada 30 meses = 15 dias.
CREATE TEMP TABLE v56_premium_cycles ON COMMIT DROP AS
WITH base AS (
    SELECT a.*,
           (a.data_admissao + interval '5 years' + interval '1 day')::date AS primeiro_credito
      FROM v56_ativos a
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
           CASE
             WHEN gs = 1 THEN 30::numeric(12,2)
             ELSE 15::numeric(12,2)
           END AS base_credito
      FROM base b
     CROSS JOIN LATERAL generate_series(1, 50) gs
     WHERE (b.primeiro_credito + make_interval(months => (gs - 1) * 30))::date <= b.ref_date
)
SELECT c.*,
       c.periodo_numero = max(c.periodo_numero) OVER (
           PARTITION BY c.matricula
       ) AS is_atual
  FROM ciclos c;

-- Movimentos PREMIUM validos do ciclo vigente.
-- Ajustes legados nao sao reaplicados automaticamente.
CREATE TEMP TABLE v56_premium_movimentos ON COMMIT DROP AS
SELECT pc.matricula,
       pc.periodo_numero,
       coalesce(sum(
           CASE
             WHEN upper(translate(coalesce(s.status, ''),
                                  'ÁÀÃÂÉÊÍÓÔÕÚÇáàãâéêíóôõúç',
                                  'AAAAEEIOOOUCaaaaeeiooouc'))
                  IN ('APROVADO', 'APROVADA')
             THEN abs(coalesce(s.dias::numeric, s.dias_solicitados, 0))
             ELSE 0
           END
       ), 0)::numeric(12,2) AS saldo_utilizado,
       coalesce(sum(
           CASE
             WHEN upper(translate(coalesce(s.status, ''),
                                  'ÁÀÃÂÉÊÍÓÔÕÚÇáàãâéêíóôõúç',
                                  'AAAAEEIOOOUCaaaaeeiooouc'))
                  IN ('PENDENTE', 'EM ANALISE', 'ANALISE', 'RESERVA', 'RESERVADO', 'RESERVADA')
             THEN abs(coalesce(s.dias::numeric, s.dias_solicitados, 0))
             ELSE 0
           END
       ), 0)::numeric(12,2) AS saldo_reservado
  FROM v56_premium_cycles pc
  LEFT JOIN solicitacoes_ferias s
    ON upper(trim(s.colaborador_matricula)) = pc.matricula
   AND upper(trim(coalesce(s.saldo_tipo, s.tipo_ferias, 'REGULAR'))) = 'PREMIUM'
   AND coalesce(s.is_ajuste, false) = false
   AND upper(trim(coalesce(s.tipo_solicitacao, ''))) <> 'AJUSTE'
   AND upper(trim(coalesce(s.solicitacao, ''))) NOT LIKE '%AJUSTE%'
   AND s.data_inicio >= pc.credito_em
   AND s.data_inicio < pc.proximo_credito_em
 WHERE pc.is_atual
 GROUP BY pc.matricula, pc.periodo_numero;

-- Estrutura final esperada.
CREATE TEMP TABLE v56_saldos_esperados ON COMMIT DROP AS
SELECT rc.colaborador_id,
       rc.matricula AS colaborador_matricula,
       rc.periodo_numero,
       rc.data_inicio,
       rc.data_fim,
       rc.is_atual,
       'REGULAR'::varchar(20) AS tipo_saldo,
       CASE
         WHEN rc.is_atual THEN
           CASE
             WHEN ra.quantidade_linhas = 0
               THEN (30 * rc.periodo_numero)::numeric(12,2)
             ELSE ra.saldo_inicial
           END
         ELSE 0::numeric(12,2)
       END AS saldo_inicial,
       CASE WHEN rc.is_atual THEN ra.saldo_utilizado ELSE 0::numeric(12,2) END AS saldo_utilizado,
       CASE WHEN rc.is_atual THEN ra.saldo_reservado ELSE 0::numeric(12,2) END AS saldo_reservado,
       CASE
         WHEN rc.is_atual THEN
           (CASE
              WHEN ra.quantidade_linhas = 0
                THEN (30 * rc.periodo_numero)::numeric(12,2)
              ELSE ra.saldo_inicial
            END) - ra.saldo_utilizado - ra.saldo_reservado
         ELSE 0::numeric(12,2)
       END AS saldo_disponivel
  FROM v56_regular_cycles rc
  JOIN v56_regular_agregado ra
    ON ra.matricula = rc.matricula

UNION ALL

SELECT pc.colaborador_id,
       pc.matricula,
       pc.periodo_numero,
       pc.data_inicio,
       pc.data_fim,
       pc.is_atual,
       'PREMIUM'::varchar(20),
       CASE WHEN pc.is_atual THEN pc.base_credito ELSE 0::numeric(12,2) END,
       CASE WHEN pc.is_atual THEN coalesce(pm.saldo_utilizado, 0) ELSE 0::numeric(12,2) END,
       CASE WHEN pc.is_atual THEN coalesce(pm.saldo_reservado, 0) ELSE 0::numeric(12,2) END,
       CASE
         WHEN pc.is_atual
           THEN pc.base_credito
                - coalesce(pm.saldo_utilizado, 0)
                - coalesce(pm.saldo_reservado, 0)
         ELSE 0::numeric(12,2)
       END
  FROM v56_premium_cycles pc
  LEFT JOIN v56_premium_movimentos pm
    ON pm.matricula = pc.matricula
   AND pm.periodo_numero = pc.periodo_numero;

CREATE TEMP TABLE v56_estatisticas (
    item text PRIMARY KEY,
    quantidade bigint NOT NULL
) ON COMMIT DROP;

INSERT INTO v56_estatisticas VALUES
    ('colaboradores_ativos_processados', (SELECT count(*) FROM v56_ativos)),
    ('linhas_saldo_antes', (
        SELECT count(*)
          FROM saldo_periodo sp
          JOIN v56_ativos a
            ON upper(trim(sp.colaborador_matricula)) = a.matricula
    )),
    ('linhas_saldo_esperadas', (SELECT count(*) FROM v56_saldos_esperados));

-- Remocao forçada: garante que nenhuma linha anual PREMIUM ou saldo historico
-- sobreviva por conflito, caixa, espacos ou IDs antigos.
WITH removidas AS (
    DELETE FROM saldo_periodo sp
     USING v56_ativos a
     WHERE upper(trim(sp.colaborador_matricula)) = a.matricula
     RETURNING sp.id
)
INSERT INTO v56_estatisticas (item, quantidade)
SELECT 'linhas_saldo_excluidas', count(*) FROM removidas;

-- Reinsere somente a estrutura calculada pela regra V56.
INSERT INTO saldo_periodo (
    colaborador_id,
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
    ultima_alteracao,
    created_at,
    updated_at
)
SELECT colaborador_id,
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
       current_timestamp,
       current_timestamp,
       current_timestamp
  FROM v56_saldos_esperados
 ORDER BY colaborador_id, tipo_saldo, periodo_numero;

INSERT INTO v56_estatisticas (item, quantidade)
VALUES ('linhas_saldo_inseridas', (SELECT count(*) FROM v56_saldos_esperados));

-- Recria os periodos aquisitivos REGULARES dos ativos.
WITH removidos AS (
    DELETE FROM periodos_aquisitivos pa
     USING v56_ativos a
     WHERE upper(trim(pa.colaborador_matricula)) = a.matricula
     RETURNING pa.id
)
INSERT INTO v56_estatisticas (item, quantidade)
SELECT 'periodos_regulares_excluidos', count(*) FROM removidos;

INSERT INTO periodos_aquisitivos (
    colaborador_id,
    colaborador_matricula,
    periodo_numero,
    data_inicio,
    data_fim,
    is_atual
)
SELECT colaborador_id,
       matricula,
       periodo_numero,
       data_inicio,
       data_fim,
       is_atual
  FROM v56_regular_cycles
 ORDER BY colaborador_id, periodo_numero;

INSERT INTO v56_estatisticas (item, quantidade)
VALUES ('periodos_regulares_inseridos', (SELECT count(*) FROM v56_regular_cycles));

-- Marca ajustes PREMIUM antigos como historicos, sem afetar o saldo recalculado.
UPDATE solicitacoes_ferias s
   SET metadata = coalesce(s.metadata, '{}'::jsonb)
                  || jsonb_build_object(
                       'v56_premium_adjustment_ignored', true,
                       'v56_reason', 'Ajuste Premium anterior a regra de ciclos 5 anos + 30 meses; mantido somente no historico.',
                       'v56_marked_at', current_timestamp
                     ),
       updated_at = current_timestamp
  FROM v56_ativos a
 WHERE upper(trim(s.colaborador_matricula)) = a.matricula
   AND upper(trim(coalesce(s.saldo_tipo, s.tipo_ferias, 'REGULAR'))) = 'PREMIUM'
   AND (
       coalesce(s.is_ajuste, false) = true
       OR upper(trim(coalesce(s.tipo_solicitacao, ''))) = 'AJUSTE'
       OR upper(trim(coalesce(s.solicitacao, ''))) LIKE '%AJUSTE%'
   );

-- Informa ao app V56 que a normalizacao do dia foi concluida.
DELETE FROM sync_state
 WHERE sync_name IN ('ciclos_saldos_v54', 'ciclos_saldos_v55', 'ciclos_saldos_v56');

INSERT INTO sync_state (
    sync_name,
    last_started_at,
    last_finished_at,
    last_success_at,
    last_status,
    last_error,
    extra,
    updated_at
)
VALUES (
    'ciclos_saldos_v56',
    current_timestamp,
    current_timestamp,
    current_timestamp,
    'success',
    NULL,
    jsonb_build_object(
        'reference_date', CURRENT_DATE,
        'source', 'correcao_periodos_saldos_v56_forcada.sql',
        'schema', current_schema(),
        'regular_rule', 'historico zerado e saldo agregado no ultimo periodo anual adquirido',
        'premium_rule', 'P1 30 dias apos 5 anos; P2+ 15 dias a cada 30 meses; ajustes legados ignorados'
    ),
    current_timestamp
);

-- Auditoria resumida.
INSERT INTO auditoria (
    actor_email,
    action,
    entity_type,
    entity_id,
    before_data,
    after_data,
    context,
    created_at
)
VALUES (
    'pgadmin-v56',
    'CORRECAO_FORCADA_PERIODOS_SALDOS_V56',
    'saldo_periodo',
    0,
    NULL,
    jsonb_build_object(
        'reference_date', CURRENT_DATE,
        'active_collaborators', (SELECT count(*) FROM v56_ativos),
        'rows_inserted', (SELECT count(*) FROM v56_saldos_esperados),
        'database', current_database(),
        'schema', current_schema()
    ),
    jsonb_build_object('script', 'correcao_periodos_saldos_v56_forcada.sql'),
    current_timestamp
);

-- Ajusta as sequences apos exclusao/reinsercao.
SELECT setval(
    pg_get_serial_sequence('saldo_periodo', 'id'),
    greatest(coalesce((SELECT max(id) FROM saldo_periodo), 1), 1),
    true
);

SELECT setval(
    pg_get_serial_sequence('periodos_aquisitivos', 'id'),
    greatest(coalesce((SELECT max(id) FROM periodos_aquisitivos), 1), 1),
    true
);

-- ============================================================================
-- VALIDACOES BLOQUEANTES
-- Se qualquer uma falhar, a transacao inteira e desfeita.
-- ============================================================================
DO $$
DECLARE
    erros_historico bigint;
    erros_multiplos_atuais bigint;
    erros_regular_futuro bigint;
    erros_premium_indevido bigint;
    linhas_atuais bigint;
    linhas_esperadas bigint;
BEGIN
    SELECT count(*)
      INTO erros_historico
      FROM saldo_periodo sp
      JOIN v56_ativos a
        ON upper(trim(sp.colaborador_matricula)) = a.matricula
     WHERE coalesce(sp.is_atual, false) = false
       AND (
           coalesce(sp.saldo_inicial, 0) <> 0
           OR coalesce(sp.saldo_utilizado, 0) <> 0
           OR coalesce(sp.saldo_reservado, 0) <> 0
           OR coalesce(sp.saldo_disponivel, 0) <> 0
       );

    SELECT count(*)
      INTO erros_multiplos_atuais
      FROM (
          SELECT upper(trim(sp.colaborador_matricula)) AS matricula,
                 upper(trim(sp.tipo_saldo)) AS tipo_saldo
            FROM saldo_periodo sp
            JOIN v56_ativos a
              ON upper(trim(sp.colaborador_matricula)) = a.matricula
           WHERE coalesce(sp.is_atual, false) = true
           GROUP BY upper(trim(sp.colaborador_matricula)), upper(trim(sp.tipo_saldo))
          HAVING count(*) > 1
      ) x;

    SELECT count(*)
      INTO erros_regular_futuro
      FROM saldo_periodo sp
      JOIN v56_ativos a
        ON upper(trim(sp.colaborador_matricula)) = a.matricula
     WHERE upper(trim(sp.tipo_saldo)) = 'REGULAR'
       AND NOT EXISTS (
           SELECT 1
             FROM v56_regular_cycles rc
            WHERE rc.matricula = a.matricula
              AND rc.periodo_numero = sp.periodo_numero
       );

    SELECT count(*)
      INTO erros_premium_indevido
      FROM saldo_periodo sp
      JOIN v56_ativos a
        ON upper(trim(sp.colaborador_matricula)) = a.matricula
     WHERE upper(trim(sp.tipo_saldo)) = 'PREMIUM'
       AND NOT EXISTS (
           SELECT 1
             FROM v56_premium_cycles pc
            WHERE pc.matricula = a.matricula
              AND pc.periodo_numero = sp.periodo_numero
       );

    SELECT count(*)
      INTO linhas_atuais
      FROM saldo_periodo sp
      JOIN v56_ativos a
        ON upper(trim(sp.colaborador_matricula)) = a.matricula;

    SELECT count(*) INTO linhas_esperadas FROM v56_saldos_esperados;

    IF erros_historico > 0
       OR erros_multiplos_atuais > 0
       OR erros_regular_futuro > 0
       OR erros_premium_indevido > 0
       OR linhas_atuais <> linhas_esperadas THEN
        RAISE EXCEPTION
            'V56 falhou na validacao: historicos_com_saldo=%, multiplos_atuais=%, regular_futuro=%, premium_indevido=%, linhas_atuais=%, linhas_esperadas=%. Transacao desfeita.',
            erros_historico,
            erros_multiplos_atuais,
            erros_regular_futuro,
            erros_premium_indevido,
            linhas_atuais,
            linhas_esperadas;
    END IF;
END $$;

-- Resultados que devem aparecer no pgAdmin.
SELECT item, quantidade
  FROM v56_estatisticas
 ORDER BY item;

SELECT 'historicos_com_saldo' AS validacao, count(*) AS quantidade
  FROM saldo_periodo sp
  JOIN v56_ativos a
    ON upper(trim(sp.colaborador_matricula)) = a.matricula
 WHERE coalesce(sp.is_atual, false) = false
   AND (
       coalesce(sp.saldo_inicial, 0) <> 0
       OR coalesce(sp.saldo_utilizado, 0) <> 0
       OR coalesce(sp.saldo_reservado, 0) <> 0
       OR coalesce(sp.saldo_disponivel, 0) <> 0
   )
UNION ALL
SELECT 'premium_indevido', count(*)
  FROM saldo_periodo sp
  JOIN v56_ativos a
    ON upper(trim(sp.colaborador_matricula)) = a.matricula
 WHERE upper(trim(sp.tipo_saldo)) = 'PREMIUM'
   AND NOT EXISTS (
       SELECT 1
         FROM v56_premium_cycles pc
        WHERE pc.matricula = a.matricula
          AND pc.periodo_numero = sp.periodo_numero
   )
UNION ALL
SELECT 'mais_de_um_periodo_atual', count(*)
  FROM (
      SELECT sp.colaborador_matricula, sp.tipo_saldo
        FROM saldo_periodo sp
        JOIN v56_ativos a
          ON upper(trim(sp.colaborador_matricula)) = a.matricula
       WHERE coalesce(sp.is_atual, false) = true
       GROUP BY sp.colaborador_matricula, sp.tipo_saldo
      HAVING count(*) > 1
  ) x;

-- Ativos que não puderam ser processados por falta de data de admissão.
SELECT c.id,
       c.matricula,
       c.nome_completo,
       c.status,
       c.data_admissao,
       'NAO PROCESSADO: DATA DE ADMISSAO AUSENTE' AS alerta
  FROM colaboradores c
 WHERE upper(trim(coalesce(c.status, ''))) IN ('ATIVO', 'ACTIVE')
   AND (nullif(trim(c.matricula), '') IS NULL OR c.data_admissao IS NULL)
 ORDER BY c.id;

-- Conferencia do exemplo MAT00116.
SELECT sp.id,
       sp.colaborador_matricula,
       sp.periodo_numero,
       sp.data_inicio,
       sp.data_fim,
       sp.is_atual,
       sp.tipo_saldo,
       sp.saldo_inicial,
       sp.saldo_utilizado,
       sp.saldo_reservado,
       sp.saldo_disponivel
  FROM saldo_periodo sp
 WHERE upper(trim(sp.colaborador_matricula)) = 'MAT00116'
 ORDER BY sp.tipo_saldo, sp.periodo_numero, sp.id;

COMMIT;

-- Resultado esperado para MAT00116 em 28/07/2026:
--   REGULAR: P1..P7; somente P7 com o saldo REGULAR agregado.
--   PREMIUM: somente P1; P1 com saldo_inicial 30, utilizado 15, disponivel 15.
--   Nao deve existir REGULAR P8 nem PREMIUM P2+ nesta data.
