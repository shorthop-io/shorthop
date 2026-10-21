# Security

## Two listeners

| | Port 8080 — private | Port 8081 — public |
|---|---|---|
| Web UI, sign-in, Google SSO | ✓ | — |
| Alert ingest, MCP, `/metrics`, API docs | ✓ | — |
| API (`/api/v1/*`) | any credential | **phone tokens only** — no cookies, no personal or integration tokens |
| Phone pairing, calendar feeds, `/healthz` | ✓ | ✓ |

Put only 8081 on the internet. Which listener a request is for is decided by the socket it
arrived on, never by a header, so a misconfigured proxy cannot open the private side.

## Credentials

| | Looks like | Used by | Can |
|---|---|---|---|
| Integration token | `shi_…` | a sender (Prometheus, a webhook) | send alerts to its own door only |
| Personal token | `sh_…` | scripts, MCP | act as its owner, limited to the scopes it was made with |
| Phone token | `shd_…` | the app | page, acknowledge, read the rota — never change configuration |

Tokens are stored as hashes and shown once. A personal token is re-checked against its owner's
role on every request, so demoting or disabling someone takes effect at once.

## Sign-in

Google SSO (optional) or a password. People are invited first — nobody can sign themselves up.
Failed password sign-ins are rate-limited per address and per email.

## Outbound

Webhook channels may only call public addresses, checked when saved and before every send. List
internal hosts you do want to call in `WEBHOOK_PRIVATE_HOSTS`.

## Reporting a vulnerability

Email **security@shorthop.io**. Please do not open a public issue.
