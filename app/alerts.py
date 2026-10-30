"""Alert operations: ingest, acknowledge, resolve, notes, silences, history.

Every door (REST API, MCP, ingest) calls these functions, so a rule is enforced once.
Functions take and return ORM rows; they commit.
"""
import datetime as dt
import logging
from dataclasses import dataclass

from prometheus_client import Counter
from sqlalchemy import func, select

from . import config as cfg
from . import locks
from .core import enrich, identity, matchers
from .db import SessionLocal
from .models import Integration, LabelRule, Occurrence, Silence, TimelineEvent, aware, now

logger = logging.getLogger(__name__)

RECEIVED = Counter(
    "shorthop_alerts_received_total",
    "Alerts received, by integration kind and what happened to them.",
    ["kind", "outcome"],
)
for _k in ("prometheus", "alertmanager", "webhook"):
    for _o in ("opened", "updated", "resolved", "ignored"):
        RECEIVED.labels(_k, _o)  # materialise at 0 — "nothing arrived" must read as 0

# Categories offered when resolving. Free text goes in the note; the category makes the
# history reportable ("this one is a false positive 9 times out of 10").
RESOLUTION_CATEGORIES = (
    "fixed", "restarted", "scaled", "config_change", "rollback", "upstream",
    "false_positive", "no_action", "other",
)

WATCHDOG_ALERTNAMES = {"Watchdog", "DeadMansSwitch"}


class AlertError(ValueError):
    """Caller-safe refusal; the message is shown to the client."""


@dataclass
class IncomingAlert:
    """One alert, normalised from any of the three wire formats."""
    labels: dict[str, str]
    annotations: dict[str, str]
    starts_at: dt.datetime | None
    ends_at: dt.datetime | None
    generator_url: str = ""
    resolved: bool = False


def _event(occ: Occurrence, kind: str, actor: str = "", detail: str = "") -> None:
    occ.events.append(TimelineEvent(kind=kind, actor=actor or "", detail=detail or ""))


# --- ingest ------------------------------------------------------------------------

def label_rules() -> list[enrich.RuleView]:
    """Enabled label rules as pure views — loaded once per batch, never once per alert."""
    return [enrich.RuleView(r.id, r.name, r.position, r.matchers or [], r.text_pattern,
                            r.text_field, r.set_labels or {}, r.integration_ids, r.stop, True)
            for r in SessionLocal.scalars(select(LabelRule).where(LabelRule.enabled.is_(True)))]


def _enrich(occ: Occurrence, rules, sent_labels: dict, annotations: dict, integration_id: str,
            new: bool) -> None:
    """Apply label rules to an occurrence. Its identity was already fixed from `sent_labels`."""
    res = enrich.apply(rules, sent_labels, annotations, integration_id)
    changed = res.labels != (occ.labels or {})
    occ.labels = res.labels
    occ.rule_labels = res.set_by or None
    occ.severity = res.labels.get("severity", "")[:40]
    if res.set_by and (new or changed):
        detail = ", ".join(f"{k}={res.labels[k]} ({rule})" for k, rule in sorted(res.set_by.items()))
        _event(occ, "labels", "label rules", ("set " if new else "now ") + detail)
    elif changed and not new:
        _event(occ, "labels", "label rules", "no label rule applies any more — labels as sent")


