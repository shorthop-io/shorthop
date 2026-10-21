"""Insight queries: load a bounded window of history and hand it to core.insight.

Reads are capped (MAX_EPISODES) so a report over a busy quarter cannot load a million rows
into one process; when the cap bites the result says `truncated: true` rather than quietly
describing a sample as the whole.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select

from .core import insight as core
from .db import SessionLocal
from .models import Incident, Notification, Occurrence, Team, aware, now

MAX_EPISODES = 50_000


def _episodes(since: dt.datetime) -> tuple[list[core.Episode], bool]:
    rows = SessionLocal.scalars(
        select(Occurrence).where(Occurrence.started_at >= since)
        .order_by(Occurrence.started_at.desc()).limit(MAX_EPISODES + 1)).all()
    truncated = len(rows) > MAX_EPISODES
    return [core.Episode(
        signature=o.signature, fingerprint=o.fingerprint, alertname=o.alertname,
        severity=o.severity, started_at=aware(o.started_at),
        resolved_at=aware(o.resolved_at) if o.resolved_at else None,
        acked_at=aware(o.acked_at) if o.acked_at else None,
        resolved_how=o.resolved_how, resolution_category=o.resolution_category,
        team_id=o.team_id, labels=o.signature_labels or {},
    ) for o in rows[:MAX_EPISODES]], truncated


def _incidents(since: dt.datetime) -> list[core.IncidentTimes]:
    rows = SessionLocal.execute(
        select(Incident.opened_at, Incident.acked_at, Incident.resolved_at, Incident.team_id)
        .where(Incident.opened_at >= since)).all()
    return [core.IncidentTimes(aware(o), aware(a) if a else None, aware(r) if r else None, t)
            for o, a, r, t in rows]


def _team_names() -> dict[str, str]:
    return dict(SessionLocal.execute(select(Team.id, Team.name)).all())


def report(days: int) -> dict:
    t = now()
    since = t - dt.timedelta(days=days)
    eps, truncated = _episodes(since)
    incs = _incidents(since)
    names = _team_names()
    pages = dict(SessionLocal.execute(
        select(Notification.status, func.count()).where(Notification.created_at >= since)
        .group_by(Notification.status)).all())
    by_team = [{"team_id": k, "team": names.get(k) if k else None, **v}
               for k, v in core.response_by_team(incs).items()]
    by_team.sort(key=lambda r: (-r["incidents"], r["team"] or "~"))
    noisy = core.noise(eps, t)
    for r in noisy:
        r["team"] = names.get(r["team_id"]) if r["team_id"] else None
        r["last_started_at"] = r["last_started_at"].isoformat()
    return {
        "days": days, "since": since.isoformat(), "truncated": truncated,
        "episodes": len(eps),
        "response": core.response(incs),
        "response_by_team": by_team,
        "notifications": {"sent": pages.get("sent", 0), "failed": pages.get("failed", 0),
                          "pending": pages.get("pending", 0),
                          "suppressed": pages.get("suppressed", 0)},
        "noise": noisy[:100],
        "noise_total": len(noisy),
        "correlations": core.correlations(eps),
        "thresholds": {"flap_gap_s": core.FLAP_GAP.total_seconds(),
                       "co_window_s": core.CO_WINDOW.total_seconds(),
                       "min_episodes_for_flag": core.MIN_EPISODES_FOR_FLAG},
    }


def related(signature: str, days: int) -> dict:
    """What fires together with ONE alert — the read an alert page and MCP use."""
    since = now() - dt.timedelta(days=days)
    eps, truncated = _episodes(since)
    pairs = core.correlations(eps, signature=signature, limit=10)
    return {"signature": signature, "days": days, "truncated": truncated,
            "related": [{"signature": p["b"], "alertname": p["b_alertname"],
                         "episodes": p["b_episodes"], "together": p["together"],
                         "ratio": p["ratio"]} for p in pairs]}
