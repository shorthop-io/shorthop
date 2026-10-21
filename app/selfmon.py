"""Shorthop watching its own inputs, run from the scheduler.

Two conditions, each raised as an ordinary alert from a built-in `shorthop` integration, so
they appear in the alert list like anything else (and, from M2, get delivered like
anything else):

* **ShorthopIngestSilent** — a Prometheus integration's Watchdog has not arrived for
  WATCHDOG_STALE_MINUTES. The Watchdog alert is always firing, so silence means the path from
  that Prometheus is broken — the one failure where nothing else would ever tell you.
* **ShorthopShadowMismatch** — that Prometheus reports firing alerts Shorthop does not have
  open (or the reverse), confirmed across two checks. See core/shadow.py.

Each is resolved explicitly when the condition clears, not left to expire.
"""
import datetime as dt
import logging

from prometheus_client import Gauge
from sqlalchemy import select

from . import alerts as alert_ops
from . import config as cfg
from . import promclient
from .auth import INTEGRATION_TOKEN_PREFIX, new_token
from .core import shadow
from .db import SessionLocal
from .models import Integration, Occurrence, aware, now

logger = logging.getLogger(__name__)

INTERNAL_NAME = "shorthop"

WATCHDOG_AGE = Gauge("shorthop_integration_watchdog_age_seconds",
                     "Seconds since this Prometheus integration's Watchdog last arrived.",
                     ["integration"])
SHADOW_MISSING = Gauge("shorthop_shadow_missing_alerts",
                       "Alerts firing in Prometheus but not open in Shorthop (confirmed).",
                       ["integration"])
SHADOW_EXTRA = Gauge("shorthop_shadow_extra_alerts",
                     "Alerts open in Shorthop but not firing in Prometheus (confirmed).",
                     ["integration"])


def internal_integration() -> Integration:
    """The built-in source of Shorthop's own alerts. Kind `internal` has no ingest door, and
    its token is random and never shown, so nothing outside can send as it."""
    db = SessionLocal
    row = db.scalar(select(Integration).where(Integration.kind == "internal"))
    if row is None:
        _, digest, prefix = new_token(INTEGRATION_TOKEN_PREFIX)
        row = Integration(name=INTERNAL_NAME, kind="internal", token_hash=digest, prefix=prefix,
                          extra_labels={})
        db.add(row)
        db.commit()
    return row


def _raise(name: str, subject: Integration, active: bool, severity: str, summary: str,
           description: str = "") -> None:
    internal = internal_integration()
    if not active:
        # Only resolve what is actually open — otherwise a healthy system sends a stray
        # "resolved" every tick, inflating the counters and the built-in's last delivery.
        open_now = SessionLocal.scalars(select(Occurrence).where(
            Occurrence.integration_id == internal.id, Occurrence.alertname == name,
            Occurrence.resolved_at.is_(None))).all()
        if not any((o.labels or {}).get("integration") == subject.name for o in open_now):
            return
    alert = alert_ops.IncomingAlert(
        labels={"alertname": name, "severity": severity, "integration": subject.name},
        annotations={"summary": summary, **({"description": description} if description else {})},
        starts_at=None,
        # Well past the next check, so a slow tick never lets it expire while still true.
        ends_at=now() + dt.timedelta(seconds=max(cfg.SCHEDULER_INTERVAL_SECONDS,
                                                 cfg.SHADOW_CHECK_INTERVAL_SECONDS) * 4),
        resolved=not active,
    )
    alert_ops.ingest(internal, [alert])


def check_watchdogs() -> None:
    t = now()
    for integ in SessionLocal.scalars(select(Integration).where(
            Integration.kind == "prometheus", Integration.enabled.is_(True))).all():
        if integ.last_watchdog_at is None:
            # Never seen one: this Prometheus may simply have no Watchdog rule. The UI says
            # "no Watchdog seen"; raising an alert for it would fire on day one, forever.
            continue
        age = (t - aware(integ.last_watchdog_at)).total_seconds()
        WATCHDOG_AGE.labels(integ.name).set(age)
        stale = age > cfg.WATCHDOG_STALE_MINUTES * 60
        _raise("ShorthopIngestSilent", integ, stale, "critical",
               f"No Watchdog from {integ.name} for {int(age // 60)} min",
               "The Watchdog alert is always firing, so its absence means alerts from this "
               "Prometheus are not reaching Shorthop. Check Prometheus's alerting config, the "
               "integration token, and the network path.")


def check_shadow(integ: Integration) -> dict:
    """Compare one integration with its Prometheus; store and return the status."""
    previous = integ.shadow_status or {}
    status: dict = {"checked_at": now().isoformat()}
    try:
        base = promclient.check_url(integ.prometheus_url or "")
        ignored = promclient.external_label_names(base) | set((integ.extra_labels or {}).keys())
        prom = promclient.firing_alerts(base)
        open_occ = SessionLocal.scalars(select(Occurrence).where(
            Occurrence.integration_id == integ.id, Occurrence.resolved_at.is_(None))).all()
        # ⚠️ Occurrence.labels are labels AFTER the label rules ran (`level=P2`), while
        # Prometheus reports them as sent — so every rule-enriched alert read as missing on
        # one side and extra on the other. A label a rule set is ours, not the sender's:
        # leave it out of the comparison on both sides (a rule may also have overwritten a
        # sent label, so stripping it from our side alone would not match either).
        for o in open_occ:
            ignored |= set((o.rule_labels or {}).keys())
        mine = [o.labels or {} for o in open_occ]
        status.update(shadow.compare(prom, mine, ignored, previous))
        status["error"] = None
    except promclient.PrometheusError as e:
        status.update({"error": str(e), "missing": [], "extra": [],
                       "missing_keys": [], "extra_keys": []})
    integ.shadow_status = status
    SessionLocal.commit()
    SHADOW_MISSING.labels(integ.name).set(len(status.get("missing", [])))
    SHADOW_EXTRA.labels(integ.name).set(len(status.get("extra", [])))

    missing, extra = len(status.get("missing", [])), len(status.get("extra", []))
    names = ", ".join(sorted({m["alertname"] for m in status.get("missing", [])})[:5])
    _raise("ShorthopShadowMismatch", integ, bool(missing or extra), "warning",
           f"{integ.name}: {missing} firing in Prometheus not in Shorthop, {extra} the other way",
           f"Missing: {names}" if names else "")
    return status


def check_all_shadows() -> None:
    for integ in SessionLocal.scalars(select(Integration).where(
            Integration.kind == "prometheus", Integration.enabled.is_(True),
            Integration.prometheus_url.is_not(None))).all():
        try:
            check_shadow(integ)
        except Exception:  # noqa: BLE001 — one bad integration must not stop the others
            SessionLocal.rollback()
            logger.exception("shadow check failed for %s", integ.name)
