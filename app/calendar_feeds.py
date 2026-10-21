"""On-call shifts as iCalendar: the feed a calendar app subscribes to, and the one-off
download. The shifts come from the SAME computation as the schedule page and /me/shifts
(schedules.shifts), so the calendar can never disagree with Shorthop about who is on call."""
import datetime as dt

from sqlalchemy import select

from . import config as cfg
from . import schedules as svc
from .core import ical, oncall
from .db import SessionLocal
from .models import Schedule, User, now

PAST_DAYS = 14
AHEAD_DAYS = 90


def _name(u: User | None) -> str:
    return (u.name or u.email) if u else "nobody"


def _users(ids: set) -> dict:
    ids.discard(None)
    if not ids:
        return {}
    return {u.id: u for u in SessionLocal.scalars(select(User).where(User.id.in_(ids)))}


def _url(s: Schedule) -> str:
    return f"{cfg.PUBLIC_URL}/schedules/{s.id}" if cfg.PUBLIC_URL else ""


def window(at: dt.datetime | None = None) -> tuple[dt.datetime, dt.datetime]:
    t = (at or now()).replace(minute=0, second=0, microsecond=0)
    return t - dt.timedelta(days=PAST_DAYS), t + dt.timedelta(days=AHEAD_DAYS)


def mine(user: User, key: str) -> str:
    """Every shift this person is on call for, across every schedule."""
    start, end = window()
    events = []
    for s in SessionLocal.scalars(select(Schedule).order_by(Schedule.name)).all():
        for seg in svc.shifts(s, start, end):
            if seg["user_id"] != user.id:
                continue
            over = seg["source"] == "override"
            events.append({
                "uid": ical.uid(key, s.id, user.id, seg["start"].isoformat(), seg["end"].isoformat()),
                "start": seg["start"], "end": seg["end"],
                "summary": f"On call: {s.name}" + (" (override)" if over else ""),
                "description": f"You are on call for {s.name}" + (" — covering (override)" if over else "")
                               + f". Schedule time zone: {s.timezone}.",
                "url": _url(s),
            })
    return ical.calendar(f"On call — {_name(user)}", sorted(events, key=lambda e: e["start"]), now(),
                         "Your on-call shifts from Shorthop.")


def schedule(s: Schedule, key: str) -> str:
    """Everyone on this schedule — and the gaps, which are the thing an overview must show."""
    start, end = window()
    begins = oncall.starts_at(svc.as_dict(s))
    # Before the first rotation starts there is no schedule yet — not "nobody on call".
    segs = [x for x in svc.shifts(s, start, end)
            if x["user_id"] or begins is None or x["end"] > begins]
    if begins is not None:
        segs = [{**x, "start": max(x["start"], begins)} if not x["user_id"] else x for x in segs]
    people = _users({x["user_id"] for x in segs})
    events = []
    for seg in segs:
        u = people.get(seg["user_id"])
        over = seg["source"] == "override"
        summary = (f"{_name(u)} on call" + (" (override)" if over else "")) if u else "⚠ Nobody on call"
        events.append({
            "uid": ical.uid(key, s.id, seg["user_id"] or "-", seg["start"].isoformat(), seg["end"].isoformat()),
            "start": seg["start"], "end": seg["end"], "summary": f"{s.name}: {summary}",
            "description": (f"{_name(u)} is on call for {s.name}" + (" (override)" if over else "") + "."
                            if u else f"Nobody is on call for {s.name} — pages to it reach nobody.")
                           + f" Schedule time zone: {s.timezone}.",
            "url": _url(s),
        })
    return ical.calendar(f"On call — {s.name}", events, now(), f"Who is on call for {s.name}, from Shorthop.")
