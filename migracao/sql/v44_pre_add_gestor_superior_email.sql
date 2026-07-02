-- V44 pre-deploy seguro: cria a coluna que a V44 passa a mapear.
-- Pode ser executado com a V43 ainda em producao.
ALTER TABLE app_ferias.hierarquia_gestao
    ADD COLUMN IF NOT EXISTS gestor_superior_email VARCHAR(255);
