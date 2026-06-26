-- V29 - Estrutura nova de saldo por periodo sem duplicar auditoria_saldos
-- Execute no pgAdmin ou deixe o app executar automaticamente no init_db.

CREATE TABLE IF NOT EXISTS app_ferias.saldo_periodo (
    id SERIAL PRIMARY KEY,
    colaborador_id INTEGER NOT NULL REFERENCES app_ferias.colaboradores(id),
    colaborador_matricula VARCHAR(50) NOT NULL REFERENCES app_ferias.colaboradores(matricula),
    periodo_numero INTEGER NOT NULL,
    data_inicio DATE NOT NULL,
    data_fim DATE NOT NULL,
    is_atual BOOLEAN DEFAULT FALSE,
    tipo_saldo VARCHAR(20) NOT NULL DEFAULT 'REGULAR',
    saldo_inicial NUMERIC(6,2) DEFAULT 0,
    saldo_utilizado NUMERIC(6,2) DEFAULT 0,
    saldo_reservado NUMERIC(6,2) DEFAULT 0,
    saldo_disponivel NUMERIC(6,2) DEFAULT 0,
    ultima_alteracao TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_saldo_periodo_matricula_periodo_tipo UNIQUE (colaborador_matricula, periodo_numero, tipo_saldo)
);

CREATE INDEX IF NOT EXISTS ix_saldo_periodo_matricula_tipo
    ON app_ferias.saldo_periodo (colaborador_matricula, tipo_saldo);

CREATE INDEX IF NOT EXISTS ix_saldo_periodo_colaborador
    ON app_ferias.saldo_periodo (colaborador_id);

ALTER TABLE app_ferias.solicitacoes_ferias
    ADD COLUMN IF NOT EXISTS periodo_aquisitivo_origem TEXT;

CREATE OR REPLACE VIEW app_ferias.vw_saldo_colaborador AS
SELECT
    colaborador_id,
    colaborador_matricula,
    SUM(CASE WHEN tipo_saldo = 'REGULAR' THEN saldo_inicial ELSE 0 END) AS regular_direito,
    SUM(CASE WHEN tipo_saldo = 'REGULAR' THEN saldo_utilizado ELSE 0 END) AS regular_usado,
    SUM(CASE WHEN tipo_saldo = 'REGULAR' THEN saldo_reservado ELSE 0 END) AS regular_reservado,
    SUM(CASE WHEN tipo_saldo = 'REGULAR' THEN saldo_disponivel ELSE 0 END) AS regular_disponivel,
    SUM(CASE WHEN tipo_saldo = 'PREMIUM' THEN saldo_inicial ELSE 0 END) AS premium_direito,
    SUM(CASE WHEN tipo_saldo = 'PREMIUM' THEN saldo_utilizado ELSE 0 END) AS premium_usado,
    SUM(CASE WHEN tipo_saldo = 'PREMIUM' THEN saldo_reservado ELSE 0 END) AS premium_reservado,
    SUM(CASE WHEN tipo_saldo = 'PREMIUM' THEN saldo_disponivel ELSE 0 END) AS premium_disponivel
FROM app_ferias.saldo_periodo
GROUP BY colaborador_id, colaborador_matricula;
