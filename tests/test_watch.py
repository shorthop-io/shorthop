"""Relay SHOR-31 / W7: Shorthop Watch from the server — forwarding Watchdogs, managing checks."""
import datetime as dt

import pytest
import requests

from app import locks, scheduler, watch
from app import settings as st
from app.db import SessionLocal
from app.models import Integration, now
from tests.conftest import bearer, make_integration, make_token, make_user

KEY = "shr_11111111-2222-3333-4444-555555555555." + "s" * 43
URL = "https://relay.example.test"
PING = "https://ping.example.test/p/"


class _Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class FakeWatch:
    """The relay's /v1/watch API at the requests layer: enough state to be honest."""

    def __init__(self):
        self.checks, self.calls, self.n, self.reply = {}, [], 0, None

    def _new_url(self):
        self.n += 1
        return f"{PING}tok{self.n:028d}"

    def __call__(self, method, url, json=None, params=None, headers=None, **kw):
        assert kw.get("allow_redirects") is False and kw.get("timeout")
        assert headers["Authorization"] == f"Bearer {KEY}"
        path = url.removeprefix(URL + "/v1/watch")
        self.calls.append((method, path, json))
        if self.reply:
            return self.reply
        if path == "/checks" and method == "GET":
            return _Resp(200, {"checks": list(self.checks.values()), "count": len(self.checks)})
        if path == "/checks" and method == "POST":
            cid = f"{len(self.checks) + 1:08d}-0000-0000-0000-000000000000"
            self.checks[cid] = {"id": cid, "state": "new", **json}
            return _Resp(201, {**self.checks[cid], "ping_url": self._new_url()})
        cid = path.split("/")[2]
        if cid not in self.checks:
            return _Resp(404, {"message": "No such Watch check for this installation."})
        if path.endswith("/rotate"):
            return _Resp(200, {**self.checks[cid], "ping_url": self._new_url()})
        if method == "DELETE":
            del self.checks[cid]
            return _Resp(204)
        if method == "PATCH":
            self.checks[cid].update(json)
        return _Resp(200, self.checks[cid])


@pytest.fixture()
def fake(app, monkeypatch):
    f = FakeWatch()
    monkeypatch.setattr(watch.requests, "request", f)
    st.put({"relay_url": URL, "relay_key": KEY}, "test")
    SessionLocal.commit()
    return f


@pytest.fixture()
def pings(monkeypatch):
    sent = []

    def fake_get(url, timeout=None, allow_redirects=None):
        assert allow_redirects is False and timeout
        sent.append(url)
        return _Resp(200)

    monkeypatch.setattr(watch.requests, "get", fake_get)
    return sent


def _fresh(iid):
    """The row as stored — the request's teardown removed the session the test row was in."""
    SessionLocal.remove()
    return SessionLocal.get(Integration, iid)


def admin():
    from sqlalchemy import select
    from app.models import User
    u = SessionLocal.scalars(select(User).where(User.email == "admin@example.com")).first() \
        or make_user("admin@example.com", role="admin")
    return bearer(make_token(u, ["alerts:read", "config:write"]))


def _bound(watchdog_at=None, forwarded_at=None, **kw):
    integ, _ = make_integration(watch_check_id="00000001-0000-0000-0000-000000000000",
                                watch_ping_url=f"{PING}tok", last_watchdog_at=watchdog_at,
                                watch_forwarded_at=forwarded_at, **kw)
    return integ


# --- forwarding -----------------------------------------------------------------------------

def test_a_new_watchdog_is_forwarded_once(app, pings):
    t = now()
    _bound(watchdog_at=t - dt.timedelta(seconds=5))
    assert watch.forward_watchdogs() == {"ok": 1, "error": 0}
    assert pings == [f"{PING}tok"]
    assert watch.forward_watchdogs() == {"ok": 0, "error": 0}      # nothing new since


def test_forwarding_is_debounced_to_once_a_minute(app, pings):
    t = now()
    integ = _bound(watchdog_at=t, forwarded_at=t - dt.timedelta(seconds=30))
    watch.forward_watchdogs()
    assert pings == []
    integ.watch_forwarded_at = t - dt.timedelta(seconds=61)
    SessionLocal.commit()
    watch.forward_watchdogs()
    assert len(pings) == 1


