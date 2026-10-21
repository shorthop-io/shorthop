"""Who is on call: layered rotations + overrides. Pure — plain dicts in, answers out.

A schedule has a timezone and LAYERS, lowest first:

    {"name": "24/7", "users": [ann, bob, cy], "rotation_hours": 168,
     "start": "2026-10-19T09:00",                     # local wall time of the first handoff
     "restriction": {"days": [0,1,2,3,4], "start": "08:00", "end": "18:00"} | None}

At any instant: an override covering it wins (the newest one, if several); otherwise the
HIGHEST layer whose restriction covers it; within a layer, the person is
users[floor((local_now - local_start) / rotation) % len(users)].

⚠️ Rotation is counted in LOCAL WALL-CLOCK time, not in elapsed hours. A weekly handoff at
"Monday 09:00" must stay at 09:00 across the DST change; counting elapsed hours would move it
to 08:00 or 10:00 for half the year — the kind of off-by-one-hour that nobody notices until
the wrong person is woken.
"""
import datetime as dt
from zoneinfo import ZoneInfo

UTC = dt.timezone.utc


def _local(at: dt.datetime, tz: ZoneInfo) -> dt.datetime:
    """Aware UTC instant → naive local wall time."""
    return at.astimezone(tz).replace(tzinfo=None)


def _to_utc(local_naive: dt.datetime, tz: ZoneInfo) -> dt.datetime:
    """Naive local wall time → aware UTC. A time that does not exist (spring-forward gap) or
    exists twice (fall-back) resolves with fold=0, the earlier reading — consistently."""
    return local_naive.replace(tzinfo=tz).astimezone(UTC)


def _parse_local(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s).replace(tzinfo=None)


def _hm(s: str) -> dt.time:
    h, m = s.split(":")
    return dt.time(int(h), int(m))


def covers(restriction: dict | None, local: dt.datetime) -> bool:
    """Does the restriction cover this local wall time? None = always. An end before the start
    wraps past midnight (18:00–08:00 is the night shift), and then the DAY that matters is
    the day the window started on."""
    if not restriction:
        return True
    start, end = _hm(restriction["start"]), _hm(restriction["end"])
    days = set(restriction.get("days") or range(7))
    t = local.time()
    if start <= end:
        return local.weekday() in days and start <= t < end
    if t >= start:
        return local.weekday() in days
    if t < end:
        return (local - dt.timedelta(days=1)).weekday() in days
    return False


def layer_user(layer: dict, local: dt.datetime) -> str | None:
    users = layer.get("users") or []
    if not users:
        return None
    start = _parse_local(layer["start"])
    if local < start:
        return None
    period = dt.timedelta(hours=float(layer["rotation_hours"]))
    n = int((local - start) / period)
    return users[n % len(users)]


def on_call(schedule: dict, overrides: list[dict], at: dt.datetime) -> dict:
    """{"user_id": … | None, "source": "override" | "layer:<name>" | None}."""
    covering = [o for o in overrides if o["starts_at"] <= at < o["ends_at"]]
    if covering:
        newest = max(covering, key=lambda o: o.get("created_at") or o["starts_at"])
        return {"user_id": newest["user_id"], "source": "override"}
    tz = ZoneInfo(schedule.get("timezone") or "UTC")
    local = _local(at, tz)
    for layer in reversed(schedule.get("layers") or []):
        if covers(layer.get("restriction"), local):
            uid = layer_user(layer, local)
            if uid:
                return {"user_id": uid, "source": f"layer:{layer.get('name', '')}"}
    return {"user_id": None, "source": None}


def _boundaries(schedule: dict, overrides: list[dict], start: dt.datetime,
                end: dt.datetime) -> list[dt.datetime]:
    """Every instant in [start, end) at which the answer COULD change."""
    tz = ZoneInfo(schedule.get("timezone") or "UTC")
    points = {start}
    lo, hi = _local(start, tz), _local(end, tz)
    for layer in schedule.get("layers") or []:
        first = _parse_local(layer["start"])
        period = dt.timedelta(hours=float(layer["rotation_hours"]))
        # Handoffs: walk from the last handoff before the window.
        k = max(0, int((lo - first) / period)) if lo > first else 0
        t = first + k * period
        while t < hi:
            points.add(_to_utc(t, tz))
            t += period
        r = layer.get("restriction")
        if r:
            day = dt.datetime.combine(lo.date() - dt.timedelta(days=1), dt.time())
            while day < hi + dt.timedelta(days=1):
                for hm in (r["start"], r["end"]):
                    points.add(_to_utc(dt.datetime.combine(day.date(), _hm(hm)), tz))
                day += dt.timedelta(days=1)
    for o in overrides:
        points.update((o["starts_at"], o["ends_at"]))
    return sorted(p for p in points if start <= p < end)


