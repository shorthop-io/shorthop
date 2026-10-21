# Shorthop

**Alerting and on-call, without the hops.** Prometheus sends its alerts straight to Shorthop,
which does what Alertmanager and a paging service do together: routing, grouping, silences,
on-call schedules, escalation, and paging people by phone, email, Slack and webhooks. Every
alert remembers what fixed it last time.

You run it yourself; your alerts never leave your servers.

- **Documentation:** https://docs.shorthop.io (also in [`docs/`](docs/index.md))
- **Install:** [Docker Compose](docs/install/docker-compose.md) · [Kubernetes (Helm)](docs/install/kubernetes.md)
- **Website:** https://www.shorthop.io

## Quick start

```bash
curl -fsSLO https://raw.githubusercontent.com/shorthop-io/shorthop/main/deploy/compose/docker-compose.yml
curl -fsSL  https://raw.githubusercontent.com/shorthop-io/shorthop/main/deploy/compose/.env.example -o .env
# fill in POSTGRES_PASSWORD, SECRET_KEY and BOOTSTRAP_ADMIN_EMAIL in .env, then:
docker compose up -d
docker compose exec shorthop python -m app.cli admin you@example.com
```

Open http://localhost:8080.

## Price

The software is free and open source. Email, Slack and webhook paging are free. Paging phones
through the Shorthop app goes through our push relay: the first 3 phones are free, then €1.99
per phone a month.

## Licence

AGPL-3.0. The mobile app is Apache-2.0. "Shorthop" and the logo are trademarks: a modified
copy you publish must use a different name and icon.

Development: [docs/development.md](docs/development.md).
