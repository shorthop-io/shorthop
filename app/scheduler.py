"""The background loops: two threads, in-process, on EVERY replica.

Escalations (M2) need timing to the second, which a CronJob cannot give, so periodic work
lives here rather than in CronJobs. Running several replicas is safe (see app/locks.py):

  * the DISPATCH loop runs on every replica — escalation steps and notifications are claimed
    row by row with `SKIP LOCKED`, so replicas share the queue and nothing is sent twice;
  * the housekeeping TICK runs only on the LEADER (a Postgres advisory lock). The others
    check every interval and take over within one tick when the leader's session dies.

Each tick (leader only):
  1. resolves alerts whose sender stopped repeating them (sweep_expired);
  2. raises/clears ShorthopIngestSilent per Prometheus integration (selfmon), and forwards
     each NEW Watchdog to its Shorthop Watch check, if bound (app/watch.py);
  3. every SHADOW_CHECK_INTERVAL_SECONDS, compares each Prometheus with what is open here;
  4. pings HEARTBEAT_URL — the dead-man's switch, watched by something OUTSIDE Shorthop
     (a Shorthop Watch check's ping URL works here: see README → Shorthop Watch).
     ⚠️ Only while this process's DISPATCH loop is completing ticks (SHOR-24): the ping means
     "this Shorthop can deliver a page", so a dispatch loop that hangs while the pod stays up
     stops the ping, and the outside watcher pages — the one path that does not go through
     the dispatch that is stuck.

A second, FASTER loop (`DISPATCH_INTERVAL_SECONDS`, 5 s) runs due escalation steps and sends
pending notifications — a page must not wait 30 s for the housekeeping tick to come round.

A tick that raises is logged and the loop carries on: the loop dying silently would be the
worst possible failure for a paging tool.
"""
import logging
import threading
import time

import requests
from prometheus_client import Counter, Gauge

from . import alerts as alert_ops
from . import config as cfg
from . import delivery, incidents, locks, ratelimit, relay, schedules, selfmon, slack, watch
from .db import SessionLocal

logger = logging.getLogger(__name__)

LAST_DISPATCH = Gauge("shorthop_dispatch_last_tick_timestamp_seconds",
                      "Unix time of the last completed dispatch (escalation + delivery) tick.")
LAST_TICK = Gauge("shorthop_scheduler_last_tick_timestamp_seconds",
                  "Unix time of the last completed scheduler tick.")
HEARTBEAT_OK = Gauge("shorthop_heartbeat_last_ok_timestamp_seconds",
                     "Unix time of the last successful heartbeat ping.")
HEARTBEAT_WITHHELD = Counter("shorthop_heartbeat_withheld_total",
                             "Heartbeat pings NOT sent because the dispatch loop is stale.")
HEARTBEAT_WITHHELD.inc(0)

# monotonic time of this process's last completed dispatch tick; None = none yet
_last_dispatch_ok: float | None = None

_started = False
_last_shadow = 0.0
_last_relay = 0.0


def tick() -> dict:
    global _last_shadow, _last_relay
    out = {"expired": 0, "leader": False}
    if not locks.leader.is_leader():
        LAST_TICK.set_to_current_time()  # alive and standing by — not a stalled loop
        return out
    out["leader"] = True
    try:
        out["expired"] = alert_ops.sweep_expired()
        selfmon.check_watchdogs()
        watch.forward_watchdogs()   # to Shorthop Watch, outside this installation
        schedules.check_handovers()
        ratelimit.prune()
        if time.monotonic() - _last_shadow >= cfg.SHADOW_CHECK_INTERVAL_SECONDS:
            _last_shadow = time.monotonic()
            selfmon.check_all_shadows()
        if relay.configured() and time.monotonic() - _last_relay >= cfg.RELAY_SYNC_INTERVAL_SECONDS:
            _last_relay = time.monotonic()
            relay.housekeeping(SessionLocal, notify_unused_phone)
    finally:
        SessionLocal.remove()
    if cfg.HEARTBEAT_URL and not dispatch_healthy():
        HEARTBEAT_WITHHELD.inc()
        logger.error("heartbeat withheld: the dispatch loop has not completed a tick for %ss",
                     "?" if _last_dispatch_ok is None else int(time.monotonic() - _last_dispatch_ok))
    elif cfg.HEARTBEAT_URL:
        try:
            requests.get(cfg.HEARTBEAT_URL, timeout=10)
            HEARTBEAT_OK.set_to_current_time()
        except requests.RequestException as e:
            logger.warning("heartbeat ping failed: %s", type(e).__name__)
    LAST_TICK.set_to_current_time()
    return out


