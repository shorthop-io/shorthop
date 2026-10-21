"""Incidents and escalation: from "an alert opened" to "these people are told".

Flow:
  ingest opens an occurrence → `attach()` routes it (first matching route) and puts it in the
  open incident for its group key, creating one if needed → a new incident gets
  `next_step_at = now` → the dispatch loop's `run_due()` executes the due step: it resolves
  the step's targets to destinations and writes `pending` Notification rows in the SAME
  transaction that advances the incident → `delivery.deliver_pending()` sends them.

Rules worth knowing:
* Acknowledging stops escalation — for the incident AND all its open alerts. Acknowledging
  one alert acknowledges its incident: the page is about the incident, and a person who
  acknowledged any part of it is on it.
* A step whose open alerts are ALL silenced or inhibited is HELD (re-checked every
  SUPPRESSED_RECHECK_SECONDS), not skipped — when the silence ends, paging resumes where it was.
* An incident resolves when its last open alert resolves; if the route asked for it, every
  destination that was told about it is told it is over.
"""
import datetime as dt
import logging

from prometheus_client import Counter
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from . import config as cfg
from . import locks
from . import settings as st
from .core import contact, matchers, routing
from .core import levels as lv
from .db import SessionLocal
from .models import (
    Channel, Device, EscalationPolicy, Incident, Inhibition, Notification, Occurrence, Route,
    Silence, Team, TeamMember, TimelineEvent, User, aware, now,
)

logger = logging.getLogger(__name__)

STEPS_RUN = Counter("shorthop_escalation_steps_total", "Escalation steps run, by outcome.",
                    ["outcome"])
for _o in ("notified", "no_targets", "held_suppressed", "exhausted"):
    STEPS_RUN.labels(_o)

SEVERITY_RANK = {"critical": 5, "page": 5, "error": 4, "high": 4, "warning": 3, "warn": 3,
                 "medium": 3, "info": 2, "low": 1}


def _rank(sev: str) -> int:
    return SEVERITY_RANK.get((sev or "").lower(), 0)


def _route_views(db) -> tuple[list[routing.RouteView], dict[str, Route]]:
    rows = db.scalars(select(Route)).all()
    views = [routing.RouteView(r.id, r.name, r.position, r.matchers or [], r.integration_ids,
                               r.group_by or [], r.enabled) for r in rows]
    return views, {r.id: r for r in rows}


def _incident_event(inc: Incident, kind: str, actor: str = "", detail: str = "") -> None:
    """Incident-level events are written onto each open alert's timeline, so the alert page
    tells the whole story without a second timeline to look at."""
    for occ in inc.alerts:
        if occ.resolved_at is None:
            occ.events.append(TimelineEvent(kind=kind, actor=actor, detail=detail))


def _slack(db, inc: Incident, reply: str | None = None) -> None:
    """The incident changed: its Slack messages are redrawn by the dispatch loop, and `reply`
    goes into their threads. In the caller's transaction, so a rolled-back action leaves Slack
    alone. Whichever door the change came through (web, phone, MCP, Slack), Slack shows it."""
    from . import slack
    slack.touch(db, inc.id, reply)


def _derive_level(inc: Incident) -> None:
    """Follow the alerts — unless a person set the level (core/levels.py)."""
    if inc.level_manual:
        return
    levels = st.incident_levels()
    labels = [(o.labels or {}).get(lv.LEVEL_LABEL) for o in inc.alerts if o.resolved_at is None]
    inc.level = lv.derive(levels, labels, inc.severity)


# --- routing new alerts into incidents --------------------------------------------------

def attach(new_alerts: list[Occurrence]) -> None:
    if not new_alerts:
        return
    db = SessionLocal
    views, by_id = _route_views(db)
    if not views:
        return
    t = now()
    for occ in new_alerts:
        view = routing.pick(views, occ.labels or {}, occ.integration_id)
        if view is None:
            continue
        route = by_id[view.id]
        key = routing.group_key(view, occ.labels or {})
        inc = db.scalar(select(Incident).where(Incident.group_key == key,
                                               Incident.resolved_at.is_(None)))
        fresh = inc is None
        if inc is None:
            inc = _open_incident(db, route, view, key, occ, t)
        elif _rank(occ.severity) > _rank(inc.severity) and not inc.level_manual:
            # A level a person set pins the severity too: their judgement beats a label.
            inc.severity = occ.severity
        occ.incident_id = inc.id
        if occ not in inc.alerts:
            inc.alerts.append(occ)
        _derive_level(inc)
        if not fresh:
            _slack(db, inc)
        occ.events.append(TimelineEvent(kind="routed", actor="shorthop",
                                        detail=f"route {route.name} → incident #{inc.number}"))
        if inc.status == "acknowledged" and occ.status == "firing":
            # It joined an incident somebody is already handling.
            occ.status, occ.acked_at, occ.acked_by = "acknowledged", inc.acked_at, inc.acked_by
    db.commit()


