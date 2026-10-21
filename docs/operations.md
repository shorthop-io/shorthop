# Running it

## Upgrading

New releases are images tagged with their version (`0.1.0`), plus `0.1` and `0` that follow the
latest of each line. The database is migrated when the first new pod starts, under a lock, so a
rolling upgrade is safe. Read the release notes before a minor version, and back up first.

A downgrade is not supported once a newer version has migrated the database: restore the
backup instead.

## Backups

Everything Shorthop knows is in its Postgres database — nothing on disk, nothing in the pods.
Back that database up the way you back up any other (`pg_dump`, your operator's scheduled
backups, or your cloud's snapshots), and test a restore.

```bash
# Docker Compose
docker compose exec postgres pg_dump -U shorthop shorthop | gzip > shorthop-$(date +%F).sql.gz
```

Keep `SECRET_KEY` with the backup: a restore with a different key works, but signs everyone out.

## High availability

Run two or more replicas against one Postgres. There is no state in the pods to keep in step:

- every pod sends notifications from a shared queue, and a row is claimed before it is sent, so
  nothing is sent twice by two pods (a pod that dies mid-send means the page is retried — a
  duplicate is possible, a lost page is not);
- exactly one pod is the **leader** and runs housekeeping; if it goes, another takes over on its
  next tick;
- the migration on start takes a lock.

Postgres is then the part that matters: give it a standby. After a database failover Shorthop
drops its old connections on the first error and carries on — no restart needed.

## Monitoring Shorthop itself

Shorthop is the thing that pages you, so watch it from **outside**:

- **`HEARTBEAT_URL`** — the leader pings it every 30 seconds, but **only while this Shorthop can
  send pages** (its delivery loop is running). Point it at a [Shorthop Watch](watch.md) check or
  any dead man's switch (Healthchecks.io works). When the pings stop, something outside pages
  you.
- **Prometheus metrics** at `/metrics` on port 8080 (`METRICS_TOKEN` to require a token).
  Useful alerts — Shorthop delivers these itself, so they cover the partial failures:

```yaml
- alert: ShorthopNoLeader
  expr: (sum(shorthop_leader) or vector(0)) == 0
  for: 2m
- alert: ShorthopReplicaDispatchStalled
  expr: time() - shorthop_dispatch_last_tick_timestamp_seconds > 120
  for: 2m
- alert: ShorthopIngestSilent        # raised by Shorthop itself when a Watchdog stops
```

## Command line

Inside the container:

```bash
python -m app.cli admin you@example.com      # make an admin / reset their password
```
