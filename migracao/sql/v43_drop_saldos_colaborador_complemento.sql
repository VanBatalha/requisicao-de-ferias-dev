-- V43 - Remove colunas obsoletas de saldo de colaborador_complemento.
-- Fonte oficial dos saldos: app_ferias.saldo_periodo.
-- Execute no banco oficial após subir o app V43 ou superior.

BEGIN;

SET LOCAL lock_timeout = '10s';
SET LOCAL statement_timeout = '120s';

-- Backup defensivo dos campos que serão removidos, se ainda existirem.
-- A tabela de backup não participa do app; serve apenas para consulta histórica.
DO $$
DECLARE
    cols text;
BEGIN
    SELECT string_agg(quote_ident(column_name), ', ' ORDER BY ordinal_position)
      INTO cols
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
       );

    IF cols IS NOT NULL THEN
        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS app_ferias.backup_colaborador_complemento_saldos_v43 AS
             SELECT id, colaborador_id, colaborador_matricula, %s, now()::timestamp AS backup_created_at
               FROM app_ferias.colaborador_complemento',
            cols
        );
    END IF;
END $$;

ALTER TABLE app_ferias.colaborador_complemento
    DROP COLUMN IF EXISTS saldo_regular_direito,
    DROP COLUMN IF EXISTS saldo_regular_usado,
    DROP COLUMN IF EXISTS saldo_regular_reservado,
    DROP COLUMN IF EXISTS saldo_regular_disponivel,
    DROP COLUMN IF EXISTS saldo_premium_direito,
    DROP COLUMN IF EXISTS saldo_premium_usado,
    DROP COLUMN IF EXISTS saldo_premium_reservado,
    DROP COLUMN IF EXISTS saldo_premium_disponivel,
    DROP COLUMN IF EXISTS total_solicitacoes,
    DROP COLUMN IF EXISTS periodo_aquisitivo_atual;

COMMIT;

-- Deve retornar zero linhas.
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