def _open_incident(db, route: Route, view: routing.RouteView, key: str, occ: Occurrence,
                   t: dt.datetime) -> Incident:
    labels = routing.group_labels(view, occ.labels or {})
    extra = ", ".join(f"{k}={v}" for k, v in labels.items() if k != "alertname" and v)
    # Serialise numbering across replicas (and request threads): max()+1 read by two
    # transactions at once gives two incidents the same #. Held until the commit.
    locks.xact_lock(db, locks.KEY_INCIDENT_NUMBER)
    number = (db.scalar(select(func.max(Incident.number))) or 0) + 1
    inc = Incident(
        number=number, route_id=route.id, team_id=route.team_id,
        escalation_policy_id=route.escalation_policy_id, group_key=key, group_labels=labels,
        title=(occ.alertname + (f" ({extra})" if extra else ""))[:300], severity=occ.severity,
        status="triggered", opened_at=t, notify_resolved=route.notify_resolved,
        next_step_at=t if route.escalation_policy_id else None,
    )
    db.add(inc)
    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError:
        # Another request thread opened the same group a moment ago — use that one.
        existing = db.scalar(select(Incident).where(Incident.group_key == key,
                                                    Incident.resolved_at.is_(None)))
        if existing is not None:
            return existing
        raise
    return inc


# --- acknowledging and resolving ---------------------------------------------------------

def get(incident_id: str) -> Incident:
    inc = SessionLocal.get(Incident, incident_id)
    if inc is None:
        raise LookupError("incident not found")
    return inc


def acknowledge(incident_id: str, actor: str) -> Incident:
    from .alerts import AlertError

    inc = get(incident_id)
    if inc.resolved_at is not None:
        raise AlertError("this incident is already resolved")
    if inc.status != "acknowledged":
        t = now()
        inc.status, inc.acked_at, inc.acked_by, inc.next_step_at = "acknowledged", t, actor, None
        for occ in inc.alerts:
            if occ.resolved_at is None and occ.status == "firing":
                occ.status, occ.acked_at, occ.acked_by = "acknowledged", t, actor
                occ.events.append(TimelineEvent(kind="acknowledged", actor=actor,
                                                detail=f"incident #{inc.number}"))
        _slack(SessionLocal, inc, f":white_check_mark: Acknowledged by {actor}")
        SessionLocal.commit()
    return inc