def ingest(integration: Integration, alerts: list[IncomingAlert]) -> dict[str, int]:
    """Upsert a batch. Returns counts per outcome.

    Semantics follow Alertmanager's: an alert whose `endsAt` is in the past is resolved; one
    still firing extends its expiry. A firing alert with no endsAt expires
    DEFAULT_ALERT_TIMEOUT_MINUTES after it was last seen — the sender is expected to repeat it.
    """
    db = SessionLocal
    t = now()
    counts = {"opened": 0, "updated": 0, "resolved": 0, "ignored": 0}
    ignore = integration.signature_ignore or cfg.SIGNATURE_IGNORE_LABELS
    timeout = dt.timedelta(minutes=cfg.DEFAULT_ALERT_TIMEOUT_MINUTES)

    rules = label_rules()
    prepared = []
    opened: list[Occurrence] = []
    closed: list[Occurrence] = []
    for a in alerts:
        labels = {str(k): str(v) for k, v in (a.labels or {}).items()}
        labels.update(integration.extra_labels or {})
        if not labels.get("alertname"):
            counts["ignored"] += 1
            continue
        prepared.append((a, labels, identity.fingerprint(integration.id, labels)))

    # ⚠️ One ingest per integration at a time, across replicas and request threads. With two
    # replicas this is the NORMAL case, not a rare one: Prometheus sends every batch to each
    # pod at once. Two batches carrying the same new alert would otherwise both read "not
    # open", both insert,
    # and the second fails the partial unique index → a 500 for the whole batch. A
    # fingerprint includes the integration id, so this is the narrowest lock that closes it,
    # and an integration sends a handful of batches a minute. Held until the commit below.
    locks.xact_lock(db, locks.key_for("ingest:" + integration.id))

    # One query for the whole batch, never one per alert.
    fps = list({fp for _, _, fp in prepared})
    open_rows = {}
    if fps:
        for occ in db.scalars(select(Occurrence).where(
                Occurrence.fingerprint.in_(fps), Occurrence.resolved_at.is_(None))):
            open_rows[occ.fingerprint] = occ

    for a, labels, fp in prepared:
        ends = aware(a.ends_at)
        resolved = a.resolved or (ends is not None and ends <= t)
        occ = open_rows.get(fp)

        if labels.get("alertname") in WATCHDOG_ALERTNAMES:
            # The always-firing heartbeat from Prometheus: proof the pipeline is alive, not
            # an incident. Recorded on the integration, never opened as an occurrence.
            integration.last_watchdog_at = t
            counts["ignored"] += 1
            continue

        if occ is None:
            if resolved:
                counts["ignored"] += 1  # a resolve for something we never saw open
                continue
            sig, sig_labels = identity.signature(labels, ignore)
            started = aware(a.starts_at) or t
            occ = Occurrence(
                integration_id=integration.id,
                team_id=integration.team_id,
                fingerprint=fp,
                signature=sig,
                signature_labels=sig_labels,
                alertname=labels.get("alertname", "")[:200],
                severity=labels.get("severity", "")[:40],
                labels=labels,
                annotations=dict(a.annotations or {}),
                generator_url=a.generator_url or "",
                status="firing",
                started_at=min(started, t),
                last_seen_at=t,
                expires_at=ends if ends and ends > t else t + timeout,
            )
            _event(occ, "fired", integration.name)
            # After identity, before routing: rules may set what routing and silences match.
            _enrich(occ, rules, labels, occ.annotations, integration.id, new=True)
            db.add(occ)
            open_rows[fp] = occ
            opened.append(occ)
            counts["opened"] += 1
            continue

        if resolved:
            _close(occ, "source", integration.name, at=ends if ends and ends <= t else t)
            closed.append(occ)
            counts["resolved"] += 1
            continue

        occ.last_seen_at = t
        occ.send_count = (occ.send_count or 0) + 1
        occ.annotations = dict(a.annotations or {}) or occ.annotations
        # Re-applied on every send: a rule edited, or text that changed, shows on the alert.
        # The incident it belongs to was chosen when it opened and is not re-routed.
        _enrich(occ, rules, labels, occ.annotations, integration.id, new=False)
        occ.expires_at = ends if ends and ends > t else t + timeout
        counts["updated"] += 1

    integration.last_received_at = t
    db.commit()
    # Routing and incidents AFTER the alerts are safely stored: a routing bug must never be
    # able to lose an alert.
    from . import incidents
    try:
        incidents.attach(opened)
        incidents.after_alerts_resolved(closed, integration.name)
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("routing failed for a batch from %s", integration.name)
    for outcome, n in counts.items():
        if n:
            RECEIVED.labels(integration.kind, outcome).inc(n)
    return counts


def _close(occ: Occurrence, how: str, actor: str, at: dt.datetime | None = None,
           category: str | None = None, note: str | None = None) -> None:
    occ.status = "resolved"
    occ.resolved_at = at or now()
    occ.resolved_how = how
    occ.resolved_by = actor or None
    if category:
        occ.resolution_category = category
    if note:
        occ.resolution_note = note
    detail = {"source": "resolved by the sender", "timeout": "stopped being sent",
              "manual": "resolved by hand"}[how]
    _event(occ, "resolved", actor, detail + (f" — {note}" if note else ""))


def sweep_expired() -> int:
    """Resolve every open occurrence whose sender stopped repeating it. Without this an
    alert whose rule was deleted would read as firing forever."""
    db = SessionLocal
    t = now()
    rows = db.scalars(select(Occurrence).where(
        Occurrence.resolved_at.is_(None), Occurrence.expires_at <= t)).all()
    for occ in rows:
        _close(occ, "timeout", "shorthop", at=aware(occ.expires_at))
    if rows:
        db.commit()
        from . import incidents
        incidents.after_alerts_resolved(rows, "shorthop")
    return len(rows)


