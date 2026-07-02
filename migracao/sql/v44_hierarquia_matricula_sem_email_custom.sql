-- V44 - Hierarquia por matricula/marcador, sem gestor_superior_tipo/EMAIL_CUSTOM.
-- Rode depois de subir a V44 no Render.

BEGIN;

ALTER TABLE app_ferias.hierarquia_gestao
    ADD COLUMN IF NOT EXISTS gestor_superior_email VARCHAR(255);

-- Remove FKs das colunas de matricula de gestor, pois gestor_superior_matricula
-- pode guardar os marcadores operacionais DP/GESTOR.
DO $$
DECLARE
    r record;
BEGIN
    FOR r IN
        SELECT DISTINCT c.conname
          FROM pg_constraint c
          JOIN unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON true
          JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
         WHERE c.conrelid = 'app_ferias.hierarquia_gestao'::regclass
           AND c.contype = 'f'
           AND a.attname IN ('gestor_direto_matricula', 'gestor_superior_matricula')
    LOOP
        EXECUTE format('ALTER TABLE app_ferias.hierarquia_gestao DROP CONSTRAINT IF EXISTS %I', r.conname);
    END LOOP;
END $$;

-- Converte a coluna antiga de tipo para o novo modelo baseado em gestor_superior_matricula.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'app_ferias'
          AND table_name = 'hierarquia_gestao'
          AND column_name = 'gestor_superior_tipo'
    ) THEN
        UPDATE app_ferias.hierarquia_gestao
           SET gestor_superior_matricula = 'DP'
         WHERE upper(coalesce(gestor_superior_tipo, '')) = 'DP'
           AND nullif(gestor_superior_matricula, '') IS NULL;

        UPDATE app_ferias.hierarquia_gestao
           SET gestor_superior_matricula = 'GESTOR'
         WHERE upper(coalesce(gestor_superior_tipo, '')) IN ('GESTOR', 'EMAIL_CUSTOM', 'CUSTOM')
           AND nullif(gestor_superior_matricula, '') IS NULL;
    END IF;
END $$;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'app_ferias'
          AND table_name = 'hierarquia_gestao'
          AND column_name = 'gestor_superior_email_custom'
    ) THEN
        UPDATE app_ferias.hierarquia_gestao
           SET gestor_superior_email = coalesce(nullif(gestor_superior_email, ''), nullif(gestor_superior_email_custom, ''));
    END IF;
END $$;

ALTER TABLE app_ferias.hierarquia_gestao
    DROP COLUMN IF EXISTS gestor_superior_tipo,
    DROP COLUMN IF EXISTS gestor_superior_email_custom;

CREATE INDEX IF NOT EXISTS idx_hierarquia_gestor_direto_matricula
    ON app_ferias.hierarquia_gestao (gestor_direto_matricula);

CREATE INDEX IF NOT EXISTS idx_hierarquia_gestor_superior_matricula
    ON app_ferias.hierarquia_gestao (gestor_superior_matricula);

COMMIT;