def reassign(incident_id: str, actor: str, team_id: str, policy_id: str | None,
             note: str | None = None, page: bool = True) -> Incident:
    """Move an incident to another team — it was routed wrong, or it turned out to be theirs.

    * The incident and its alerts take the new team; new alerts in the same group keep
      joining it, so it stays with the new team.
    * `policy_id` decides who is paged. None = the team's own policy when it has exactly one
      (the caller is told to choose when it has several or none — a silent "nobody" is worse).
    * Pages still QUEUED for the old policy are cancelled (status suppressed, with the reason):
      the old team should not get a page about something that is no longer theirs.
    * page=True (and always when still triggered): escalation restarts at step 1 of the new
      policy, now. An acknowledgement by the old team is cleared — it must not stop the new
      team being paged. page=False keeps an acknowledged incident acknowledged (the person on
      it is just moving ownership).
    """
    from .alerts import AlertError

    db = SessionLocal
    inc = get(incident_id)
    if inc.resolved_at is not None:
        raise AlertError("this incident is already resolved")
    team = db.get(Team, team_id)
    if team is None:
        raise AlertError("that team does not exist")
    if policy_id:
        policy = db.get(EscalationPolicy, policy_id)
        if policy is None:
            raise AlertError("that escalation policy does not exist")
    else:
        own = db.scalars(select(EscalationPolicy).where(EscalationPolicy.team_id == team.id)
                         .order_by(EscalationPolicy.name)).all()
        if len(own) != 1:
            raise AlertError(
                f"team {team.name} has {'no' if not own else len(own)} escalation "
                f"polic{'y' if len(own) == 1 else 'ies'} — choose which one pages them")
        policy = own[0]
    if team.id == inc.team_id and policy.id == inc.escalation_policy_id:
        raise AlertError("the incident is already with that team and policy")

    old_team = db.get(Team, inc.team_id) if inc.team_id else None
    old_policy = db.get(EscalationPolicy, inc.escalation_policy_id) if inc.escalation_policy_id else None
    t = now()
    from_txt = f"{old_team.name if old_team else 'no team'} ({old_policy.name if old_policy else 'no policy'})"
    to_txt = f"{team.name} ({policy.name})"

    cancelled = 0
    for n in db.scalars(select(Notification).where(Notification.incident_id == inc.id,
                                                   Notification.status == "pending")).all():
        if n.event in ("triggered", "escalated"):
            n.status, n.error = "suppressed", f"reassigned to {team.name} before it was sent"
            cancelled += 1

    inc.team_id, inc.escalation_policy_id = team.id, policy.id
    inc.reassigned_at, inc.reassigned_by = t, actor
    inc.reassign_note = f"from {from_txt}" + (f": {note.strip()}" if note and note.strip() else "")
    for occ in inc.alerts:
        occ.team_id = team.id

    repage = page or inc.status == "triggered"
    if repage:
        inc.status, inc.acked_at, inc.acked_by = "triggered", None, None
        inc.next_step, inc.pass_no, inc.next_step_at = 0, 0, t
        for occ in inc.alerts:
            if occ.resolved_at is None and occ.status == "acknowledged":
                occ.status, occ.acked_at, occ.acked_by = "firing", None, None
    detail = f"{from_txt} → {to_txt}" + (f" — {note.strip()}" if note and note.strip() else "")
    detail += "; paging the new team now" if repage else "; still acknowledged, nobody paged"
    if cancelled:
        detail += f"; {cancelled} queued page(s) to the old team cancelled"
    _incident_event(inc, "reassigned", actor, detail)
    _slack(db, inc, f":arrow_right: Moved by {actor}: {detail}")
    db.commit()
    return inc


def on_alert_acknowledged(occ: Occurrence, actor: str) -> None:
    if occ.incident_id:
        inc = SessionLocal.get(Incident, occ.incident_id)
        if inc is not None and inc.status == "triggered":
            acknowledge(inc.id, actor)


def resolve(incident_id: str, actor: str, category: str | None = None,
            note: str | None = None) -> Incident:
    """Resolve every open alert in the incident by hand (with the same fix note). On an
    incident that is already resolved — usually by its sender — this records WHAT FIXED IT on
    its alerts, which is the part only a person can add."""
    from . import alerts as alert_ops

    inc = get(incident_id)
    open_alerts = [o for o in inc.alerts if o.resolved_at is None]
    for occ in open_alerts or (list(inc.alerts) if (category or note) else []):
        alert_ops.resolve(occ.id, actor, category=category, note=note)
    inc = get(incident_id)
    if category or (note and note.strip()):
        what = " — ".join(x for x in [(category or "").replace("_", " "), (note or "").strip()] if x)
        _slack(SessionLocal, inc, f":wrench: What fixed it ({actor}): {what}")
        SessionLocal.commit()
    return inc


def set_level(incident_id: str, actor: str, level: str) -> Incident:
    """A person sets the level (P1 …). The severity follows the level's mapping, and BOTH stop
    following the alerts from now on (`level_manual`).

    The new severity applies to every page from here on — the next escalation step and the
    contact rules it reads ("push me for critical"). Pages already queued are not rewritten;
    raise it and press Escalate now to page the next step at once."""
    from .alerts import AlertError

    inc = get(incident_id)
    if inc.resolved_at is not None:
        raise AlertError("this incident is already resolved")
    levels = st.incident_levels()
    chosen = lv.find(levels, level)
    if chosen is None:
        raise AlertError(f"unknown level {level!r}; use one of: "
                         + ", ".join(x["name"] for x in levels))
    old = f"{inc.level or 'no level'} ({inc.severity or 'no severity'})"
    if inc.level == chosen["name"] and inc.severity == chosen["severity"] and inc.level_manual:
        return inc
    inc.level, inc.severity = chosen["name"], chosen["severity"]
    inc.level_manual, inc.level_set_by, inc.level_set_at = True, actor, now()
    detail = f"{old} → {chosen['name']} ({chosen['severity']})"
    _incident_event(inc, "level", actor, detail)
    _slack(SessionLocal, inc, f":label: Level set by {actor}: {detail}")
    SessionLocal.commit()
    return inc


