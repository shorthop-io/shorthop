"""Insight — what the history says about the alerts themselves. Pure functions over plain
records, so every rule here is testable without a database.

Three questions, each one a reason somebody stops trusting their pager:

- **Noise**: which alerts fire most, which FLAP (resolve and come straight back), and which
  nobody ever acts on (they resolve by themselves, unacknowledged). An alert in that last
  group is either a ticket wearing a pager's clothes or a threshold that is wrong.
- **Response**: how long until somebody acknowledged (MTTA) and until it was over (MTTR),
  as percentiles — a mean is dragged by the one incident left open over a weekend.
- **Correlation**: which alerts fire together, so one cause is not paged as five.

Nothing here invents a figure: a percentile over no data is None, not 0, and every ratio
carries the count it was computed from.
"""

from __future__ import annotations

import datetime as dt
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass, field

# An alert that fires again within this long after its previous episode on the SAME series
# (fingerprint) resolved is a flap, not a new problem.
FLAP_GAP = dt.timedelta(minutes=15)
# Two alerts whose episodes START this close together are counted as firing together.
CO_WINDOW = dt.timedelta(minutes=5)
# Flags are only raised from enough evidence — two episodes are an anecdote.
MIN_EPISODES_FOR_FLAG = 3
FLAP_RATIO = 0.3
SELF_RESOLVE_RATIO = 0.8
SHORT_LIVED = dt.timedelta(minutes=5)


@dataclass
class Episode:
    """The fields of an Occurrence that insight reads."""
    signature: str
    fingerprint: str
    alertname: str
    severity: str
    started_at: dt.datetime
    resolved_at: dt.datetime | None
    acked_at: dt.datetime | None
    resolved_how: str | None
    resolution_category: str | None
    team_id: str | None = None
    labels: dict = field(default_factory=dict)


@dataclass
class IncidentTimes:
    opened_at: dt.datetime
    acked_at: dt.datetime | None
    resolved_at: dt.datetime | None
    team_id: str | None = None


