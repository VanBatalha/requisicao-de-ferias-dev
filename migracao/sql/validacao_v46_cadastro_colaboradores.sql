-- Validação V46 - cadastro vindo da planilha CADASTRO DE COLABORADORES
-- Fonte atual: 1745799836133252

-- 1) Conferir origem dos cadastros sincronizados pela fonte atual.
SELECT
    origem_sheet_id,
    COUNT(*) AS total
FROM app_ferias.colaboradores
GROUP BY origem_sheet_id
ORDER BY total DESC;

-- 2) Linhas inválidas não devem entrar como cadastro válido.
SELECT
    id,
    matricula,
    nome_completo,
    status,
    origem_sheet_id,
    origem_row_id
FROM app_ferias.colaboradores
WHERE upper(coalesce(status, '')) IN ('#NO MATCH', 'NO MATCH', '#N/A', 'N/A');

-- 3) Permissões continuam sendo controladas no PostgreSQL.
SELECT
    role,
    COUNT(*) AS total
FROM app_ferias.permissoes_usuario
GROUP BY role
ORDER BY role;

-- 4) Colaboradores sem permissão; esperado: zero ou casos recém-criados antes do sync terminar.
SELECT
    c.matricula,
    c.nome_completo,
    c.status
FROM app_ferias.colaboradores c
LEFT JOIN app_ferias.permissoes_usuario p
    ON p.colaborador_matricula = c.matricula
WHERE p.colaborador_matricula IS NULL
ORDER BY c.nome_completo;

-- 5) Hierarquia operacional deve estar por matrícula ou marcador DP/GESTOR.
SELECT
    colaborador_matricula,
    gestor_direto_matricula,
    gestor_superior_matricula
FROM app_ferias.hierarquia_gestao
WHERE (gestor_direto_matricula IS NOT NULL AND gestor_direto_matricula !~ '^MAT[0-9]+$')
   OR (gestor_superior_matricula IS NOT NULL
       AND gestor_superior_matricula NOT IN ('DP', 'GESTOR')
       AND gestor_superior_matricula !~ '^MAT[0-9]+$')
ORDER BY colaborador_matricula;