def escalate_now(incident_id: str, actor: str) -> Incident:
    """Run the next escalation step now instead of after its delay. Only while paging
    (triggered): escalating an acknowledged incident means un-acknowledging it, which is a
    different decision — move it or add a responder instead."""
    from .alerts import AlertError

    inc = get(incident_id)
    if inc.resolved_at is not None:
        raise AlertError("this incident is already resolved")
    if inc.status != "triggered":
        raise AlertError("it is acknowledged, so nobody is being paged — add a responder "
                         "or move it to another team instead")
    if inc.next_step_at is None:
        raise AlertError("its escalation policy has no further steps — add a responder instead")
    inc.next_step_at = now()
    _incident_event(inc, "escalation", actor, f"escalate now: step {inc.next_step + 1}")
    SessionLocal.commit()
    return inc


def add_responder(incident_id: str, actor: str, user_id: str | None = None,
                  schedule_id: str | None = None, note: str | None = None) -> list[dict]:
    """Page one more person (or whoever is on call for a schedule) about this incident now,
    outside its escalation policy. Works on an acknowledged incident too — "I need Ann on
    this" is exactly when it is asked. Their own contact rules decide how they are reached.
    Returns the destinations paged."""
    from .alerts import AlertError

    db = SessionLocal
    inc = get(incident_id)
    if inc.resolved_at is not None:
        raise AlertError("this incident is already resolved")
    if bool(user_id) == bool(schedule_id):
        raise AlertError("name one person or one schedule")
    if user_id:
        u = db.get(User, user_id)
        if u is None or u.disabled:
            raise AlertError("that person does not exist or is disabled")
        target, who = {"type": "user", "id": u.id}, u.name or u.email
    else:
        from .models import Schedule
        sch = db.get(Schedule, schedule_id)
        if sch is None:
            raise AlertError("that schedule does not exist")
        target, who = {"type": "schedule", "id": sch.id}, f"on call for {sch.name}"
    dests = _destinations(db, [target], inc.severity)
    # A delayed contact rule ("email me after 5 min if I have not reacted") is about being
    # paged by the policy; a person ASKED for is reached by every rule at once.
    immediate = [d for d in dests if not d.get("delay_minutes")] or dests
    if not immediate:
        raise AlertError(f"{who} cannot be reached — no email, phone or Slack to page")
    t = now()
    for d in immediate:
        db.add(Notification(incident_id=inc.id, event="triggered", step=None, kind=d["kind"],
                            target=(d["target"] + f" (added by {actor})")[:300],
                            destination=d["destination"][:500], channel_id=d["channel_id"],
                            status="pending", next_attempt_at=t))
    text = f"added responder {who}" + (f": {note.strip()}" if note and note.strip() else "")
    _incident_event(inc, "responder", actor, text)
    _slack(db, inc, f":sos: {actor} {text}")
    db.commit()
    return immediate


def add_note(incident_id: str, actor: str, text: str) -> Incident:
    """A note on the incident: written onto every open alert's timeline (and the Slack
    threads). On a resolved incident it goes on its alerts all the same — the debrief happens
    after."""
    from .alerts import AlertError

    text = (text or "").strip()
    if not text:
        raise AlertError("note is empty")
    inc = get(incident_id)
    targets = [o for o in inc.alerts if o.resolved_at is None] or list(inc.alerts)
    for occ in targets:
        occ.events.append(TimelineEvent(kind="note", actor=actor, detail=text[:4000]))
    _slack(SessionLocal, inc, f":memo: {actor}: {text[:2900]}")
    SessionLocal.commit()
    return inc


SNOOZE_MAX = dt.timedelta(hours=24)


