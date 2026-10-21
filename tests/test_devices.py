import pytest
from sqlalchemy import select

from app import delivery, incidents
from app.db import SessionLocal
from app.models import Device, Notification
from tests.conftest import bearer, make_integration, make_token, make_user
from tests.test_ingest import PROM, _prom_alert

PUSH = "ExponentPushToken[abcdefghijklmnop]"


def _pair(client, user_tok, **body):
    code = client.post("/api/v1/devices/pair-codes", headers=bearer(user_tok)).json
    r = client.post("/api/v1/devices/pair", json={"code": code["code"], "name": "pixel", **body})
    return code, r


def test_pairing_gives_a_device_token_once(client):
    admin = make_user(email="boss@example.com", role="admin")
    tok = make_token(admin, ["admin"])
    code, r = _pair(client, tok)
    assert r.status_code == 200 and r.json["token"].startswith("shd_")
    assert code["link"].startswith("shorthop://pair?url=")
    again = client.post("/api/v1/devices/pair", json={"code": code["code"]})
    assert again.status_code == 400                      # single use
    dev = bearer(r.json["token"])
    assert client.get("/api/v1/incidents", headers=dev).status_code == 200
    # a phone never gets config rights, even when its owner is an admin
    assert client.post("/api/v1/routes", headers=dev, json={"name": "x"}).status_code == 403
    assert client.post("/api/v1/devices/pair-codes", headers=dev).status_code == 403


def test_wrong_code_and_rate_limit(client, monkeypatch):
    from app.api import devices as d
    monkeypatch.setattr(d, "_PAIR_LIMIT", 3)
    for _ in range(3):
        assert client.post("/api/v1/devices/pair", json={"code": "AAAA-BBBB"}).status_code == 400
    assert client.post("/api/v1/devices/pair", json={"code": "AAAA-BBBB"}).status_code == 429


def test_push_token_validation_and_routing(client, monkeypatch):
    sent = []
    monkeypatch.setattr(delivery, "send_email", lambda *a: None)
    monkeypatch.setattr(delivery, "send_push", lambda db, did, inc, ev: sent.append((did, ev)))
    admin = make_user(email="boss@example.com", role="admin")
    tok = make_token(admin, ["admin"])
    _, r = _pair(client, tok)
    dev = bearer(r.json["token"])
    assert client.put("/api/v1/devices/me/push", headers=dev, json={"push_token": "nope"}).status_code == 422
    assert client.put("/api/v1/devices/me/push", headers=dev, json={"push_token": PUSH}).json["has_push"]
    assert client.put("/api/v1/devices/me/push", headers=bearer(tok), json={"push_token": PUSH}).status_code == 403

    pol = client.post("/api/v1/escalation-policies", headers=bearer(tok), json={
        "name": "p", "steps": [{"targets": [{"type": "user", "id": admin.id}]}]}).json
    client.post("/api/v1/routes", headers=bearer(tok), json={"name": "all", "escalation_policy_id": pol["id"]})
    _, itok = make_integration("prometheus")
    client.post(PROM, json=[_prom_alert()], headers=bearer(itok))
    incidents.run_due()
    delivery.deliver_pending()
    kinds = sorted(n.kind for n in SessionLocal.scalars(select(Notification)).all())
    assert kinds == ["email", "push"] and sent and sent[0][1] == "triggered"


def test_device_not_registered_drops_the_push_token(client, monkeypatch):
    admin = make_user(email="boss@example.com", role="admin")
    tok = make_token(admin, ["admin"])
    _, r = _pair(client, tok)
    client.put("/api/v1/devices/me/push", headers=bearer(r.json["token"]), json={"push_token": PUSH})

    class Resp:
        status_code = 200

        def json(self):
            return {"data": {"status": "error", "details": {"error": "DeviceNotRegistered"}}}
    monkeypatch.setattr(delivery.requests, "post", lambda *a, **k: Resp())
    dev = SessionLocal.scalar(select(Device))

    class Inc:
        number, title, severity, alerts, id = 1, "X", "", [], "i"
        reassigned_at = None
    monkeypatch.setattr(delivery, "_history_line", lambda inc: "")
    with pytest.raises(delivery.DeliveryError, match="DeviceNotRegistered"):
        delivery.send_push(SessionLocal, dev.id, Inc(), "triggered")
    assert SessionLocal.get(Device, dev.id).push_token is None


def test_revoked_phone_is_locked_out(client):
    admin = make_user(email="boss@example.com", role="admin")
    tok = make_token(admin, ["admin"])
    _, r = _pair(client, tok)
    dev_id = r.json["device"]["id"]
    assert client.delete(f"/api/v1/devices/{dev_id}", headers=bearer(tok)).json["revoked"]
    assert client.get("/api/v1/incidents", headers=bearer(r.json["token"])).status_code == 401