def test_no_watchdog_means_no_ping(app, pings):
    # The whole point: a silent Prometheus must let the check go down.
    t = now()
    _bound(watchdog_at=t - dt.timedelta(minutes=30), forwarded_at=t - dt.timedelta(minutes=29))
    _bound(name="never", watchdog_at=None)
    watch.forward_watchdogs()
    assert pings == []


def test_a_failed_ping_is_retried_next_tick(app, monkeypatch):
    integ = _bound(watchdog_at=now())

    def boom(*a, **k):
        raise requests.ConnectionError()

    monkeypatch.setattr(watch.requests, "get", boom)
    assert watch.forward_watchdogs() == {"ok": 0, "error": 1}
    SessionLocal.refresh(integ)
    assert integ.watch_forwarded_at is None


def test_disabled_and_archived_integrations_are_not_forwarded(app, pings):
    _bound(name="off", watchdog_at=now(), enabled=False)
    _bound(name="gone", watchdog_at=now(), archived_at=now())
    watch.forward_watchdogs()
    assert pings == []


def test_the_leader_tick_forwards(app, pings, monkeypatch):
    monkeypatch.setattr(locks.leader, "is_leader", lambda: True)
    _bound(watchdog_at=now())
    scheduler.tick()
    assert len(pings) == 1


def test_a_standby_pod_never_forwards(app, pings, monkeypatch):
    monkeypatch.setattr(locks.leader, "is_leader", lambda: False)
    _bound(watchdog_at=now())
    scheduler.tick()
    assert pings == []


# --- managing checks ------------------------------------------------------------------------

def test_create_bound_to_an_integration_uses_watchdog_defaults(client, fake):
    integ, _ = make_integration()
    iid = integ.id
    r = client.post("/api/v1/watch/checks", headers=admin(),
                    json={"name": "big", "integration_id": integ.id})
    assert r.status_code == 201
    body = r.json
    sent = fake.calls[-1][2]
    # One ping a minute; grace longer than ShorthopIngestSilent (10 min) so Shorthop pages first.
    assert sent["interval_s"] == 60 and sent["grace_s"] == 15 * 60
    assert body["ping_url"].startswith(PING) and body["snippets"]["curl"].endswith(body["ping_url"])
    assert body["forwarded_by"]["id"] == integ.id
    integ = _fresh(iid)
    assert integ.watch_ping_url == body["ping_url"] and integ.watch_check_id == body["id"]


def test_the_ping_url_is_never_returned_with_the_integration(client, fake):
    integ, _ = make_integration()
    iid = integ.id
    url = client.post("/api/v1/watch/checks", headers=admin(),
                      json={"name": "big", "integration_id": integ.id}).json["ping_url"]
    listed = client.get("/api/v1/integrations", headers=admin()).get_data(as_text=True)
    assert url not in listed and url.rsplit("/", 1)[1] not in listed
    assert '"watch_check_id"' in listed


def test_only_a_prometheus_integration_can_forward(client, fake):
    integ, _ = make_integration(kind="webhook")
    r = client.post("/api/v1/watch/checks", headers=admin(),
                    json={"name": "x", "integration_id": integ.id})
    assert r.status_code == 422 and fake.calls == []


def test_one_check_per_integration(client, fake):
    integ, _ = make_integration()
    iid = integ.id
    h = admin()
    client.post("/api/v1/watch/checks", headers=h, json={"name": "a", "integration_id": integ.id})
    r = client.post("/api/v1/watch/checks", headers=h, json={"name": "b", "integration_id": integ.id})
    assert r.status_code == 409


def test_a_standalone_check_needs_an_interval(client, fake):
    assert client.post("/api/v1/watch/checks", headers=admin(), json={"name": "x"}).status_code == 422
    r = client.post("/api/v1/watch/checks", headers=admin(), json={"name": "backup", "interval_s": 86400})
    assert r.status_code == 201 and r.json.get("forwarded_by") is None


