# Connect Prometheus

Create an integration of kind **Prometheus (direct)** under **Integrations** and copy its token
(`shi_…`, shown once). Then point Prometheus at Shorthop instead of — or as well as — an
Alertmanager. Shorthop speaks Alertmanager's own API, so nothing else changes.

```yaml
# prometheus.yml
alerting:
  alertmanagers:
    - scheme: https            # http inside a cluster
      path_prefix: /ingest/prometheus
      authorization:
        credentials: shi_…
      static_configs:
        - targets: [oncall.internal.example.com]
```

With the Prometheus Operator, add an entry to `alertingEndpoints` (it replaces the default
list, so keep your Alertmanager in it while you migrate):

```yaml
alertingEndpoints:
  - name: shorthop
    namespace: shorthop
    port: http
    pathPrefix: /ingest/prometheus
    apiVersion: v2
    authorization:
      type: Bearer
      credentials: { name: prometheus-shorthop, key: token }   # a Secret holding shi_…
```

Prometheus's always-firing **Watchdog** alert is recorded as proof the path works, never opened
as an incident. If it stops arriving, Shorthop raises `ShorthopIngestSilent`.

## Migrating from Alertmanager

Send to both for a week. Give the integration your Prometheus's address (**Integrations → the
Prometheus → Compare with Prometheus**) and Shorthop checks every minute that what Prometheus
says is firing matches what is open in Shorthop. When it has agreed for a week, remove
Alertmanager.

## Alertmanager webhook

To keep Alertmanager and forward to Shorthop, create an integration of kind **Alertmanager
webhook** and add a receiver:

```yaml
receivers:
  - name: shorthop
    webhook_configs:
      - url: https://oncall.internal.example.com/ingest/alertmanager
        http_config:
          authorization: { credentials: shi_… }
```

## Anything else (Grafana, CI, scripts)

An integration of kind **Generic webhook** takes JSON at `/ingest/webhook` — one alert, a list,
or `{"alerts": [...]}`:

```bash
curl -fsS https://oncall.internal.example.com/ingest/webhook \
  -H "Authorization: Bearer shi_…" -H "Content-Type: application/json" \
  -d '{"name": "BackupFailed", "severity": "critical", "summary": "Nightly backup failed",
       "labels": {"service": "db"}, "status": "firing"}'
```

Fields: `name` (becomes `alertname`), `severity`, `summary`, `description`, `runbook_url`,
`labels`, `annotations`, `status` (`firing` / `resolved`), `starts_at`, `ends_at`, `url`. A
firing alert without `ends_at` resolves itself 5 minutes after it was last sent
(`DEFAULT_ALERT_TIMEOUT_MINUTES`); send it again to keep it open, or send `"status":
"resolved"`.
