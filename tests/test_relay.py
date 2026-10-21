"""SHOR-9: pushing through the Shorthop push relay when one is configured."""
import pytest
import requests
from sqlalchemy import select

from app import delivery, relay
from app import settings as st
from app.db import SessionLocal
from app.models import Device
from tests.conftest import bearer, make_token, make_user

KEY = "shr_11111111-2222-3333-4444-555555555555." + "s" * 43
URL = "https://relay.example.test"


def push_token(n=1):
    return f"ExponentPushToken[phone{n:03d}abcdefghijk]"


class FakeRelay:
    """Stands in for relay.shorthop.io at the requests layer."""

    def __init__(self, free=3):
        self.calls, self.free, self.devices, self.push_reply = [], free, {}, None

    def __call__(self, method, url, json=None, headers=None, **kw):
        assert kw.get("allow_redirects") is False and kw.get("timeout")
        assert headers["Authorization"] == f"Bearer {KEY}"
        path = url.removeprefix(URL)
        self.calls.append((method, path, json))
        if method == "POST" and path == "/v1/devices":
            for did, tok in self.devices.items():
                if tok == json["push_token"]:
                    return _Resp(200, {"id": did})
            if len(self.devices) >= self.free:
                return _Resp(402, {"code": 402, "message": "Add a payment method at https://shorthop.io",
                                   "errors": {"code": "device_quota"}})
            did = f"{len(self.devices) + 1:08d}-0000-0000-0000-000000000000"
            self.devices[did] = json["push_token"]
            return _Resp(201, {"id": did})
        if method == "DELETE" and path.startswith("/v1/devices/"):
            self.devices.pop(path.rsplit("/", 1)[1], None)
            return _Resp(204, None)
        if method == "POST" and path == "/v1/devices/seen":
            return _Resp(200, {"updated": sum(1 for d in json["devices"] if d["id"] in self.devices)})
        if method == "POST" and path == "/v1/push":
            if self.push_reply:
                return self.push_reply
            return _Resp(200, {"outcome": "ok", "error": None})
        if method == "GET" and path == "/v1/installation":
            return _Resp(200, {"id": "inst", "name": "Acme", "signed_in": len(self.devices),
                               "free": self.free, "card_on_file": False,
                               "can_add_device": len(self.devices) < self.free,
                               "account_url": self.account_url})
        return _Resp(404, {"message": "no"})

    account_url = "https://account.example.test/account"


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


@pytest.fixture()
def fake(app, monkeypatch):
    f = FakeRelay()
    monkeypatch.setattr(relay.requests, "request", f)

    def no_expo(*a, **k):
        raise AssertionError("went straight to Expo although a relay is configured")
    monkeypatch.setattr(delivery.requests, "post", no_expo)
    st.put({"relay_url": URL, "relay_key": KEY}, "test")
    SessionLocal.commit()
    return f


def _phone(client, tok, n=1, name="pixel"):
    code = client.post("/api/v1/devices/pair-codes", headers=bearer(tok)).json
    r = client.post("/api/v1/devices/pair", json={"code": code["code"], "name": name, "platform": "android"})
    dev_tok = r.json["token"]
    reg = client.put("/api/v1/devices/me/push", headers=bearer(dev_tok), json={"push_token": push_token(n)})
    return dev_tok, reg


def _admin():
    u = make_user(email="boss@example.com", role="admin")
    return u, make_token(u, ["admin"])


class _Inc:
    id, number, title, severity, alerts, reassigned_at = "inc-123", 7, "DB primary down", "critical", [], None


# --- registration -------------------------------------------------------------------------

def test_registering_push_signs_the_phone_in_at_the_relay(client, fake):
    _, tok = _admin()
    _, reg = _phone(client, tok)
    assert reg.status_code == 200
    assert reg.json["relay_status"] == "registered"
    method, path, body = fake.calls[0]
    assert (method, path) == ("POST", "/v1/devices")
    assert body == {"push_token": push_token(1), "platform": "android", "label": "pixel"}