def snooze_matchers(inc: Incident) -> list[dict]:
    """The silence that covers THIS incident: its group labels (what made the alerts one
    incident), or — for a route that groups nothing — the alert name, when every alert shares
    one. Anything wider would silence other incidents too."""
    labels = {k: v for k, v in (inc.group_labels or {}).items() if v}
    if labels.get("alertname") == HELP_ALERTNAME:
        # A help request: THIS request only — the group labels would silence every future
        # request to the same team.
        req = next(((o.labels or {}).get("request") for o in inc.alerts if (o.labels or {}).get("request")), None)
        if not req:
            return []
        labels = {"alertname": HELP_ALERTNAME, "request": req}
    if not labels:
        names = {o.alertname for o in inc.alerts if o.resolved_at is None} or \
            {o.alertname for o in inc.alerts}
        if len(names) != 1:
            return []
        labels = {"alertname": names.pop()}
    return [{"name": k, "value": v, "isRegex": False, "isEqual": True}
            for k, v in sorted(labels.items())]


def snooze(incident_id: str, actor: str, minutes: int, comment: str | None = None):
    """Silence the incident's alerts for a while (≤ 24 h). Escalation HOLDS while every open
    alert is silenced and resumes where it was when the silence ends — the same silence the
    Silences page makes, so it can be seen and expired there."""
    from . import alerts as alert_ops
    from .alerts import AlertError

    inc = get(incident_id)
    if inc.resolved_at is not None:
        raise AlertError("this incident is already resolved")
    m = snooze_matchers(inc)
    if not m:
        raise AlertError("this incident's alerts share no labels to silence on — "
                         "make a silence on the Silences page")
    if not 1 <= int(minutes) <= SNOOZE_MAX.total_seconds() // 60:
        raise AlertError("snooze for 1 minute to 24 hours")
    t = now()
    why = (comment or "").strip() or f"snoozed incident #{inc.number}"
    s = alert_ops.create_silence(m, t, t + dt.timedelta(minutes=int(minutes)), why, actor,
                                 team_id=inc.team_id, max_duration=SNOOZE_MAX)
    span = f"{int(minutes) // 60} h" if int(minutes) % 60 == 0 else f"{int(minutes)} min"
    _incident_event(inc, "silenced", actor, f"snoozed for {span}: {why}")
    _slack(SessionLocal, inc, f":zzz: Snoozed for {span} by {actor}: {why}")
    SessionLocal.commit()
    return s


HELP_ALERTNAME = "HelpRequested"
# A help request stays open until a person resolves it — it must never time out quietly.
HELP_OPEN_DAYS = 30


def find_team(ref: str) -> Team | None:
    """A team by slug or name, case-insensitive (`techops`, `TechOps`, `Tech Ops`)."""
    r = (ref or "").strip().lstrip("@#").lower()
    if not r:
        return None
    for t in SessionLocal.scalars(select(Team)).all():
        if r in (t.slug.lower(), t.name.lower(), t.name.lower().replace(" ", "")):
            return t
    return None


