-- Validacao V44
SELECT column_name
FROM information_schema.columns
WHERE table_schema = 'app_ferias'
  AND table_name = 'hierarquia_gestao'
  AND column_name IN ('gestor_superior_tipo', 'gestor_superior_email_custom');
-- Esperado: zero linhas.

SELECT column_name
FROM information_schema.columns
WHERE table_schema = 'app_ferias'
  AND table_name = 'hierarquia_gestao'
  AND column_name IN (
      'gestor_direto_id', 'gestor_direto_matricula', 'gestor_direto_email',
      'gestor_superior_id', 'gestor_superior_matricula', 'gestor_superior_email'
  )
ORDER BY column_name;

SELECT colaborador_matricula, gestor_direto_matricula, gestor_superior_matricula, gestor_superior_email
FROM app_ferias.hierarquia_gestao
WHERE gestor_superior_matricula IN ('DP', 'GESTOR')
   OR gestor_superior_matricula LIKE 'MAT%'
ORDER BY colaborador_matricula
LIMIT 50;