def test_a_refused_phone_still_pairs_and_says_why(client, fake):
    fake.free = 0
    _, tok = _admin()
    dev_tok, reg = _phone(client, tok)
    assert reg.status_code == 200 and reg.json["has_push"]
    assert reg.json["relay_status"] == "refused"
    assert "payment method" in reg.json["relay_message"]
    # still a working phone for everything but push
    assert client.get("/api/v1/incidents", headers=bearer(dev_tok)).status_code == 200


def test_unpairing_frees_the_seat(client, fake):
    _, tok = _admin()
    _, reg = _phone(client, tok)
    assert client.delete(f"/api/v1/devices/{reg.json['id']}", headers=bearer(tok)).status_code == 200
    assert fake.calls[-1][0] == "DELETE" and not fake.devices


# --- delivery -----------------------------------------------------------------------------

def test_page_goes_through_the_relay_with_no_alert_content(client, fake):
    _, tok = _admin()
    _, reg = _phone(client, tok)
    delivery.send_push(SessionLocal, reg.json["id"], _Inc(), "triggered")
    method, path, body = fake.calls[-1]
    assert (method, path) == ("POST", "/v1/push")
    assert body == {"device_id": next(iter(fake.devices)), "incident_id": "inc-123", "event": "triggered"}
    assert "DB primary" not in repr(fake.calls)


def test_events_the_relay_does_not_know_page_as_triggered(client, fake):
    _, tok = _admin()
    _, reg = _phone(client, tok)
    delivery.send_push(SessionLocal, reg.json["id"], _Inc(), "reassigned")
    assert fake.calls[-1][2]["event"] == "triggered"


def test_a_refused_phone_fails_the_page_with_the_reason(client, fake):
    fake.free = 0
    _, tok = _admin()
    _, reg = _phone(client, tok)
    with pytest.raises(delivery.DeliveryError, match="payment method"):
        delivery.send_push(SessionLocal, reg.json["id"], _Inc(), "triggered")


def test_app_removed_drops_the_push_token(client, fake):
    _, tok = _admin()
    _, reg = _phone(client, tok)
    fake.push_reply = _Resp(200, {"outcome": "unregistered", "error": "DeviceNotRegistered"})
    with pytest.raises(delivery.DeliveryError, match="DeviceNotRegistered"):
        delivery.send_push(SessionLocal, reg.json["id"], _Inc(), "triggered")
    dev = SessionLocal.get(Device, reg.json["id"])
    assert dev.push_token is None and dev.relay_device_id is None


def test_signed_out_at_the_relay_signs_in_again_on_the_next_attempt(client, fake):
    _, tok = _admin()
    _, reg = _phone(client, tok)
    fake.push_reply = _Resp(410, {"message": "signed out", "errors": {"code": "signed_out", "reason": "admin"}})
    fake.devices.clear()   # the relay has signed it out (not for being unused)
    with pytest.raises(delivery.DeliveryError, match="signed in again"):
        delivery.send_push(SessionLocal, reg.json["id"], _Inc(), "triggered")
    fake.push_reply = None
    delivery.send_push(SessionLocal, reg.json["id"], _Inc(), "triggered")
    assert [c[1] for c in fake.calls[-2:]] == ["/v1/devices", "/v1/push"]


def test_relay_down_is_a_retryable_failure_without_the_key(client, fake, monkeypatch):
    _, tok = _admin()
    _, reg = _phone(client, tok)

    def down(*a, **k):
        raise requests.ConnectionError(f"connection to {URL} failed, key {KEY}")
    monkeypatch.setattr(relay.requests, "request", down)
    with pytest.raises(delivery.DeliveryError) as e:
        delivery.send_push(SessionLocal, reg.json["id"], _Inc(), "triggered")
    assert "could not reach" in str(e.value) and KEY not in str(e.value)


