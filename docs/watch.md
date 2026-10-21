# Shorthop Watch

Shorthop raises `ShorthopIngestSilent` when a Prometheus goes quiet — but that runs inside
Shorthop, so it cannot fire when Shorthop itself, its database or the whole cluster is down.

**Shorthop Watch** is a dead man's switch that runs on our side, outside your installation. A
check expects a ping on a schedule; when the pings stop, the relay pages your phones directly
and emails your contact address. It needs the [push relay](phones.md). The first check is free,
then €1.99 per check a month.

- **A Prometheus** — Integrations → the Prometheus → *Watch from outside*. Shorthop forwards its
  Watchdog, so one check proves Prometheus → Shorthop → the network all work.
- **Shorthop itself** — create a check (e.g. every minute, 5 minutes' grace) and set its ping URL
  as `HEARTBEAT_URL`. The leader pings it only while it can actually send pages.
- **Anything else** — backups, cron jobs: `your-job && curl -fsS -m 10 --retry 3 <ping URL>`.

A ping URL is a credential: anyone holding it can keep a dead check looking alive. It is shown
once; *New URL* replaces it.

What Watch sends outside your installation: the check's name, its state and two times, in the
push and the email. Name checks with that in mind.
