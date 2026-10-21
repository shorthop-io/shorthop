# Shorthop

**Alerting and on-call, without the hops.** Prometheus sends its alerts straight to Shorthop,
which does what Alertmanager and a paging service do together: routing, grouping, silences,
on-call schedules, escalation, and paging people by phone, email, Slack and webhooks. Every
alert keeps its history, so the next time it fires you see what fixed it last time.

You run Shorthop yourself. Your alerts never leave your servers.

## What you need

- A server or Kubernetes cluster to run it on, and **Postgres** (included in the Docker
  Compose setup).
- Prometheus (or anything that can send a webhook).
- Optional: the **Shorthop app** on your team's phones. Pages reach it through our push relay;
  the first 3 phones are free, then €1.99 per phone a month. Email, Slack and webhooks are free
  and need nothing from us.

## Install

- [Docker Compose](install/docker-compose.md) — one machine, five minutes.
- [Kubernetes (Helm)](install/kubernetes.md) — two or more replicas, for production.

Then: [first steps](getting-started.md) → [connect Prometheus](connect/prometheus.md) →
[phones](phones.md).

## How it fits together

```
Prometheus ──alerts──▶ Shorthop (your servers) ──▶ email · Slack · webhooks
                           │
                           └── incident id only ──▶ push relay (shorthop.io) ──▶ the Shorthop app
                                                                                     │
                       the app fetches the details from YOUR Shorthop ◀──────────────┘
```

The relay never sees the content of an alert: the notification says only that something needs
attention, and the app asks your own Shorthop for the rest.

## More

- [Configuration](configuration.md) — every setting.
- [Running it](operations.md) — upgrades, backups, high availability, monitoring Shorthop itself.
- [Security](security.md) — the two listeners, tokens and what to expose.
- [Shorthop Watch](watch.md) — being paged when Shorthop itself goes quiet.
- [Claude and MCP](mcp.md) — let an assistant read and act on your alerts.

Licence: the server is AGPL-3.0, the app Apache-2.0.