def dispatch() -> dict:
    """Run due escalation steps, then send what they queued — in that order, so a step that
    just became due is delivered in the same tick."""
    try:
        steps = incidents.run_due()
        sent = delivery.deliver_pending()
        redrawn = slack.refresh_dirty()   # Slack messages of incidents that changed
    finally:
        SessionLocal.remove()
    global _last_dispatch_ok
    _last_dispatch_ok = time.monotonic()
    LAST_DISPATCH.set_to_current_time()
    return {"steps": steps, "sent": sent, "slack": redrawn}


def dispatch_healthy(now: float | None = None) -> bool:
    """Has this process's dispatch loop completed a tick recently? Never ticked counts as NOT
    healthy: a process that cannot deliver must not tell the outside it can."""
    if _last_dispatch_ok is None:
        return False
    return (now if now is not None else time.monotonic()) - _last_dispatch_ok \
        <= cfg.HEARTBEAT_DISPATCH_MAX_AGE_SECONDS


def _dispatch_loop() -> None:
    while True:
        try:
            dispatch()
        except Exception:  # noqa: BLE001
            logger.exception("dispatch tick failed")
        time.sleep(cfg.DISPATCH_INTERVAL_SECONDS)


def _loop() -> None:
    while True:
        try:
            result = tick()
            if result["expired"]:
                logger.info("scheduler: resolved %s expired alert(s)", result["expired"])
        except Exception:  # noqa: BLE001
            logger.exception("scheduler tick failed")
        time.sleep(cfg.SCHEDULER_INTERVAL_SECONDS)


def start() -> None:
    global _started
    if _started or not cfg.SCHEDULER_ENABLED:
        return
    _started = True
    threading.Thread(target=_loop, name="scheduler", daemon=True).start()
    threading.Thread(target=_dispatch_loop, name="dispatch", daemon=True).start()
    slack.start()   # Socket Mode, on every replica — Slack delivers each click to one of them


def notify_unused_phone(dev, kind: str, when) -> None:
    """Email a phone's owner and every admin that the push relay is about to sign it out
    (kind="warning", at `when`) or has (kind="signed_out"). Best effort: the warning push
    to the phone itself has already gone out from the relay."""
    from .models import User

    db = SessionLocal
    owner = dev.user
    to = {u.email for u in db.query(User).filter(User.role == "admin", User.disabled.is_(False)).all()}
    if owner is not None and not owner.disabled:
        to.add(owner.email)
    whose = f"{owner.name or owner.email}'s phone" if owner else "A phone"
    if kind == "warning":
        date = f"{when:%a %d %b %Y}" if when else "soon"
        subject = f"[Shorthop] {dev.name} will stop receiving pages on {date}"
        body = (f"{whose} \"{dev.name}\" has not been used with Shorthop for a month.\n\n"
                f"Unless the Shorthop app is opened on it, it is signed out of the push relay on "
                f"{date} and stops receiving pages. Opening the app once is enough.\n\n"
                "If the phone is no longer used, unpair it in Shorthop (My account → Phones) to "
                "free its seat now.\n")
    else:
        subject = f"[Shorthop] {dev.name} was signed out and no longer receives pages"
        body = (f"{whose} \"{dev.name}\" went unused for two months and was signed out of the "
                "push relay. It receives no pages until the Shorthop app is opened on it again.\n")
    if cfg.PUBLIC_URL:
        body += f"\n{cfg.PUBLIC_URL}/account?tab=phones\n"
    for addr in sorted(to):
        try:
            delivery.send_email(addr, subject, body)
        except delivery.DeliveryError as e:
            logger.warning("unused-phone notice to %s failed: %s", addr, e)
