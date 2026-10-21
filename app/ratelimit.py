"""Rate limits counted in the database, so every replica draws on ONE budget.

A fixed window (not sliding): `count` per (bucket, key, window start), bumped with an atomic
upsert — `INSERT … ON CONFLICT DO UPDATE … RETURNING count`, one round trip, race-free across
replicas. A fixed window lets up to 2× the limit through across a window boundary; for a
brute-force guard on a short-lived single-use code that is fine and much simpler than a
sliding log.
"""
import datetime as dt
import hashlib

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .db import IS_SQLITE, SessionLocal
from .models import RateWindow, now

# The longest window any caller uses; rows older than this are pruned.
MAX_WINDOW_S = 3600


def hit(bucket: str, key: str, limit: int, window_s: int) -> bool:
    """Count one attempt. True when this attempt is OVER the limit. Commits on its own, so
    a request that is then refused still counts."""
    db = SessionLocal
    t = now()
    epoch = int(t.timestamp())
    start = dt.datetime.fromtimestamp(epoch - epoch % window_s, tz=dt.timezone.utc)
    ins = (sqlite_insert if IS_SQLITE else pg_insert)(RateWindow).values(
        bucket=bucket, key_hash=hashlib.sha256(key.encode()).hexdigest(),
        window_start=start, count=1)
    stmt = ins.on_conflict_do_update(
        index_elements=["bucket", "key_hash", "window_start"],
        set_={"count": RateWindow.count + 1}).returning(RateWindow.count)
    count = db.execute(stmt).scalar()
    db.commit()
    return count > limit


def count(bucket: str, key: str, window_s: int) -> int:
    """Attempts in the CURRENT window, without counting one. For limits that charge only
    failures: check `count` first, `hit` when the attempt failed."""
    t = now()
    epoch = int(t.timestamp())
    start = dt.datetime.fromtimestamp(epoch - epoch % window_s, tz=dt.timezone.utc)
    n = SessionLocal.execute(select(RateWindow.count).where(
        RateWindow.bucket == bucket,
        RateWindow.key_hash == hashlib.sha256(key.encode()).hexdigest(),
        RateWindow.window_start == start)).scalar()
    return n or 0


def prune() -> int:
    """Delete windows that can no longer matter. Called from the leader's tick."""
    db = SessionLocal
    gone = db.execute(delete(RateWindow).where(
        RateWindow.window_start < now() - dt.timedelta(seconds=MAX_WINDOW_S))).rowcount
    db.commit()
    return gone or 0