def shifts(schedule: dict, overrides: list[dict], start: dt.datetime,
           end: dt.datetime) -> list[dict]:
    """The final schedule over [start, end) as contiguous segments:
    [{"user_id", "source", "start", "end"}], gaps included with user_id None."""
    out: list[dict] = []
    points = _boundaries(schedule, overrides, start, end) + [end]
    for a, b in zip(points, points[1:]):
        if a >= b:
            continue
        who = on_call(schedule, overrides, a)
        if out and out[-1]["user_id"] == who["user_id"] and out[-1]["source"] == who["source"]:
            out[-1]["end"] = b
        else:
            out.append({**who, "start": a, "end": b})
    return out


def _reach(schedule: dict) -> dt.timedelta:
    """How far outside a window a shift that crosses its edge can extend: the longest rotation
    (one person's whole turn), and never less than an override can last (62 days)."""
    hours = max([float(l.get("rotation_hours") or 0) for l in schedule.get("layers") or []] or [0])
    return min(max(dt.timedelta(hours=hours), dt.timedelta(days=62)), dt.timedelta(days=366))


def framed_shifts(schedule: dict, overrides: list[dict], start: dt.datetime,
                  end: dt.datetime) -> list[dict]:
    """`shifts` over [start, end) — clipped to the window for DRAWING — with each segment's
    whole extent beside it as `shift_start`/`shift_end`. A view of 14 days cuts the shift that
    began last Thursday at the window's first instant; overriding "that shift" must mean the
    whole of it, not the part that happened to be on screen. Overrides passed in must cover
    the reach (see `_reach`), or a segment's far end is only as known as they are."""
    reach = _reach(schedule)
    out = []
    for seg in shifts(schedule, overrides, start - reach, end + reach):
        if seg["end"] <= start or seg["start"] >= end:
            continue
        out.append({**seg, "start": max(seg["start"], start), "end": min(seg["end"], end),
                    "shift_start": seg["start"], "shift_end": seg["end"]})
    return out


def starts_at(schedule: dict) -> dt.datetime | None:
    """When the schedule begins: its earliest layer start, as a UTC instant. Before it there is
    no schedule yet — which is NOT a gap in coverage and must not be shown as one."""
    layers = schedule.get("layers") or []
    if not layers:
        return None
    tz = ZoneInfo(schedule.get("timezone") or "UTC")
    return min(_to_utc(_parse_local(l["start"]), tz) for l in layers)


def validate_layers(layers) -> list[dict]:
    """Normalise; raise ValueError with a caller-safe message."""
    if not isinstance(layers, list) or not layers:
        raise ValueError("a schedule needs at least one layer")
    if len(layers) > 10:
        raise ValueError("at most 10 layers")
    out = []
    for i, layer in enumerate(layers, 1):
        if not isinstance(layer, dict):
            raise ValueError(f"layer #{i} must be an object")
        users = layer.get("users") or []
        if not users or len(users) > 50:
            raise ValueError(f"layer #{i}: 1–50 people")
        try:
            hours = float(layer.get("rotation_hours", 0))
        except (TypeError, ValueError):
            raise ValueError(f"layer #{i}: rotation_hours must be a number") from None
        if not 1 <= hours <= 24 * 28:
            raise ValueError(f"layer #{i}: rotation between 1 hour and 4 weeks")
        try:
            start = _parse_local(str(layer.get("start", "")))
        except ValueError:
            raise ValueError(f"layer #{i}: start must be a local date-time like 2026-10-19T09:00") from None
        r = layer.get("restriction")
        if r:
            try:
                _hm(r["start"]), _hm(r["end"])
            except (KeyError, ValueError, TypeError):
                raise ValueError(f"layer #{i}: restriction needs start and end as HH:MM") from None
            days = r.get("days") or list(range(7))
            if not all(isinstance(d, int) and 0 <= d <= 6 for d in days):
                raise ValueError(f"layer #{i}: restriction days are 0 (Monday) … 6 (Sunday)")
            r = {"days": sorted(set(days)), "start": r["start"], "end": r["end"]}
        out.append({"name": str(layer.get("name") or f"Layer {i}")[:100], "users": list(users),
                    "rotation_hours": hours, "start": start.isoformat(timespec="minutes"),
                    "restriction": r or None})
    return out