# --- people acting on alerts --------------------------------------------------------

def get(occurrence_id: str) -> Occurrence:
    occ = SessionLocal.get(Occurrence, occurrence_id)
    if occ is None:
        raise LookupError("alert not found")
    return occ


def acknowledge(occurrence_id: str, actor: str) -> Occurrence:
    occ = get(occurrence_id)
    if occ.resolved_at is not None:
        raise AlertError("this alert is already resolved")
    if occ.status != "acknowledged":
        occ.status = "acknowledged"
        occ.acked_at = now()
        occ.acked_by = actor
        _event(occ, "acknowledged", actor)
        SessionLocal.commit()
        from . import incidents
        incidents.on_alert_acknowledged(occ, actor)
    return occ


def resolve(occurrence_id: str, actor: str, category: str | None = None,
            note: str | None = None) -> Occurrence:
    """Resolve by hand. If the condition is still true the sender re-sends it and a NEW
    occurrence opens — that is the correct answer, not a bug: the problem came back."""
    if category and category not in RESOLUTION_CATEGORIES:
        raise AlertError(f"unknown category; use one of: {', '.join(RESOLUTION_CATEGORIES)}")
    occ = get(occurrence_id)
    if occ.resolved_at is not None:
        # Already closed (often by the sender, seconds earlier): still record WHAT FIXED IT,
        # which is the part only a human can add.
        if category or note:
            occ.resolution_category = category or occ.resolution_category
            occ.resolution_note = note or occ.resolution_note
            _event(occ, "resolution_noted", actor, note or category or "")
            SessionLocal.commit()
        return occ
    _close(occ, "manual", actor, category=category, note=(note or "").strip() or None)
    SessionLocal.commit()
    from . import incidents
    incidents.after_alerts_resolved([occ], actor)
    return occ


# A timeline note is either a person's ("note") or an automation's ("suggestion" — e.g. an AI
# triage agent's summary). ⚠️ The two must never look alike: a suggestion is a guess written
# by something that was not there, and a responder reading it as a colleague's finding is the
# failure this exists to prevent. Neither feeds history — "what fixed it" is the resolution
# note, written only by resolving.
NOTE_KINDS = ("note", "suggestion")


def check_note(text: str, kind: str) -> str:
    text = (text or "").strip()
    if not text:
        raise AlertError("note is empty")
    if kind not in NOTE_KINDS:
        raise AlertError(f"kind must be one of {', '.join(NOTE_KINDS)}")
    return text


def add_note(occurrence_id: str, actor: str, text: str, kind: str = "note") -> Occurrence:
    text = check_note(text, kind)
    occ = get(occurrence_id)
    _event(occ, kind, actor, text[:4000])
    SessionLocal.commit()
    return occ


# --- reading ------------------------------------------------------------------------

def list_occurrences(status: str = "open", label_filters: dict[str, str] | None = None,
                     q: str = "", team_id: str | None = None, limit: int = 100):
    """status: open | firing | acknowledged | resolved | all."""
    stmt = select(Occurrence)
    if status == "open":
        stmt = stmt.where(Occurrence.resolved_at.is_(None))
    elif status in ("firing", "acknowledged", "resolved"):
        stmt = stmt.where(Occurrence.status == status)
    if team_id:
        stmt = stmt.where(Occurrence.team_id == team_id)
    if q:
        stmt = stmt.where(Occurrence.alertname.ilike(f"%{q}%"))
    order = (Occurrence.resolved_at.desc() if status == "resolved"
             else Occurrence.started_at.desc())
    limit = max(1, min(limit, 1000))
    fetch = min(limit * 4, 4000) if label_filters else limit
    rows = SessionLocal.scalars(stmt.order_by(order).limit(fetch)).all()
    if label_filters:
        # Filtered in Python so the same code serves SQLite and Postgres; on Postgres this
        # becomes a `labels @> :filter` GIN query once volumes call for it.
        rows = [r for r in rows
                if all((r.labels or {}).get(k) == v for k, v in label_filters.items())][:limit]
    return rows


def active_silences() -> list[Silence]:
    t = now()
    return SessionLocal.scalars(select(Silence).where(
        Silence.starts_at <= t, Silence.ends_at > t)).all()


def silenced_by(occ: Occurrence, silences: list[Silence]) -> list[str]:
    return [s.id for s in silences if matchers.matches(s.matchers, occ.labels or {})]