def test_a_rejected_key_says_where_to_fix_it(client, fake):
    _, tok = _admin()
    _, reg = _phone(client, tok)
    fake.push_reply = _Resp(401, {"message": "Missing or invalid installation key."})
    with pytest.raises(delivery.DeliveryError, match="Settings → Push relay"):
        delivery.send_push(SessionLocal, reg.json["id"], _Inc(), "triggered")


# --- settings -----------------------------------------------------------------------------

def test_relay_settings_are_validated_and_the_key_is_write_only(client):
    _, tok = _admin()
    h = bearer(tok)
    assert client.put("/api/v1/settings", headers=h, json={"relay_url": "http://relay.example.test"}).status_code == 422
    assert client.put("/api/v1/settings", headers=h, json={"relay_url": "https://r.example.test/x"}).status_code == 422
    assert client.put("/api/v1/settings", headers=h, json={"relay_key": "nope"}).status_code == 422
    r = client.put("/api/v1/settings", headers=h, json={"relay_url": URL, "relay_key": KEY})
    assert r.status_code == 200
    item = {s["key"]: s for s in r.json["settings"]}["relay_key"]
    assert item["value"] is None and item["is_set"] is True
    assert KEY not in repr(client.get("/api/v1/settings", headers=h).json)


def test_saving_the_relay_registers_every_phone_now(client, monkeypatch):
    f = FakeRelay(free=2)
    monkeypatch.setattr(relay.requests, "request", f)
    _, tok = _admin()
    for n in range(3):   # paired while no relay was configured: straight-to-Expo phones
        _phone(client, tok, n=n, name=f"phone{n}")
    assert not f.calls
    r = client.put("/api/v1/settings", headers=bearer(tok), json={"relay_url": URL, "relay_key": KEY})
    assert r.json["relay_phones"] == {"registered": 2, "refused": 1, "error": 0}
    states = sorted(d.relay_status for d in SessionLocal.scalars(select(Device)).all())
    assert states == ["refused", "registered", "registered"]


# --- SHOR-10: unused phones ------------------------------------------------------------------

def test_a_phone_signed_out_for_going_unused_waits_for_the_phone(client, fake):
    _, tok = _admin()
    dev_tok, reg = _phone(client, tok)
    fake.push_reply = _Resp(410, {"message": "signed out", "errors": {"code": "signed_out", "reason": "stale"}})
    fake.devices.clear()
    with pytest.raises(delivery.DeliveryError, match="opening the Shorthop app"):
        delivery.send_push(SessionLocal, reg.json["id"], _Inc(), "triggered")
    fake.push_reply = None
    n = len(fake.calls)
    # A later page does NOT sign it back in (that would bill a phone in a drawer) ...
    with pytest.raises(delivery.DeliveryError, match="opening the Shorthop app"):
        delivery.send_push(SessionLocal, reg.json["id"], _Inc(), "triggered")
    assert len(fake.calls) == n
    # ... the phone itself opening the app does.
    again = client.put("/api/v1/devices/me/push", headers=bearer(dev_tok), json={"push_token": push_token(1)})
    assert again.json["relay_status"] == "registered"


def _housekeep(fake, remote):
    sent = []
    fake_list = _Resp(200, {"devices": remote})

    orig = fake.__call__

    def call(method, url, json=None, headers=None, **kw):
        if method == "GET" and url.endswith("/v1/devices"):
            fake.calls.append((method, "/v1/devices", None))
            return fake_list
        return orig(method, url, json=json, headers=headers, **kw)
    return call, sent