def page_team(team_id: str, actor: str, message: str, level: str | None = None,
              policy_id: str | None = None, via: str = "") -> Incident:
    """A person asks a team for help: a new incident owned by that team, paged NOW by the
    team's escalation policy — no alert sender involved.

    It is an ordinary incident with one alert from the built-in `shorthop` integration
    (`alertname=HelpRequested, team=<slug>`), so ack / resolve / snooze / reassign, the fix
    note and the history all work as for any other. Each request is its own alert (a unique
    `request` label) — two people asking for help are two problems — but the label is left out
    of the signature, so the history reads "this team was asked for help N times before".

    Level: the one named, else the most urgent — asking a team for help means "page them",
    and saying P3 is how to make it a softer ask. The level counts as set by a person.
    Policy: the one given, else the team's own when it has exactly one (as for a move)."""
    import uuid

    from .alerts import AlertError
    from .core import identity
    from .selfmon import internal_integration

    db = SessionLocal
    message = (message or "").strip()
    if not message:
        raise AlertError("say what you need help with")
    team = db.get(Team, team_id)
    if team is None:
        raise AlertError("that team does not exist")
    if policy_id:
        policy = db.get(EscalationPolicy, policy_id)
        if policy is None:
            raise AlertError("that escalation policy does not exist")
    else:
        own = db.scalars(select(EscalationPolicy).where(EscalationPolicy.team_id == team.id)
                         .order_by(EscalationPolicy.name)).all()
        if not own:
            # None owned by the team: the one its routes already page it with, if just one.
            ids = {r.escalation_policy_id for r in db.scalars(select(Route).where(
                Route.team_id == team.id, Route.escalation_policy_id.is_not(None))).all()}
            own = [db.get(EscalationPolicy, i) for i in sorted(ids)]
        if not own:
            raise AlertError(f"team {team.name} has no escalation policy — give it one "
                             "(Escalation), so Shorthop knows who to page")
        if len(own) > 1:
            raise AlertError(f"team {team.name} is paged by {len(own)} escalation policies "
                             f"({', '.join(p.name for p in own)}) — choose which one")
        policy = own[0]
    levels = st.incident_levels()
    chosen = lv.find(levels, level) if level else levels[0]
    if chosen is None:
        raise AlertError(f"unknown level {level!r}; use one of: " + ", ".join(x["name"] for x in levels))

    internal = internal_integration()
    t = now()
    request = uuid.uuid4().hex[:12]
    labels = {"alertname": HELP_ALERTNAME, "team": team.slug, "severity": chosen["severity"],
              "level": chosen["name"], "request": request}
    sig, sig_labels = identity.signature(labels, ["request", "severity", "level"])
    occ = Occurrence(
        integration_id=internal.id, team_id=team.id,
        fingerprint=identity.fingerprint(internal.id, labels), signature=sig,
        signature_labels=sig_labels, alertname=HELP_ALERTNAME, severity=chosen["severity"],
        labels=labels, annotations={"summary": message[:2000], "requested_by": actor,
                                    **({"via": via} if via else {})},
        status="firing", started_at=t, last_seen_at=t,
        expires_at=t + dt.timedelta(days=HELP_OPEN_DAYS),
    )
    occ.events.append(TimelineEvent(kind="opened", actor=actor,
                                    detail=f"asked {team.name} for help" + (f" ({via})" if via else "")
                                    + f": {message[:2000]}"))
    db.add(occ)
    locks.xact_lock(db, locks.KEY_INCIDENT_NUMBER)
    number = (db.scalar(select(func.max(Incident.number))) or 0) + 1
    inc = Incident(
        number=number, route_id=None, team_id=team.id, escalation_policy_id=policy.id,
        group_key=identity.fingerprint("help", {"request": request}),
        group_labels={"alertname": HELP_ALERTNAME, "team": team.slug},
        title=f"{team.name}: {message}"[:300], severity=chosen["severity"], status="triggered",
        opened_at=t, notify_resolved=True, next_step_at=t,
        level=chosen["name"], level_manual=True, level_set_by=actor, level_set_at=t,
    )
    db.add(inc)
    db.flush()
    occ.incident_id = inc.id
    inc.alerts.append(occ)
    db.commit()
    return inc


def after_alerts_resolved(occs: list[Occurrence], actor: str = "") -> None:
    """Close each touched incident whose last open alert just resolved."""
    db = SessionLocal
    ids = {o.incident_id for o in occs if o.incident_id}
    for inc_id in ids:
        inc = db.get(Incident, inc_id)
        if inc is None or inc.resolved_at is not None:
            continue
        if any(o.resolved_at is None for o in inc.alerts):
            continue
        inc.status, inc.resolved_at, inc.resolved_by, inc.next_step_at = \
            "resolved", now(), actor or None, None
        _slack(db, inc, ":large_green_circle: Resolved" + (f" by {actor}" if actor else " — every alert cleared"))
        if inc.notify_resolved:
            _enqueue_resolved(db, inc)
    db.commit()


# --- escalation ----------------------------------------------------------------------------