def history(signature: str, exclude_id: str | None = None, limit: int = 20) -> dict:
    """What happened the other times this alert fired — the knowledge-base read."""
    db = SessionLocal
    base = select(Occurrence).where(Occurrence.signature == signature)
    if exclude_id:
        base = base.where(Occurrence.id != exclude_id)
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    since = now() - dt.timedelta(days=90)
    last_90d = db.scalar(select(func.count()).select_from(
        base.where(Occurrence.started_at >= since).subquery())) or 0
    recent = db.scalars(base.order_by(Occurrence.started_at.desc()).limit(limit)).all()
    fixes = [o for o in recent if o.resolution_note or o.resolution_category]
    return {
        "signature": signature,
        "total": total,
        "last_90_days": last_90d,
        "last_started_at": recent[0].started_at if recent else None,
        "recent": recent,
        "recent_fixes": fixes[:5],
    }


# --- silences ------------------------------------------------------------------------

def create_silence(raw_matchers, starts_at: dt.datetime | None, ends_at: dt.datetime,
                   comment: str, actor: str, team_id: str | None = None,
                   max_duration: dt.timedelta | None = None) -> Silence:
    try:
        clean = matchers.validate(raw_matchers)
    except matchers.MatcherError as e:
        raise AlertError(str(e)) from None
    # A reason is welcome, never required: at 3 am the person who knows it can wait wants to
    # press Silence and go back to sleep. Who made it and when are always recorded. (MCP still
    # requires one — an assistant must say why it muted something.)
    comment = (comment or "").strip()
    t = now()
    starts = aware(starts_at) or t
    ends = aware(ends_at)
    if ends is None or ends <= starts:
        raise AlertError("ends_at must be after starts_at")
    if ends <= t:
        raise AlertError("ends_at is in the past")
    cap = max_duration or dt.timedelta(days=cfg.MAX_SILENCE_DAYS)
    if ends - starts > cap:
        raise AlertError(f"a silence may last at most {cap}")
    s = Silence(matchers=clean, starts_at=starts, ends_at=ends, comment=comment[:2000],
                created_by=actor, team_id=team_id)
    SessionLocal.add(s)
    SessionLocal.commit()
    return s


def silence_alert(occurrence_id: str, actor: str, minutes: int,
                  comment: str | None = None) -> Silence:
    """One tap: silence THIS alert — its signature labels, i.e. the same fault on any pod —
    for `minutes`. Works on a RESOLVED alert too: a flapping alert is resolved most of the
    time, and silencing it then is what stops the next flap paging anyone."""
    occ = get(occurrence_id)
    labels = {k: v for k, v in (occ.signature_labels or {}).items() if v}
    if not labels:
        raise AlertError("this alert has no labels to silence on — make a silence on the Silences page")
    if not 1 <= int(minutes) <= cfg.MAX_SILENCE_DAYS * 24 * 60:
        raise AlertError(f"silence for 1 minute to {cfg.MAX_SILENCE_DAYS} days")
    m = [{"name": k, "value": v, "isRegex": False, "isEqual": True}
         for k, v in sorted(labels.items(), key=lambda kv: (kv[0] != "alertname", kv[0]))]
    t = now()
    s = create_silence(m, t, t + dt.timedelta(minutes=int(minutes)), comment or "", actor,
                       team_id=occ.team_id)
    span = f"{int(minutes) // 60} h" if int(minutes) % 60 == 0 else f"{int(minutes)} min"
    _event(occ, "silenced", actor, f"for {span}" + (f": {comment.strip()}" if (comment or "").strip() else ""))
    SessionLocal.commit()
    return s


def expire_silence(silence_id: str, actor: str) -> Silence:
    s = SessionLocal.get(Silence, silence_id)
    if s is None:
        raise LookupError("silence not found")
    t = now()
    if aware(s.ends_at) > t:
        s.ends_at = t
        s.expired_by = actor
        SessionLocal.commit()
    return s


def preview_silence(raw_matchers) -> list[Occurrence]:
    """The OPEN alerts a silence would mute right now — shown before anyone commits to it."""
    try:
        clean = matchers.validate(raw_matchers)
    except matchers.MatcherError as e:
        raise AlertError(str(e)) from None
    rows = SessionLocal.scalars(select(Occurrence).where(Occurrence.resolved_at.is_(None))).all()
    return [o for o in rows if matchers.matches(clean, o.labels or {})]