def test_housekeeping_reports_check_ins_and_emails_a_warning_once(client, fake, monkeypatch):
    owner, tok = _admin()
    _, reg = _phone(client, tok)
    rid = next(iter(fake.devices))
    remote = [{"id": rid, "stale_warned_at": "2026-10-01T09:00:00", "signs_out_at": "2026-10-31T09:00:00"}]
    call, _ = _housekeep(fake, remote)
    monkeypatch.setattr(relay.requests, "request", call)
    notes = []
    out = relay.housekeeping(SessionLocal, lambda dev, kind, when: notes.append((dev.name, kind, when)))
    assert out["reported"] == 1 and out["warned"] == 1
    seen = [c for c in fake.calls if c[1] == "/v1/devices/seen"][0][2]["devices"]
    assert seen[0]["id"] == rid
    assert notes[0][:2] == ("pixel", "warning") and notes[0][2].day == 31
    relay.housekeeping(SessionLocal, lambda dev, kind, when: notes.append(kind))
    assert len(notes) == 1          # told once per warning
    listed = client.get("/api/v1/devices", headers=bearer(tok)).json[0]
    assert listed["relay_signs_out_at"].startswith("2026-10-31")


def test_housekeeping_notices_a_sign_out_once(client, fake, monkeypatch):
    _, tok = _admin()
    _, reg = _phone(client, tok)
    call, _ = _housekeep(fake, [])          # the relay no longer lists the phone
    monkeypatch.setattr(relay.requests, "request", call)
    notes = []
    relay.housekeeping(SessionLocal, lambda dev, kind, when: notes.append(kind))
    relay.housekeeping(SessionLocal, lambda dev, kind, when: notes.append(kind))
    assert notes == ["signed_out"]
    assert SessionLocal.get(Device, reg.json["id"]).relay_status == "signed_out"


def test_the_unused_phone_email_goes_to_the_owner_and_admins(client, monkeypatch):
    from app import scheduler
    admin = make_user(email="boss@example.com", role="admin")
    ann = make_user(email="ann@example.com", role="responder")
    make_user(email="off@example.com", role="admin", disabled=True)
    dev = Device(user_id=ann.id, name="Ann's Pixel", platform="android", token_hash="h" * 64, prefix="shd_x")
    SessionLocal.add(dev)
    SessionLocal.commit()
    mails = []
    monkeypatch.setattr(delivery, "send_email", lambda to, subj, body: mails.append((to, subj)))
    import datetime as dt
    scheduler.notify_unused_phone(dev, "warning", dt.datetime(2026, 10, 31, tzinfo=dt.timezone.utc))
    assert sorted(m[0] for m in mails) == ["ann@example.com", "boss@example.com"]
    assert "Sat 31 Oct 2026" in mails[0][1]
    del admin


# --- SHOR-14: the account page link ----------------------------------------------------------

def test_relay_status_shows_seats_and_the_account_page(client, fake):
    _, tok = _admin()
    _phone(client, tok)
    r = client.get("/api/v1/settings/relay/status", headers=bearer(tok)).json
    assert r["ok"] is True and r["name"] == "Acme"
    assert (r["signed_in"], r["free"], r["card_on_file"]) == (1, 3, False)
    assert r["account_url"] == "https://account.example.test/account"


def test_relay_status_never_passes_on_a_non_https_link(client, fake):
    """The URL becomes an <a href> in the admin UI: a relay answering `javascript:` is dropped."""
    _, tok = _admin()
    fake.account_url = "javascript:alert(1)"
    assert client.get("/api/v1/settings/relay/status", headers=bearer(tok)).json["account_url"] is None


def test_relay_status_says_why_when_the_relay_cannot_be_asked(client, monkeypatch):
    _, tok = _admin()
    h = bearer(tok)
    assert client.get("/api/v1/settings/relay/status", headers=h).json == \
        {"configured": False, "ok": False, "error": None}
    st.put({"relay_url": URL, "relay_key": KEY}, "test")
    SessionLocal.commit()
    monkeypatch.setattr(relay.requests, "request", lambda *a, **k: _Resp(401, {}))
    r = client.get("/api/v1/settings/relay/status", headers=h).json
    assert r["ok"] is False and "rejected this install's key" in r["error"]


def test_relay_status_is_admin_only(client, fake):
    u = make_user(email="viewer@example.com", role="viewer")
    assert client.get("/api/v1/settings/relay/status",
                      headers=bearer(make_token(u, ["alerts:read"]))).status_code == 403
