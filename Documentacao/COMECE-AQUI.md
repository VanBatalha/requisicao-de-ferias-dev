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
set SMARTSHEET_CLIENT_ID=...
set SMARTSHEET_CLIENT_SECRET=...
set SMARTSHEET_REDIRECT_URI=http://localhost:5000/callback
set ID_FOLHA_CADASTRO=...
set ID_FOLHA_SOLICITACOES=...
```

4) Rode
```bash
python app.py
```

Acesse: `http://localhost:5000`

## Entrypoints

- Execução local: `app.py`
- Produção (Gunicorn/Render): `wsgi.py` (ou `app:app`)
