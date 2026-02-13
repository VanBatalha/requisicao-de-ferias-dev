# Login LDAP + Smartsheet via Token (Método A)

Este projeto foi ajustado para:

- **Autenticar usuários via LDAP/Active Directory** (tela `/login`)
- **Acessar o Smartsheet usando 1 token fixo** (`SMARTSHEET_ACCESS_TOKEN`) — sem OAuth / Client ID / Client Secret

## Como funciona

1. O usuário entra com **usuário + senha do LDAP/AD**.
2. O backend valida a senha fazendo **bind** no LDAP.
3. O app guarda na sessão apenas dados mínimos do usuário (`email`, `nome`, `grupos`).
4. Todas as leituras/escritas no Smartsheet são feitas com o **token fixo** configurado no Render.

## Variáveis de ambiente

### Smartsheet

- `SMARTSHEET_ACCESS_TOKEN` (obrigatório)
- `ID_FOLHA_CADASTRO` (obrigatório)
- `ID_FOLHA_SOLICITACOES` (obrigatório)

### LDAP/Active Directory

- `LDAP_URI` (obrigatório) 
  - Ex.: `ldap://10.0.0.10:389` (sem TLS) 
  - Ex.: `ldaps://10.0.0.10:636` (TLS)
- `LDAP_BASE_DN` (obrigatório)
  - Ex.: `dc=certare,dc=local`
- `LDAP_USER_FILTER` (obrigatório)
  - AD (comum): `(sAMAccountName={username})`
  - OpenLDAP (comum): `(uid={username})`
- `LDAP_BIND_DN` (recomendado)
- `LDAP_BIND_PASSWORD` (recomendado)
  - Conta técnica usada para **pesquisar** o DN do usuário.
- `LDAP_EMAIL_ATTR` (padrão: `mail`)
- `LDAP_NAME_ATTR` (padrão: `displayName`)
- `LDAP_MEMBEROF_ATTR` (padrão: `memberOf`)
- `LDAP_VERIFY_CERT` (padrão: `true`) 
  - Use `false` apenas em laboratório/ambiente sem CA.

### Flask

- `FLASK_SECRET_KEY` (obrigatório)

## Dicas de diagnóstico

- Se o login falhar, verifique primeiro:
  - `LDAP_URI` alcançável do Render (porta 389/636 liberada no firewall/VPN)
  - `LDAP_BASE_DN` correto
  - filtro `LDAP_USER_FILTER` correto
  - `LDAP_BIND_DN`/`LDAP_BIND_PASSWORD` com permissão de pesquisa

## Arquivos relevantes no código

- `ferias_app/blueprints/auth.py` (rotas `/login` e `/logout`)
- `ferias_app/services/ldap_service.py` (busca e autenticação LDAP)
- `ferias_app/services/auth_service.py` (`get_access_token()` lê `SMARTSHEET_ACCESS_TOKEN`)