def percentile(values: list[float], q: float) -> float | None:
    """Linear interpolation between closest ranks — what Postgres' percentile_cont does, so
    a figure read here and one computed in SQL agree. None for no values, never 0."""
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    pos = (len(s) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def _secs(a: dt.datetime | None, b: dt.datetime | None) -> float | None:
    if a is None or b is None:
        return None
    return max(0.0, (b - a).total_seconds())


def noise(episodes: list[Episode], now: dt.datetime) -> list[dict]:
    """One row per alert signature, noisiest first."""
    by_sig: dict[str, list[Episode]] = defaultdict(list)
    for e in episodes:
        by_sig[e.signature].append(e)

    # Flaps are judged per SERIES: a cluster-wide alert with 40 pods is 40 series, and pod 7
    # firing right after pod 3 resolved is not a flap.
    flaps: Counter[str] = Counter()
    by_fp: dict[str, list[Episode]] = defaultdict(list)
    for e in episodes:
        by_fp[e.fingerprint].append(e)
    for eps in by_fp.values():
        eps.sort(key=lambda x: x.started_at)
        for prev, cur in zip(eps, eps[1:]):
            if prev.resolved_at is not None and cur.started_at - prev.resolved_at <= FLAP_GAP:
                flaps[cur.signature] += 1

    rows = []
    for sig, eps in by_sig.items():
        eps.sort(key=lambda x: x.started_at)
        n = len(eps)
        closed = [e for e in eps if e.resolved_at is not None]
        durations = [_secs(e.started_at, e.resolved_at) for e in closed]
        acked = sum(1 for e in eps if e.acked_at is not None)
        # Ended on its own (the sender said resolved, or it stopped being sent) and nobody
        # ever acknowledged it. A manual resolve is a person acting, so it does not count.
        self_resolved = sum(1 for e in closed
                            if e.acked_at is None and e.resolved_how in ("source", "timeout"))
        fixes = Counter(e.resolution_category for e in eps if e.resolution_category)
        median = percentile([d for d in durations if d is not None], 0.5)
        flags = []
        if n >= MIN_EPISODES_FOR_FLAG:
            if flaps[sig] / n >= FLAP_RATIO:
                flags.append("flapping")
            if closed and self_resolved / len(closed) >= SELF_RESOLVE_RATIO:
                flags.append("nobody_acts")
            if median is not None and median < SHORT_LIVED.total_seconds():
                flags.append("short_lived")
        last = eps[-1]
        rows.append({
            "signature": sig,
            "alertname": last.alertname,
            "severity": last.severity,
            "labels": last.labels,
            "team_id": last.team_id,
            "episodes": n,
            "series": len({e.fingerprint for e in eps}),
            "open": n - len(closed),
            "flaps": flaps[sig],
            "acked": acked,
            "self_resolved": self_resolved,
            "median_duration_s": median,
            "firing_s": sum(_secs(e.started_at, e.resolved_at or now) or 0 for e in eps),
            "last_started_at": last.started_at,
            "usual_fix": fixes.most_common(1)[0][0] if fixes else None,
            "flags": flags,
        })
    rows.sort(key=lambda r: (-r["episodes"], -r["flaps"], r["alertname"]))
    return rows


def _times(values: list[float | None]) -> dict:
    v = [x for x in values if x is not None]
    return {"n": len(v), "p50_s": percentile(v, 0.5), "p90_s": percentile(v, 0.9)}


def response(incidents: list[IncidentTimes]) -> dict:
    """MTTA/MTTR over incidents, plus how many were never acknowledged by anyone."""
    acked = [i for i in incidents if i.acked_at is not None]
    resolved = [i for i in incidents if i.resolved_at is not None]
    return {
        "incidents": len(incidents),
        "acknowledged": len(acked),
        "resolved": len(resolved),
        # Resolved without anybody acknowledging: either it fixed itself, or the page was
        # ignored. Both are worth knowing; neither is a response time.
        "resolved_unacked": sum(1 for i in resolved if i.acked_at is None),
        "open": len(incidents) - len(resolved),
        "time_to_ack": _times([_secs(i.opened_at, i.acked_at) for i in acked]),
        "time_to_resolve": _times([_secs(i.opened_at, i.resolved_at) for i in resolved]),
    }


def response_by_team(incidents: list[IncidentTimes]) -> dict[str | None, dict]:
    groups: dict[str | None, list[IncidentTimes]] = defaultdict(list)
    for i in incidents:
        groups[i.team_id].append(i)
    return {k: response(v) for k, v in groups.items()}


def correlations(episodes: list[Episode], *, signature: str | None = None,
                 min_together: int = 2, min_ratio: float = 0.5, limit: int = 20) -> list[dict]:
    """Pairs of alerts whose episodes start within CO_WINDOW of each other.

    `together` counts episodes with a partner starting in the window, from both sides, and
    keeps the smaller count. The ratio divides by
    the SMALLER episode count, so "B almost never fires without A" scores high even when A
    also fires on its own — which is the shape of a cause and its symptom.
    With `signature`, only pairs involving that alert are returned (its "fires together with").
    """
    starts: dict[str, list[dt.datetime]] = defaultdict(list)
    name: dict[str, str] = {}
    for e in episodes:
        starts[e.signature].append(e.started_at)
        name[e.signature] = e.alertname
    for v in starts.values():
        v.sort()
    sigs = [s for s, v in starts.items() if len(v) >= min_together]
    if signature is not None:
        if signature not in starts:
            return []
        pairs = [(signature, s) for s in sigs if s != signature]
    else:
        sigs.sort()
        pairs = [(a, b) for i, a in enumerate(sigs) for b in sigs[i + 1:]]

    def near(xs: list[dt.datetime], ys: list[dt.datetime]) -> int:
        return sum(1 for t in xs if bisect_right(ys, t + CO_WINDOW) > bisect_left(ys, t - CO_WINDOW))

    out = []
    for a, b in pairs:
        # Counted from BOTH sides and the smaller kept: five A episodes clustered round one B
        # are one coincidence, not five — and the ratio can then never exceed 1.
        together = min(near(starts[a], starts[b]), near(starts[b], starts[a]))
        if together < min_together:
            continue
        ratio = together / min(len(starts[a]), len(starts[b]))
        if ratio < min_ratio:
            continue
        out.append({"a": a, "a_alertname": name[a], "a_episodes": len(starts[a]),
                    "b": b, "b_alertname": name[b], "b_episodes": len(starts[b]),
                    "together": together, "ratio": round(ratio, 3)})
    out.sort(key=lambda r: (-r["ratio"], -r["together"], r["a_alertname"], r["b_alertname"]))
    return out[:limit]
