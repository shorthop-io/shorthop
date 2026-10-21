"""Two replicas against one database.

Each test runs the same work in two threads. A thread has its own scoped session, i.e. its
own Postgres connection — exactly what a second pod is, as far as the database can tell.
The slow stubs are there to make the two overlap; without the locks they then step on each
other every run, not occasionally.

Postgres only: SQLite has no row locks or advisory locks, and the code paths are no-ops there.
"""
import threading
import time

import pytest
from sqlalchemy import func, select

from app import delivery, incidents, locks
from app.core import identity
from app.db import IS_SQLITE, SessionLocal
from app.models import Incident, Notification, Occurrence, now
from tests.conftest import bearer, make_integration
from tests.test_ingest import PROM, _prom_alert
from tests.test_paging import setup  # noqa: F401 — fixture

pytestmark = pytest.mark.skipif(IS_SQLITE, reason="needs Postgres row and advisory locks")


def _both(fn, *args):
    """Run fn in two threads at once (two "replicas"); re-raise anything either raised."""
    errors, barrier = [], threading.Barrier(2)

    def run():
        try:
            barrier.wait()
            fn(*args)
        except Exception as e:  # noqa: BLE001
            errors.append(e)
        finally:
            SessionLocal.remove()

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    if errors:
        raise errors[0]


def test_two_replicas_page_each_step_exactly_once(client, setup, monkeypatch):  # noqa: F811
    sent, mu = [], threading.Lock()

    def slow_email(to, subject, body):
        time.sleep(0.05)
        with mu:
            sent.append((to, subject))

    monkeypatch.setattr(delivery, "send_email", slow_email)
    real_dest = incidents._destinations

    def slow_destinations(*a, **kw):
        time.sleep(0.05)
        return real_dest(*a, **kw)

    monkeypatch.setattr(incidents, "_destinations", slow_destinations)

    # Ten incidents (one per namespace), each with step 1 due now → one page to ann each.
    client.post(PROM, headers=bearer(setup["itok"]),
                json=[_prom_alert(pod="p", namespace=f"ns{i}") for i in range(10)])
    SessionLocal.remove()

    _both(lambda: (incidents.run_due(), delivery.deliver_pending()))
    _both(delivery.deliver_pending)  # whatever the first round left for the other replica

    SessionLocal.expire_all()
    incs = SessionLocal.scalars(select(Incident)).all()
    assert len(incs) == 10
    assert all(i.next_step == 1 for i in incs), "a step ran twice, or not at all"
    notes = SessionLocal.scalars(select(Notification)).all()
    assert len(notes) == 10, "duplicate notifications queued"
    assert all(n.status == "sent" and n.attempts == 1 for n in notes)
    assert len(sent) == 10, f"pages sent: {len(sent)}"


def test_concurrent_batches_with_the_same_new_alert(client, monkeypatch):
    integ, itok = make_integration("prometheus")
    real_sig = identity.signature

    def slow_signature(*a, **kw):
        time.sleep(0.2)  # between "is it open?" and the insert
        return real_sig(*a, **kw)

    monkeypatch.setattr(identity, "signature", slow_signature)
    codes = []

    def send():
        codes.append(client.application.test_client().post(
            PROM, json=[_prom_alert(pod="same")], headers=bearer(itok)).status_code)

    _both(send)
    assert codes == [200, 200]
    SessionLocal.expire_all()
    occ = SessionLocal.scalars(select(Occurrence)).all()
    assert len(occ) == 1 and occ[0].send_count == 2  # one opened it, one updated it


def test_incidents_opened_at_once_get_different_numbers(client, setup, monkeypatch):  # noqa: F811
    _, itok2 = make_integration("prometheus", name="prometheus-2")
    real_event = incidents.TimelineEvent

    def slow_event(*a, **kw):
        time.sleep(0.2)  # after the number was read, before the commit
        return real_event(*a, **kw)

    monkeypatch.setattr(incidents, "TimelineEvent", slow_event)
    tokens = iter([setup["itok"], itok2])
    mu = threading.Lock()

    def send():
        with mu:
            tok = next(tokens)
        client.application.test_client().post(
            PROM, json=[_prom_alert(pod="x", namespace=tok[-6:])], headers=bearer(tok))

    _both(send)
    SessionLocal.expire_all()
    numbers = SessionLocal.scalars(select(Incident.number)).all()
    assert sorted(numbers) == [1, 2]


