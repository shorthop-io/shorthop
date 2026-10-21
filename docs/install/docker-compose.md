# Install with Docker Compose

Shorthop and its own Postgres on one machine. Good for a small team and for trying it out;
for two or more replicas, use [Kubernetes](kubernetes.md).

## 1. Get the files

```bash
mkdir shorthop && cd shorthop
curl -fsSLO https://raw.githubusercontent.com/shorthop-io/shorthop/main/deploy/compose/docker-compose.yml
curl -fsSL  https://raw.githubusercontent.com/shorthop-io/shorthop/main/deploy/compose/.env.example -o .env
```

## 2. Fill in `.env`

Three values are required:

```bash
POSTGRES_PASSWORD=$(openssl rand -hex 24)   # the database password
SECRET_KEY=$(openssl rand -hex 32)          # signs sessions; keep it stable
BOOTSTRAP_ADMIN_EMAIL=you@example.com       # the first admin
```

Everything else can wait. Every setting is described in [Configuration](../configuration.md).

## 3. Start it

```bash
docker compose up -d
docker compose ps          # shorthop should become "healthy" within half a minute
```

## 4. Give the first admin a password

```bash
docker compose exec shorthop python -m app.cli admin you@example.com
```

It asks for the password twice (at least 12 characters). Then open
**http://localhost:8080** and sign in.

> Run the same command any time to reset an admin's password, or to make someone an admin.

## Ports

| Port | What | Where it belongs |
|---|---|---|
| **8080** | Web UI, sign-in, alert ingest, API, MCP, `/metrics` | Your private network only |
| **8081** | What phones and calendar apps need — nothing else | The internet, behind your proxy |

Both are bound to `127.0.0.1` by default. To reach the web UI from other machines, set
`SHORTHOP_BIND=0.0.0.0` (on a private network) or put it behind your VPN. To let phones in, see
[Phones → reaching your Shorthop](../phones.md#reaching-your-shorthop).
If 8080 or 8081 is taken, set `SHORTHOP_PORT` / `SHORTHOP_PUBLIC_PORT`.

## Upgrading

```bash
docker compose pull && docker compose up -d
```

The database is migrated on start. `SHORTHOP_VERSION=0` follows every 0.x release; pin an exact
version (e.g. `0.1.0`) to upgrade on your own schedule. Back up first — see
[Running it](../operations.md#backups).

Next: [first steps](../getting-started.md).
