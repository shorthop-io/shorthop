# Install on Kubernetes (Helm)

For production: two or more replicas, rolling upgrades, and a Postgres you already run.

## What you need

- Kubernetes 1.25 or newer, and Helm 3.
- **A Postgres database** (14 or newer) and a user that owns it. The chart does not bring one:
  use CloudNativePG, your cloud's managed Postgres, or the server your other services use.
  Shorthop creates and migrates its own tables.

## 1. A Secret

```bash
kubectl create namespace shorthop
kubectl -n shorthop create secret generic shorthop \
  --from-literal=SECRET_KEY="$(openssl rand -hex 32)" \
  --from-literal=POSTGRES_PASSWORD='the database password'
```

Optional keys in the same Secret: `RELAY_KEY`, `SMTP_PASSWORD`, `GOOGLE_CLIENT_SECRET`,
`SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `METRICS_TOKEN`.

## 2. Values

```yaml
# values.yaml
postgres:
  host: postgres-rw.databases.svc
  database: shorthop
  user: shorthop
  sslmode: require

secret:
  existingSecret: shorthop

env:
  BOOTSTRAP_ADMIN_EMAIL: you@example.com
  PUBLIC_URL: https://oncall.internal.example.com
  PUBLIC_API_URL: https://oncall.example.com     # the public listener, for phones

ingress:                  # the web UI — an INTERNAL ingress class
  enabled: true
  className: internal
  host: oncall.internal.example.com

publicIngress:            # only what phones need — the one thing on the internet
  enabled: true
  className: public
  host: oncall.example.com
```

## 3. Install

```bash
helm install shorthop oci://ghcr.io/shorthop-io/charts/shorthop -n shorthop -f values.yaml
kubectl -n shorthop exec -it deploy/shorthop -- python -m app.cli admin you@example.com
```

The second command sets the first admin's password.

## Replicas

`replicaCount: 2` is the default and is safe: every pod sends notifications from a shared
queue (nothing is sent twice), exactly one pod runs housekeeping at a time and another takes
over within a tick, and the database migration on start takes a lock. A PodDisruptionBudget
keeps one pod up during node drains. More replicas spread the load; they never change
behaviour.

## Metrics

`serviceMonitor.enabled: true` creates a ServiceMonitor for the Prometheus Operator. To alert
on Shorthop itself, see [Running it → monitoring Shorthop](../operations.md#monitoring-shorthop-itself).

## Upgrading

```bash
helm upgrade shorthop oci://ghcr.io/shorthop-io/charts/shorthop -n shorthop -f values.yaml
```

Pods roll one at a time; the first new pod migrates the database. Back up first.

All values: [`values.yaml`](https://github.com/shorthop-io/shorthop/blob/main/deploy/helm/shorthop/values.yaml).
