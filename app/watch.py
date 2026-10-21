"""Shorthop Watch, from this server's side (relay SHOR-31 / W7).

Shorthop Watch is a dead man's switch run by the push relay, OUTSIDE this installation: a
check expects a ping on a schedule, and when the pings stop the relay pages this
installation's phones itself — which is the only way to be paged when this server, its
database or the whole cluster is down.

Two things here:

* **Forwarding a Prometheus Watchdog** (`forward_watchdogs`, on the leader's housekeeping
  tick). An integration bound to a check gets one ping per NEW Watchdog, at most once per
  WATCH_FORWARD_MIN_SECONDS. Sent by the leader rather than on ingest, so a ping proves the
  whole chain — Prometheus delivered, ingest stored it, and housekeeping is running — and so
  the two replicas (each receiving every batch) do not both ping. Shorthop's own
  ShorthopIngestSilent fires after WATCHDOG_STALE_MINUTES; the Watch check's default grace is
  longer, so Shorthop pages first with context and Watch only when Shorthop is quiet too.
* **Managing checks** through the relay's `/v1/watch` API with the relay key already stored
  for push (`call`), used by app/api/watch.py.

⚠️ A ping URL is a bearer credential: stored write-only, never returned, never logged — log
lines name the integration, never the URL.
"""
from __future__ import annotations

import datetime as dt
import logging
from urllib.parse import urlparse

import requests
from prometheus_client import Counter
from sqlalchemy import select

from . import config as cfg
from . import relay
from .db import SessionLocal
from .models import Integration, now

logger = logging.getLogger(__name__)

TIMEOUT = 10
FORWARDS = Counter("shorthop_watch_forwards_total",
                   "Watchdogs forwarded to a Shorthop Watch check, by outcome.", ["outcome"])
for _o in ("ok", "error"):
    FORWARDS.labels(_o)


def valid_ping_url(url: str) -> bool:
    """https://host/p/<token> and nothing else — the only shape the relay hands out."""
    try:
        p = urlparse(url)
    except ValueError:
        return False
    return (p.scheme == "https" and bool(p.hostname) and not p.username and not p.password
            and p.path.startswith("/p/") and p.path.count("/") == 2 and len(p.path) > 3
            and not p.query and not p.fragment)


def default_grace_s() -> int:
    """Longer than ShorthopIngestSilent's threshold, so Shorthop pages first (W7.4)."""
    return (cfg.WATCHDOG_STALE_MINUTES + 5) * 60


def call(method: str, path: str, json: dict | None = None, params: dict | None = None):
    """The relay's Watch API. Raises relay.RelayError (caller-safe) when it is unreachable or
    not configured; otherwise returns the response for the caller to map."""
    conf = relay.config()
    if conf is None:
        raise relay.RelayError("Shorthop Watch runs on the push relay — set Settings → Push "
                               "relay first")
    url, key = conf
    try:
        return requests.request(method, f"{url}/v1/watch{path}", json=json, params=params,
                                timeout=TIMEOUT, allow_redirects=False,
                                headers={"Authorization": f"Bearer {key}",
                                         "Accept": "application/json"})
    except requests.RequestException as e:
        raise relay.RelayError(f"could not reach the push relay ({type(e).__name__})") from None


def _due(integ: Integration, t: dt.datetime) -> bool:
    if not integ.watch_ping_url or integ.last_watchdog_at is None:
        return False
    last = integ.watch_forwarded_at
    if last is None:
        return True
    return (_aware(integ.last_watchdog_at) > _aware(last)
            and (t - _aware(last)).total_seconds() >= cfg.WATCH_FORWARD_MIN_SECONDS)


def _aware(v: dt.datetime) -> dt.datetime:
    return v if v.tzinfo else v.replace(tzinfo=dt.timezone.utc)


def forward_watchdogs() -> dict:
    """Ping each bound check whose integration has a Watchdog newer than the last forward.
    Leader only (scheduler.tick). Never raises; a failed ping is retried next tick, because
    `watch_forwarded_at` only moves on success."""
    t = now()
    out = {"ok": 0, "error": 0}
    for integ in SessionLocal.scalars(select(Integration).where(
            Integration.watch_ping_url.is_not(None), Integration.enabled.is_(True),
            Integration.archived_at.is_(None))).all():
        if not _due(integ, t):
            continue
        try:
            resp = requests.get(integ.watch_ping_url, timeout=TIMEOUT, allow_redirects=False)
            ok = resp.status_code == 200
        except requests.RequestException:
            ok = False
        if ok:
            integ.watch_forwarded_at = t
            SessionLocal.commit()
            out["ok"] += 1
            FORWARDS.labels("ok").inc()
        else:
            out["error"] += 1
            FORWARDS.labels("error").inc()
            logger.warning("Watchdog not forwarded to Shorthop Watch",
                           extra={"integration": integ.name})
    return out
