"""On-call schedules around the pure rules in core/oncall.py: loading rows into the shape
those rules take, the escalation target "whoever is on call", and handover notices."""
import datetime as dt
import logging

from sqlalchemy import select, update

from .audit import record
from .core import oncall
from .db import SessionLocal
from .models import Schedule, ScheduleOverride, User, aware, now

logger = logging.getLogger(__name__)


def as_dict(s: Schedule) -> dict:
    return {"timezone": s.timezone, "layers": s.layers or []}


def overrides_of(s: Schedule, start: dt.datetime | None = None,
                 end: dt.datetime | None = None) -> list[dict]:
    out = []
    for o in s.overrides:
        a, b = aware(o.starts_at), aware(o.ends_at)
        if (end is None or a < end) and (start is None or b > start):
            out.append({"id": o.id, "user_id": o.user_id, "starts_at": a, "ends_at": b,
                        "created_at": aware(o.created_at)})
    return out


def on_call_now(s: Schedule, at: dt.datetime | None = None) -> dict:
    at = at or now()
    return oncall.on_call(as_dict(s), overrides_of(s, at, at + dt.timedelta(seconds=1)), at)


def shifts(s: Schedule, start: dt.datetime, end: dt.datetime) -> list[dict]:
    return oncall.shifts(as_dict(s), overrides_of(s, start, end), start, end)


def framed_shifts(s: Schedule, start: dt.datetime, end: dt.datetime) -> list[dict]:
    """`shifts`, each segment also carrying its WHOLE extent (oncall.framed_shifts)."""
    d = as_dict(s)
    reach = oncall._reach(d)
    return oncall.framed_shifts(d, overrides_of(s, start - reach, end + reach), start, end)


def on_call_user(schedule_id: str) -> User | None:
    """For escalation: the person on call right now, or None (nobody, or disabled)."""
    s = SessionLocal.get(Schedule, schedule_id)
    if s is None:
        return None
    uid = on_call_now(s)["user_id"]
    u = SessionLocal.get(User, uid) if uid else None
    return u if u and not u.disabled else None


def next_change(s: Schedule, at: dt.datetime) -> dt.datetime | None:
    """When the current person's shift ends (within the next 4 weeks)."""
    segs = shifts(s, at, at + dt.timedelta(days=28))
    return segs[0]["end"] if segs and len(segs) > 1 else None


def check_handovers() -> None:
    """Called from the scheduler: email whoever just came on call. Detected by comparing with
    `last_on_call`, so a missed tick still sends exactly one notice."""
    from . import delivery
    from . import config as cfg

    db = SessionLocal
    t = now()
    for s in db.scalars(select(Schedule)).all():
        uid = on_call_now(s, t)["user_id"]
        if uid == s.last_on_call:
            continue
        previous = s.last_on_call
        # Compare-and-set, not a blind write: only the transaction that MOVES last_on_call
        # sends the notice, so a brief second leader (or a retried tick) cannot mail twice.
        moved = db.execute(update(Schedule).where(
            Schedule.id == s.id,
            Schedule.last_on_call.is_(None) if previous is None else Schedule.last_on_call == previous,
        ).values(last_on_call=uid).execution_options(synchronize_session=False)).rowcount
        db.commit()
        db.refresh(s)
        if not moved:
            continue
        if not (s.handover_notify and uid and previous is not None):
            continue  # the first observation is not a handover
        u = db.get(User, uid)
        if u is None or u.disabled:
            continue
        until = next_change(s, t)
        url = f"{cfg.PUBLIC_URL}/schedules/{s.id}" if cfg.PUBLIC_URL else ""
        try:
            delivery.send_email(
                u.email, f"[ON CALL] You are now on call for {s.name}",
                f"You are on call for {s.name}"
                + (f" until {until.astimezone(oncall.ZoneInfo(s.timezone)):%a %d %b %H:%M} ({s.timezone})" if until else "")
                + ".\n" + (f"\nSchedule: {url}\n" if url else ""))
            record("shorthop", "schedule.handover", target=s.name, detail=u.email)
        except delivery.DeliveryError as e:
            record("shorthop", "schedule.handover", target=s.name, status="error", detail=str(e))
            logger.warning("handover notice for %s failed: %s", s.name, e)
