-- Migração V30 - Relação de gestores por matrícula
-- Execute no pgAdmin conectado ao banco correto. É idempotente.

ALTER TABLE app_ferias.colaborador_complemento
    ADD COLUMN IF NOT EXISTS gestor_direto VARCHAR(50),
    ADD COLUMN IF NOT EXISTS gestor_superior VARCHAR(50);

CREATE INDEX IF NOT EXISTS ix_colaborador_complemento_gestor_direto
    ON app_ferias.colaborador_complemento (gestor_direto);

CREATE INDEX IF NOT EXISTS ix_colaborador_complemento_gestor_superior
    ON app_ferias.colaborador_complemento (gestor_superior);

-- Preenche gestor_direto por matrícula usando a tabela hierarquia_gestao quando possível.
UPDATE app_ferias.colaborador_complemento cc
   SET gestor_direto = h.gestor_direto_matricula
  FROM app_ferias.hierarquia_gestao h
 WHERE h.colaborador_matricula = cc.colaborador_matricula
   AND h.gestor_direto_matricula IS NOT NULL
   AND (cc.gestor_direto IS NULL OR btrim(cc.gestor_direto) = '');

-- Preenche gestor_direto por matrícula resolvendo o e-mail legado.
UPDATE app_ferias.colaborador_complemento cc
   SET gestor_direto = g.matricula
  FROM app_ferias.colaboradores g
 WHERE lower(g.email) = lower(cc.gestor_direto_email)
   AND upper(coalesce(g.status, 'ATIVO')) IN ('ATIVO','ACTIVE')
   AND (cc.gestor_direto IS NULL OR btrim(cc.gestor_direto) = '');

-- Preenche gestor_superior por matrícula usando hierarquia_gestao quando possível.
UPDATE app_ferias.colaborador_complemento cc
   SET gestor_superior = h.gestor_superior_matricula
  FROM app_ferias.hierarquia_gestao h
 WHERE h.colaborador_matricula = cc.colaborador_matricula
   AND h.gestor_superior_matricula IS NOT NULL
   AND (cc.gestor_superior IS NULL OR btrim(cc.gestor_superior) = '');

-- Mantém valores especiais como texto operacional.
UPDATE app_ferias.colaborador_complemento
   SET gestor_superior = upper(gestor_superior_email)
 WHERE lower(coalesce(gestor_superior_email, '')) IN ('dp', 'gestor')
   AND (gestor_superior IS NULL OR btrim(gestor_superior) = '');

-- Preenche gestor_superior por matrícula resolvendo o e-mail legado.
UPDATE app_ferias.colaborador_complemento cc
   SET gestor_superior = g.matricula
  FROM app_ferias.colaboradores g
 WHERE lower(g.email) = lower(cc.gestor_superior_email)
   AND upper(coalesce(g.status, 'ATIVO')) IN ('ATIVO','ACTIVE')
   AND (cc.gestor_superior IS NULL OR btrim(cc.gestor_superior) = '');

-- Sincroniza hierarquia_gestao a partir dos novos campos por matrícula.
UPDATE app_ferias.hierarquia_gestao h
   SET gestor_direto_matricula = NULLIF(cc.gestor_direto, ''),
       gestor_direto_id = gd.id,
       gestor_direto_email = gd.email
  FROM app_ferias.colaborador_complemento cc
  LEFT JOIN app_ferias.colaboradores gd
    ON gd.matricula = cc.gestor_direto
 WHERE h.colaborador_matricula = cc.colaborador_matricula
   AND NULLIF(cc.gestor_direto, '') IS NOT NULL;

UPDATE app_ferias.hierarquia_gestao h
   SET gestor_superior_tipo = CASE
           WHEN upper(cc.gestor_superior) = 'DP' THEN 'DP'
           WHEN upper(cc.gestor_superior) = 'GESTOR' THEN 'GESTOR'
           WHEN gs.id IS NOT NULL THEN 'GESTOR'
           ELSE 'CUSTOM'
       END,
       gestor_superior_matricula = CASE WHEN gs.id IS NOT NULL THEN gs.matricula ELSE NULL END,
       gestor_superior_id = gs.id,
       gestor_superior_email_custom = CASE
           WHEN upper(cc.gestor_superior) IN ('DP','GESTOR') THEN NULL
           WHEN gs.id IS NOT NULL THEN NULL
           ELSE cc.gestor_superior
       END
  FROM app_ferias.colaborador_complemento cc
  LEFT JOIN app_ferias.colaboradores gs
    ON gs.matricula = cc.gestor_superior
 WHERE h.colaborador_matricula = cc.colaborador_matricula
   AND NULLIF(cc.gestor_superior, '') IS NOT NULL;
