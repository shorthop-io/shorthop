"""SHOR-24: the heartbeat means "this Shorthop can deliver a page", not "the process is up"."""
import time

import pytest

from app import config as cfg
from app import locks, scheduler


@pytest.fixture
def pings(monkeypatch):
    sent = []
    monkeypatch.setattr(cfg, "HEARTBEAT_URL", "https://ping.example/p/abc")
    monkeypatch.setattr(locks.leader, "is_leader", lambda: True)
    monkeypatch.setattr(scheduler.requests, "get", lambda url, timeout: sent.append(url))
    return sent


def test_ping_goes_out_while_dispatch_is_ticking(pings):
    scheduler.dispatch()
    scheduler.tick()
    assert pings == ["https://ping.example/p/abc"]


def test_ping_is_withheld_when_dispatch_has_stalled(pings, monkeypatch):
    scheduler.dispatch()
    stale = time.monotonic() - cfg.HEARTBEAT_DISPATCH_MAX_AGE_SECONDS - 1
    monkeypatch.setattr(scheduler, "_last_dispatch_ok", stale)
    before = scheduler.HEARTBEAT_WITHHELD._value.get()
    scheduler.tick()
    assert pings == []
    assert scheduler.HEARTBEAT_WITHHELD._value.get() == before + 1


def test_no_ping_before_dispatch_has_ever_ticked(pings, monkeypatch):
    monkeypatch.setattr(scheduler, "_last_dispatch_ok", None)
    scheduler.tick()
    assert pings == []


def test_a_failing_dispatch_does_not_count_as_a_tick(pings, monkeypatch):
    monkeypatch.setattr(scheduler, "_last_dispatch_ok", None)
    def boom():
        raise RuntimeError("database gone")
    monkeypatch.setattr(scheduler.incidents, "run_due", boom)
    with pytest.raises(RuntimeError):
        scheduler.dispatch()
    scheduler.tick()
    assert pings == []


def test_followers_never_ping(pings, monkeypatch):
    monkeypatch.setattr(locks.leader, "is_leader", lambda: False)
    scheduler.dispatch()
    scheduler.tick()
    assert pings == []
