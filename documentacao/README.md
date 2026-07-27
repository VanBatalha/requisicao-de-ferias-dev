# Gestão de Férias - Documentação

## Documentos principais

- `CONFIGURACAO_AMBIENTES.md` - variáveis de ambiente, banco oficial/teste e Render.
- `SINCRONIZACAO_CADASTRO_SMARTSHEET.md` - origem atual do cadastro, regras de matrícula, permissões e status inválido.
- `HIERARQUIA_MATRICULA.md` - regra de visibilidade por gestor direto, gestor superior, DP e ADMIN.
- `SALDOS_MATRICULA.md` - fonte oficial dos saldos em `saldo_periodo`.
- `ROTAS_PERFORMANCE.md` - rotas principais e pontos de performance.
- `SINCRONIZACAO_MIGRACAO.md` - rotinas manuais e automáticas de sincronização.

## Fonte de cadastro atual

A sincronização cadastral usa a planilha **CADASTRO DE COLABORADORES** (`1745799836133252`). A antiga **CONTROLE_DP** (`3609445264215940`) não é mais fonte para permissões, saldos ou cadastro principal.

## Fonte operacional no PostgreSQL

- colaboradores: `app_ferias.colaboradores`
- permissões: `app_ferias.permissoes_usuario`
- hierarquia: `app_ferias.hierarquia_gestao` e cache em `app_ferias.colaborador_complemento`
- saldo vivo: `app_ferias.saldo_periodo`
- histórico/eventos: `app_ferias.solicitacoes_ferias`

## Arquivos principais do app

- `app.py` e `wsgi.py` - entrada do app.
- `ferias_app/models.py` - modelos SQLAlchemy.
- `ferias_app/services/postgres_service.py` - conexão e estrutura PostgreSQL.
- `ferias_app/services/smartsheet_sync_service.py` - sincronização Smartsheet -> PostgreSQL.
- `ferias_app/services/postgres_compat_service.py` - consultas operacionais por matrícula.
- `ferias_app/blueprints/admin_api.py` - APIs do painel admin.
- `templates/painel_admin.html` - tela administrativa.
- `templates/ferias.html` - tela de solicitação de férias.

- `CORRECAO_RELATORIO_LANCAMENTO_V51.md`: remoção do auto-sync, controle de conexões e diagnóstico do relatório.
- `CORRECAO_RELATORIO_ADMIN_SALDOS_V52.md`: cache de contingência do relatório e manutenção ADMIN de saldos/ajustes.
