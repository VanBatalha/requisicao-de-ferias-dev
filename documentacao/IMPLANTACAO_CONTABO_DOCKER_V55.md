# Implantação do app de férias no servidor Contabo com Docker

## Estratégia recomendada

Não é necessário escolher entre GitHub e Docker. O fluxo recomendado usa os dois:

1. O código permanece versionado no GitHub.
2. O repositório é clonado no servidor Contabo.
3. O Docker Compose constrói e executa aplicação, PostgreSQL e proxy HTTPS.
4. Atualizações são feitas com `git pull` e reconstrução do container.

A V55 inclui:

- `Dockerfile`;
- `compose.yaml`;
- `deploy/Caddyfile`;
- `.env.example`;
- scripts de atualização, backup e execução diária.

## Pré-requisitos importantes

- Servidor Ubuntu 22.04 ou 24.04 de 64 bits.
- Domínio apontando para o IP público do Contabo.
- Portas TCP 22, 80 e 443 liberadas.
- Acesso seguro do servidor ao LDAP/Active Directory.

O último item é obrigatório: se o LDAP estiver somente na rede interna da empresa, o Contabo precisará entrar nessa rede por VPN. Não é recomendado publicar LDAP simples na internet. Prefira VPN e, quando possível, LDAPS com certificado validado.

## 1. Preparar o servidor

Conecte por SSH e atualize o sistema:

```bash
sudo apt update
sudo apt upgrade -y
sudo timedatectl set-timezone America/Fortaleza
```

Crie um usuário administrativo, configure chave SSH e mantenha a sessão atual aberta até confirmar o novo acesso.

## 2. Instalar Docker pelo repositório oficial

```bash
sudo apt update
sudo apt install -y ca-certificates curl git
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF

sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo docker run --rm hello-world
```

Opcionalmente, adicione seu usuário ao grupo Docker:

```bash
sudo usermod -aG docker "$USER"
```

Saia e entre novamente para a alteração de grupo valer.

## 3. Clonar a aplicação

```bash
sudo mkdir -p /opt/app-ferias
sudo chown "$USER":"$USER" /opt/app-ferias
git clone URL_DO_SEU_REPOSITORIO.git /opt/app-ferias
cd /opt/app-ferias
```

Se o repositório for privado, use uma chave SSH de implantação ou um token de acesso com o menor privilégio possível.

## 4. Configurar variáveis

```bash
cp .env.example .env
nano .env
```

Antes de restaurar o banco, confirme a versão principal do PostgreSQL de origem:

```sql
SHOW server_version;
```

Ajuste `POSTGRES_IMAGE` no `.env` para a mesma versão principal, por exemplo `postgres:16-alpine`.

Troque obrigatoriamente:

- `APP_DOMAIN`;
- `FLASK_SECRET_KEY`;
- `POSTGRES_PASSWORD`;
- dados LDAP;
- token do Smartsheet, se a sincronização manual continuar sendo usada.

Gere uma chave Flask forte:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(64))"
```

Proteja o arquivo:

```bash
chmod 600 .env
mkdir -p backups exports
```

## 5. Subir o ambiente

```bash
docker compose build --pull
docker compose up -d
docker compose ps
docker compose logs -f app
```

O Caddy solicita e renova o certificado HTTPS automaticamente quando o domínio aponta corretamente para o servidor e as portas 80/443 estão liberadas.

Verifique:

```bash
curl -I "https://SEU_DOMINIO/healthz"
```

## 6. Migrar o PostgreSQL atual

Antes da migração definitiva, impeça novos lançamentos no ambiente antigo.

Na máquina que possui acesso ao banco atual:

```bash
pg_dump \
  --format=custom \
  --no-owner \
  --no-privileges \
  --schema=app_ferias \
  "$DATABASE_URL_ATUAL" > ferias_render.dump
```

Copie o arquivo para `/opt/app-ferias/backups/` no Contabo. Depois restaure:

```bash
cd /opt/app-ferias
docker compose exec -T db pg_restore \
  --clean \
  --if-exists \
  --no-owner \
  --no-privileges \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" < backups/ferias_render.dump
```

Como as variáveis do `.env` não são carregadas automaticamente no shell, execute antes, quando necessário:

```bash
set -a
. ./.env
set +a
```

Depois da restauração, execute a correção V55 no banco e valide a MAT00116 e outros casos conhecidos.

## 7. Agendar a criação diária de períodos

A aplicação já verifica no primeiro acesso do dia. No Contabo, configure também o cron do sistema para garantir a execução mesmo sem acessos.

```bash
sudo crontab -e
```

Adicione:

```cron
10 0 * * * APP_DIR=/opt/app-ferias /opt/app-ferias/deploy/scripts/periodos_diarios.sh >> /var/log/app-ferias-periodos.log 2>&1
30 2 * * * APP_DIR=/opt/app-ferias /opt/app-ferias/deploy/scripts/backup_postgres.sh >> /var/log/app-ferias-backup.log 2>&1
```

Com o servidor em `America/Fortaleza`, a primeira tarefa roda diariamente às 00:10.

Teste manualmente:

```bash
sudo APP_DIR=/opt/app-ferias /opt/app-ferias/deploy/scripts/periodos_diarios.sh
sudo APP_DIR=/opt/app-ferias /opt/app-ferias/deploy/scripts/backup_postgres.sh
```

## 8. Atualizar a aplicação

```bash
cd /opt/app-ferias
./deploy/scripts/update_app.sh
```

Antes de atualizações relevantes, faça backup e valide em ambiente de teste.

## 9. Comandos úteis

```bash
# Status
docker compose ps

# Logs do app
docker compose logs -f --tail=200 app

# Logs do PostgreSQL
docker compose logs -f --tail=200 db

# Reiniciar apenas o app
docker compose restart app

# Entrar no PostgreSQL
docker compose exec db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"

# Exportar banco para XLSX ordenado
docker compose exec -T app python export_database_xlsx.py \
  /app/exports/export_app_ferias_$(date +%Y%m%d_%H%M%S).xlsx
```

## 10. Checklist antes de trocar produção

- Backup e teste de restauração concluídos.
- Login LDAP funcionando pelo Contabo.
- Sincronização manual do ADMIN funcionando.
- Solicitações e relatórios funcionando.
- Correção V55 executada.
- Cron diário testado.
- Backup diário testado.
- DNS e HTTPS funcionando.
- Firewall liberando somente as portas necessárias.
- Ambiente antigo mantido em modo somente leitura durante a validação final.
