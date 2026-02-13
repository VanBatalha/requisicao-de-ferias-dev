# Guia de Teste

## Smoke test manual (Render ou local)

1. Acesse `/` e valide que a home renderiza
2. Clique em Login → deve ir para Smartsheet
3. Autorize → volta em `/callback` e redireciona pra home
4. Abra tela de Solicitações e faça um POST de teste (via UI)
5. Confirme na folha de Solicitações se foi criada a linha

## Testes automatizados (opcional)

Existe um esqueleto de testes em `tests/`.

Para rodar:
```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest -q
```
