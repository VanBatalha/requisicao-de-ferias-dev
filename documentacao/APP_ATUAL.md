# Gestão de Férias — funcionamento atual (V67 corrigida)

## Fonte de dados e identidade
- PostgreSQL é a fonte operacional do app.
- Matrícula é a chave de negócio dos colaboradores. E-mail é usado para login/contato e compatibilidade.
- `saldo_periodo` é a fonte oficial de saldos.
- `solicitacoes_ferias` guarda solicitações e ajustes.
- `admin_configs` guarda exceções administrativas, incluindo acumulação PREMIUM.
- Smartsheet permanece apenas na sincronização manual do cadastro pelo Painel ADMIN.

## Perfis
- ADMIN: acesso integral, manutenção de cadastro, saldos, solicitações, ajustes e regras de exceção.
- DP: acesso integral às telas operacionais do DP e às solicitações de todos os colaboradores, sem poder editar histórico pelo painel DP.
- Gestor: acesso à própria equipe conforme `hierarquia_gestao`.

## Férias REGULARES
- Um ciclo só é criado após completar 12 meses desde a admissão.
- Ciclo ainda em formação não existe em `saldo_periodo`.
- Saldos podem permanecer em mais de um período adquirido.
- Consumo é FIFO: período mais antigo com saldo primeiro.
- `is_atual` indica o ciclo mais recente, não significa que períodos anteriores perdem o saldo.

## Licença Certariana / PREMIUM
- P1: 30 dias após 5 anos completos, com crédito no dia seguinte.
- P2 em diante: 15 dias a cada 30 meses.
- Regra padrão: quando nasce novo ciclo PREMIUM, saldo remanescente anterior expira.
- Exceção ADMIN `PREMIUM_ACCUMULATION`: preserva os créditos adquiridos em ciclos anteriores e permite consumo FIFO.
- Temporariamente não existe mínimo de 10 dias, saldo remanescente mínimo de 10 nem regra 3x10. Permanece o máximo de 3 segmentos e a proteção contra sobreposição.

## Ajustes
- Ajuste não é férias utilizadas.
- Ajuste positivo: aumenta `saldo_inicial` e `saldo_disponivel`.
- Ajuste negativo: reduz `saldo_inicial` e `saldo_disponivel`.
- Ajuste nunca altera `saldo_utilizado` ou `saldo_reservado`.
- O tipo de saldo é canônico (`REGULAR` ou `PREMIUM`) e toda movimentação filtra explicitamente matrícula + tipo.

## Solicitações
- PENDENTE/EM ANÁLISE: reserva saldo.
- APROVADA: reserva é convertida em utilizado.
- CANCELADA/REPROVADA: reserva/uso é estornado conforme o mapa de períodos gravado.
- REGULAR pode consumir múltiplos períodos adquiridos.
- PREMIUM padrão consome somente o ciclo vigente; com exceção de acumulação, pode consumir vários ciclos PREMIUM adquiridos.

## Geração de períodos
Arquivo principal: `ferias_app/services/period_accrual_service.py`.
- `regular_cycles`: calcula ciclos anuais concluídos.
- `premium_cycles`: calcula 5 anos + ciclos de 30 meses.
- `ensure_due_periods`: cria/normaliza somente ciclos adquiridos de colaboradores ATIVOS.
- Inativos não recebem novos ciclos; histórico existente é preservado.

## Serviços principais
- `postgres_service.py`: escrita e movimentação de saldo/solicitações.
- `postgres_compat_service.py`: leitura no formato usado pelas telas.
- `solicitacoes_service.py`: validação e criação de solicitações.
- `admin_cadastro_service.py`: manutenção administrativa.
- `premium_policy_service.py`: exceção de acumulação PREMIUM.
- `period_accrual_service.py`: criação diária de ciclos.
- `permissions_service.py`: perfis e permissões.
- `ldap_service.py`: autenticação LDAP.

## Render / produção
- `DB_SCHEMA=app_ferias`.
- `/healthz` informa o build.
- Start recomendado: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --worker-class gthread --timeout 180`.
- Recomenda-se `LDAP_CONNECT_TIMEOUT=8` e `LDAP_RECEIVE_TIMEOUT=10`.

## Observação sobre dados antigos
A correção do código impede novos ajustes de entrarem como `saldo_utilizado`. Registros históricos que já tenham sido movimentados pela regra antiga devem ser conciliados separadamente antes de serem considerados definitivamente corrigidos.


## Correções operacionais V67.1

- O saldo disponível de `saldo_periodo` é a fonte de verdade para validar uma nova solicitação PREMIUM. Solicitações PREMIUM antigas são consideradas para sobreposição e limite de segmentos, mas não são debitadas uma segunda vez durante a validação.
- Ajustes DP positivos ou negativos alteram direito (`saldo_inicial`) e saldo disponível, nunca representam dias utilizados.
- Ajustes negativos deixam de ser ignorados quando uma mudança de status os torna aprovados.
- O tipo REGULAR/PREMIUM é normalizado também nos caminhos administrativos de estorno.
- Mensagens de saldo REGULAR informam quantos dias corridos existem no intervalo solicitado.
