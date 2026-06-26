-- Validação V30 - Relação de gestores por matrícula

SELECT
    COUNT(*) AS total_complementos,
    COUNT(*) FILTER (WHERE NULLIF(gestor_direto, '') IS NOT NULL) AS com_gestor_direto_matricula,
    COUNT(*) FILTER (WHERE NULLIF(gestor_superior, '') IS NOT NULL) AS com_gestor_superior_matricula_ou_especial,
    COUNT(*) FILTER (WHERE NULLIF(gestor_direto_email, '') IS NOT NULL AND NULLIF(gestor_direto, '') IS NULL) AS ainda_so_com_email_direto,
    COUNT(*) FILTER (WHERE NULLIF(gestor_superior_email, '') IS NOT NULL AND NULLIF(gestor_superior, '') IS NULL) AS ainda_so_com_email_superior
FROM app_ferias.colaborador_complemento;

SELECT
    cc.colaborador_matricula,
    c.nome_completo AS colaborador,
    cc.gestor_direto,
    gd.nome_completo AS gestor_direto_nome,
    cc.gestor_superior,
    gs.nome_completo AS gestor_superior_nome,
    cc.gestor_direto_email,
    cc.gestor_superior_email
FROM app_ferias.colaborador_complemento cc
LEFT JOIN app_ferias.colaboradores c ON c.matricula = cc.colaborador_matricula
LEFT JOIN app_ferias.colaboradores gd ON gd.matricula = cc.gestor_direto
LEFT JOIN app_ferias.colaboradores gs ON gs.matricula = cc.gestor_superior
WHERE NULLIF(cc.gestor_direto, '') IS NOT NULL
   OR NULLIF(cc.gestor_superior, '') IS NOT NULL
ORDER BY c.nome_completo
LIMIT 80;

SELECT
    cc.gestor_direto AS gestor_matricula,
    gd.nome_completo AS gestor_nome,
    COUNT(*) AS subordinados
FROM app_ferias.colaborador_complemento cc
LEFT JOIN app_ferias.colaboradores gd ON gd.matricula = cc.gestor_direto
WHERE NULLIF(cc.gestor_direto, '') IS NOT NULL
GROUP BY cc.gestor_direto, gd.nome_completo
ORDER BY subordinados DESC, gestor_nome;