def _destinations(db, targets: list[dict], severity: str = "") -> list[dict]:
    """Resolve step targets to concrete destinations, de-duplicated. A target that resolves to
    nothing (deleted user, empty team, disabled channel) is reported, not silently dropped.
    `delay_minutes` > 0 = a page from a person's delayed contact rule."""
    out, seen = [], set()

    def add(kind, target, destination, channel_id=None, delay=0):
        k = (kind, destination, channel_id, delay)
        if destination and k not in seen:
            seen.add(k)
            out.append({"kind": kind, "target": target, "destination": destination,
                        "channel_id": channel_id, "delay_minutes": delay})

    def person(u, why=""):
        """A person is paged by THEIR rules (My account → How I'm paged): each rule that
        admits this severity is a page, by email or on every paired phone, after its delay.
        No rules = email + every phone at once."""
        devices = None
        for rule in contact.applicable(u.contact_rules, severity):
            delay = rule["delay_minutes"]
            later = f" (after {delay} min)" if delay else ""
            if rule["method"] == "email":
                add("email", f"user {u.email}{why}{later}", u.email, None, delay)
            elif rule["method"] == "slack":
                # The DM conversation is found at send time (by email the first time).
                add("slack", f"user {u.email}{why} — Slack{later}", f"user:{u.id}", None, delay)
            else:
                if devices is None:
                    devices = db.scalars(select(Device).where(
                        Device.user_id == u.id, Device.revoked.is_(False),
                        Device.push_token.is_not(None))).all()
                for dev in devices:
                    add("push", f"user {u.email}{why} — {dev.name}{later}", dev.id, None, delay)

    for t in targets or []:
        typ, tid = t.get("type"), t.get("id")
        if typ == "user":
            u = db.get(User, tid)
            if u is not None and not u.disabled:
                person(u)
        elif typ == "team":
            for m in db.scalars(select(TeamMember).where(TeamMember.team_id == tid)).all():
                if m.user and not m.user.disabled:
                    person(m.user, " (team)")
        elif typ == "schedule":
            from .schedules import on_call_user
            u = on_call_user(tid)
            if u is not None:
                person(u, " (on call)")
        elif typ == "channel":
            ch = db.get(Channel, tid)
            if ch is None or not ch.enabled:
                continue
            if ch.kind == "email":
                for addr in (ch.config or {}).get("to", []):
                    add("email", f"channel {ch.name}", addr, ch.id)
            elif ch.kind == "webhook":
                # The URL is often the credential (Slack, Telegram) — the log shows the origin.
                from .delivery import redact_url
                add("webhook", f"channel {ch.name}",
                    redact_url((ch.config or {}).get("url", "")), ch.id)
            elif ch.kind == "slack":
                cfg_ = ch.config or {}
                add("slack", f"channel {ch.name}",
                    "#" + (cfg_.get("channel_name") or cfg_.get("channel_id") or "?"), ch.id)
    return out


def _suppressed(db, inc: Incident) -> str | None:
    """Why the incident should not page right now, or None. Every open alert must be
    silenced or inhibited — one live alert is enough to page."""
    open_alerts = [o for o in inc.alerts if o.resolved_at is None]
    if not open_alerts:
        return "no open alerts"
    t = now()
    silences = db.scalars(select(Silence).where(Silence.starts_at <= t, Silence.ends_at > t)).all()
    rules = [{"source_matchers": r.source_matchers, "target_matchers": r.target_matchers,
              "equal": r.equal or []}
             for r in db.scalars(select(Inhibition).where(Inhibition.enabled.is_(True))).all()]
    firing = [o.labels or {} for o in db.scalars(select(Occurrence).where(
        Occurrence.resolved_at.is_(None))).all()] if rules else []
    reasons = []
    for o in open_alerts:
        labels = o.labels or {}
        if any(matchers.matches(s.matchers, labels) for s in silences):
            reasons.append("silenced")
        elif rules and routing.inhibited(labels, firing, rules):
            reasons.append("inhibited")
        else:
            return None
    return " and ".join(sorted(set(reasons)))


def _claim_due(db, t: dt.datetime, skip: set[str]) -> Incident | None:
    """Lock ONE due incident for this transaction, skipping any another replica holds.

    ⚠️ The due conditions are re-evaluated under the lock, never trusted from an earlier
    read: the other replica may have run the step and committed a moment ago, and running it
    again is a duplicate page. `SKIP LOCKED` makes two replicas share the queue instead of
    queueing behind each other. On SQLite `with_for_update` is a no-op (one process)."""
    return db.scalar(select(Incident).where(
        Incident.status == "triggered", Incident.next_step_at.is_not(None),
        Incident.next_step_at <= t, Incident.id.not_in(skip or {""}))
        .order_by(Incident.next_step_at).limit(1).with_for_update(skip_locked=True))


def run_due(limit: int = 50) -> int:
    """Run every escalation step that is due, one claimed incident per transaction.
    Safe with several replicas. Returns how many ran."""
    db = SessionLocal
    t = now()
    ran, failed = 0, set()
    while ran + len(failed) < limit:
        inc = _claim_due(db, t, failed)  # a failed step stays due; retried next tick
        if inc is None:
            db.rollback()
            break
        try:
            _run_step(db, inc, t)
            db.commit()
            ran += 1
        except Exception:  # noqa: BLE001 — one broken incident must not stop the others
            db.rollback()
            failed.add(inc.id)
            logger.exception("escalation step failed for incident %s", inc.id)
    return ran


