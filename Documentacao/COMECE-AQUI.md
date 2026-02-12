# Comece aqui (Etapa 3)

## Rodar local

1) Crie e ative um ambiente virtual
```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux/macOS
source .venv/bin/activate
```

2) Instale dependências
```bash
pip install -r requirements.txt
```

3) Configure variáveis de ambiente (exemplo)
```bash
set FLASK_SECRET_KEY=...
set SMARTSHEET_ACCESS_TOKEN=...
set ID_FOLHA_CADASTRO=...
set ID_FOLHA_SOLICITACOES=...

set LDAP_URI=ldap://10.0.0.10:389
set LDAP_BASE_DN=dc=certare,dc=local
set LDAP_BIND_DN=cn=svc-app,ou=Users,dc=certare,dc=local
set LDAP_BIND_PASSWORD=...
set LDAP_USER_FILTER=(sAMAccountName={username})
```

4) Rode
```bash
python app.py
```

Acesse: `http://localhost:5000`

Login: `http://localhost:5000/login`

Mais detalhes do LDAP/Token: `Documentacao/LDAP-E-SMARTSHEET-TOKEN.md`

## Entrypoints

- Execução local: `app.py`
- Produção (Gunicorn/Render): `wsgi.py` (ou `app:app`)