def test_list_says_which_integration_forwards(client, fake):
    integ, _ = make_integration(name="prom-big")
    h = admin()
    client.post("/api/v1/watch/checks", headers=h, json={"name": "a", "integration_id": integ.id})
    client.post("/api/v1/watch/checks", headers=h, json={"name": "b", "interval_s": 300})
    checks = {c["name"]: c for c in client.get("/api/v1/watch/checks", headers=h).json["checks"]}
    assert checks["a"]["forwarded_by"]["name"] == "prom-big" and checks["b"]["forwarded_by"] is None


def test_rotate_re_stores_a_bound_url(client, fake):
    integ, _ = make_integration()
    iid = integ.id
    h = admin()
    cid = client.post("/api/v1/watch/checks", headers=h,
                      json={"name": "a", "integration_id": integ.id}).json["id"]
    new = client.post(f"/api/v1/watch/checks/{cid}/rotate", headers=h).json["ping_url"]
    integ = _fresh(iid)
    assert integ.watch_ping_url == new


def test_deleting_the_check_stops_forwarding(client, fake):
    integ, _ = make_integration()
    iid = integ.id
    h = admin()
    cid = client.post("/api/v1/watch/checks", headers=h,
                      json={"name": "a", "integration_id": integ.id}).json["id"]
    assert client.delete(f"/api/v1/watch/checks/{cid}", headers=h).status_code == 200
    integ = _fresh(iid)
    assert integ.watch_ping_url is None and integ.watch_check_id is None


def test_binding_an_existing_check_rotates_it(client, fake):
    integ, _ = make_integration()
    iid = integ.id
    h = admin()
    created = client.post("/api/v1/watch/checks", headers=h, json={"name": "a", "interval_s": 60}).json
    r = client.post(f"/api/v1/integrations/{integ.id}/watch", headers=h, json={"check_id": created["id"]})
    assert r.status_code == 200 and r.json["watch_check_id"] == created["id"]
    integ = _fresh(iid)
    assert integ.watch_ping_url and integ.watch_ping_url != created["ping_url"]
    assert client.delete(f"/api/v1/integrations/{integ.id}/watch", headers=h).json["watch_check_id"] is None


def test_a_check_cannot_take_two_integrations(client, fake):
    a, _ = make_integration(name="a")
    b, _ = make_integration(name="b")
    h = admin()
    cid = client.post("/api/v1/watch/checks", headers=h,
                      json={"name": "x", "integration_id": a.id}).json["id"]
    r = client.post(f"/api/v1/integrations/{b.id}/watch", headers=h, json={"check_id": cid})
    assert r.status_code == 409


# --- relay errors in the relay's own words --------------------------------------------------

def test_the_relays_refusal_is_passed_through(client, fake):
    fake.reply = _Resp(402, {"message": "Add a payment method at https://shorthop.io (€1.99 per check)",
                             "errors": {"code": "check_quota"}})
    r = client.post("/api/v1/watch/checks", headers=admin(), json={"name": "x", "interval_s": 60})
    assert r.status_code == 402 and "€1.99 per check" in r.json["message"]


def test_a_rejected_key_says_where_to_look(client, fake):
    fake.reply = _Resp(401, {"message": "bad key"})
    r = client.get("/api/v1/watch/checks", headers=admin())
    assert r.status_code == 502 and "Settings → Push relay" in r.json["message"]


def test_no_relay_configured(client, app):
    r = client.get("/api/v1/watch/checks", headers=admin())
    assert r.status_code == 502 and "Push relay" in r.json["message"]


# --- scopes ---------------------------------------------------------------------------------

def test_a_responder_can_look_but_not_change(client, fake):
    h = bearer(make_token(make_user("r@example.com", role="responder"), ["alerts:read", "alerts:write"]))
    assert client.get("/api/v1/watch/checks", headers=h).status_code == 200
    assert client.post("/api/v1/watch/checks", headers=h, json={"name": "x", "interval_s": 60}).status_code == 403


def test_valid_ping_url():
    assert watch.valid_ping_url("https://ping.shorthop.io/p/abcdefghijklmnopqrstuvwxyz")
    for bad in ("http://ping.shorthop.io/p/abc", "https://ping.shorthop.io/x/abc",
                "https://u:p@ping.shorthop.io/p/abc", "https://ping.shorthop.io/p/abc?x=1",
                "https://ping.shorthop.io/p/a/b", "https://ping.shorthop.io/p/"):
        assert not watch.valid_ping_url(bad), bad