def _run_step(db, inc: Incident, t: dt.datetime) -> None:
    policy = db.get(EscalationPolicy, inc.escalation_policy_id) if inc.escalation_policy_id else None
    steps = (policy.steps if policy else None) or []
    if not steps:
        inc.next_step_at = None
        STEPS_RUN.labels("no_targets").inc()
        return

    why = _suppressed(db, inc)
    if why:
        inc.next_step_at = t + dt.timedelta(seconds=cfg.SUPPRESSED_RECHECK_SECONDS)
        STEPS_RUN.labels("held_suppressed").inc()
        return

    idx = inc.next_step
    step = steps[idx]
    dests = _destinations(db, step.get("targets", []), inc.severity)
    event = "triggered" if (idx == 0 and inc.pass_no == 0) else "escalated"
    for d in dests:
        delay = d.get("delay_minutes", 0)
        db.add(Notification(incident_id=inc.id, event=event, step=idx, kind=d["kind"],
                            target=d["target"][:300], destination=d["destination"][:500],
                            channel_id=d["channel_id"], status="pending",
                            next_attempt_at=t + dt.timedelta(minutes=delay),
                            until_ack=delay > 0))
    _incident_event(inc, "notified" if dests else "escalation", "shorthop",
                    (f"step {idx + 1}: " + ", ".join(d["target"] for d in dests)) if dests
                    else f"step {idx + 1} has no reachable targets")
    if event == "escalated":
        who = ", ".join(sorted({d["target"].split(" (")[0].split(" — ")[0] for d in dests}))
        _slack(db, inc, f":rotating_light: Escalated — step {idx + 1}: {who or 'nobody reachable'}")
    STEPS_RUN.labels("notified" if dests else "no_targets").inc()

    # Advance: next step after its delay, or start the next pass, or stop.
    if idx + 1 < len(steps):
        inc.next_step = idx + 1
        inc.next_step_at = t + dt.timedelta(minutes=float(steps[idx + 1].get("delay_minutes", 0)))
    elif inc.pass_no < (policy.repeat or 0):
        inc.pass_no += 1
        inc.next_step = 0
        inc.next_step_at = t + dt.timedelta(minutes=float(steps[0].get("delay_minutes", 0)) or 5)
    else:
        inc.next_step_at = None
        STEPS_RUN.labels("exhausted").inc()


def _enqueue_resolved(db, inc: Incident) -> None:
    """Tell everyone who was told about it that it is over (each destination once)."""
    told = db.scalars(select(Notification).where(
        Notification.incident_id == inc.id, Notification.event.in_(("triggered", "escalated")),
        Notification.status.in_(("pending", "sent")))).all()
    seen = set()
    for n in told:
        if n.until_ack and n.status == "pending":
            continue  # a delayed page that never went out: nothing to take back
        k = (n.kind, n.destination, n.channel_id)
        if k in seen:
            continue
        seen.add(k)
        db.add(Notification(incident_id=inc.id, event="resolved", step=None, kind=n.kind,
                            target=n.target, destination=n.destination, channel_id=n.channel_id,
                            status="pending", next_attempt_at=now()))


def explain_routing(labels: dict[str, str], integration_id: str) -> dict:
    """For the routing tester: every route in order, whether it matched and why."""
    views, by_id = _route_views(SessionLocal)
    rows, chosen = [], None
    for v in sorted(views, key=lambda r: (r.position, r.name)):
        ok, why = routing.explain(v, labels, integration_id)
        if ok and chosen is None:
            chosen = v.id
        rows.append({"route_id": v.id, "name": v.name, "matched": ok, "why": why,
                     "chosen": ok and chosen == v.id})
    return {"chosen_route_id": chosen,
            "group_labels": routing.group_labels(next(v for v in views if v.id == chosen), labels)
            if chosen else {},
            "routes": rows}


def ages(inc: Incident) -> dict:
    return {"open_alerts": sum(1 for o in inc.alerts if o.resolved_at is None),
            "alerts": len(inc.alerts),
            "next_step_in_s": max(0, int((aware(inc.next_step_at) - now()).total_seconds()))
            if inc.next_step_at else None}