def test_one_leader_and_failover(app):
    a, b = locks.Leader(), locks.Leader()
    try:
        assert a.is_leader() is True
        assert b.is_leader() is False
        assert a.is_leader() is True, "the leader must keep the lock across ticks"
        a.release()  # the leader's session ends (pod gone)
        assert b.is_leader() is True
        assert a.is_leader() is False
    finally:
        a.release()
        b.release()


def test_rate_limit_is_shared_between_replicas(client, monkeypatch):
    from app.api import devices as d
    monkeypatch.setattr(d, "_PAIR_LIMIT", 4)
    codes, mu = [], threading.Lock()

    def attempt():
        for _ in range(3):
            c = client.application.test_client().post(
                "/api/v1/devices/pair", json={"code": "AAAA-BBBB"}).status_code
            with mu:
                codes.append(c)

    _both(attempt)  # six attempts from one address across two "replicas", limit four
    assert sorted(codes) == [400] * 4 + [429] * 2
    SessionLocal.expire_all()
    from app.models import RateWindow
    assert SessionLocal.scalar(select(func.sum(RateWindow.count))) == 6


def test_handover_notice_sent_once_by_two_ticks(app, monkeypatch):
    """Two leaders for a moment (a switchover) must not mail the same handover twice."""
    from app import schedules
    from app.models import Schedule, User
    u1 = User(email="one@example.com", role="responder")
    u2 = User(email="two@example.com", role="responder")
    SessionLocal.add_all([u1, u2])
    SessionLocal.commit()
    s = Schedule(name="primary", timezone="UTC", layers=[], handover_notify=True,
                 last_on_call=u1.id)
    SessionLocal.add(s)
    SessionLocal.commit()
    SessionLocal.remove()

    mails, mu = [], threading.Lock()

    def slow_mail(to, subject, body):
        with mu:
            mails.append(to)

    monkeypatch.setattr(delivery, "send_email", slow_mail)
    def slow_on_call(sched, t):
        time.sleep(0.1)  # both "leaders" have read last_on_call before either writes
        return {"user_id": u2.id}

    monkeypatch.setattr(schedules, "on_call_now", slow_on_call)
    monkeypatch.setattr(schedules, "next_change", lambda sched, t: None)
    _both(schedules.check_handovers)
    assert mails == ["two@example.com"]


def test_leader_steps_down_when_its_session_is_killed(app):
    """The pod is alive but its database session died (network blip, pg_terminate_backend,
    a dbdeck switch disconnecting clients): Postgres has released the lock, so this replica
    must stop believing it is the leader — and the other must take over."""
    from sqlalchemy import text

    from app.db import engine
    a, b = locks.Leader(), locks.Leader()
    try:
        assert a.is_leader()
        pid = a._conn.execute(text("SELECT pg_backend_pid()")).scalar()
        a._conn.commit()
        with engine.connect() as admin:
            admin.execute(text("SELECT pg_terminate_backend(:p)"), {"p": pid})
            admin.commit()
        assert b.is_leader() is True
        assert a.is_leader() is False
    finally:
        a.release()
        b.release()


def test_leader_steps_down_when_the_lock_is_gone_but_the_connection_lives(app):
    """The nastier case: the connection still answers, the lock is no longer held. Only the
    explicit pg_locks check catches it; trusting "the connection works" gives two leaders."""
    from sqlalchemy import text
    a, b = locks.Leader(), locks.Leader()
    try:
        assert a.is_leader()
        a._conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": a.key})
        a._conn.commit()
        assert b.is_leader() is True
        assert a.is_leader() is False, "two leaders"
    finally:
        a.release()
        b.release()


def test_leader_steps_down_on_a_fenced_primary(app):
    """After a dbdeck switchover the old primary is alive and READ-ONLY, and the leader's
    lock is still held THERE — while the other replica can take the same lock on the new
    primary. The leader must notice it is on a fenced server and step down. (Simulated with a
    session SET; dbdeck fences with the server-wide default, which reads the same.)"""
    from sqlalchemy import text
    a, b = locks.Leader(), locks.Leader()
    try:
        assert a.is_leader()
        fenced_pid = a._conn.execute(text("SELECT pg_backend_pid()")).scalar()
        a._conn.execute(text("SET default_transaction_read_only = on"))
        a._conn.commit()
        # It gives the fenced session (and the lock on it) up, then reconnects — in a real
        # switch to the NEW primary, where taking the lock again is correct.
        assert a.is_leader() is True
        new_pid = a._conn.execute(text("SELECT pg_backend_pid()")).scalar()
        a._conn.commit()
        assert new_pid != fenced_pid, "still leading from the fenced session"
        assert b.is_leader() is False
    finally:
        a.release()
        b.release()
