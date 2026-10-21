import datetime as dt

from sqlalchemy import select

from app.auth import hash_password
from app.db import SessionLocal
from app.models import now
from tests.conftest import bearer, make_integration, make_token, make_user
from tests.test_ingest import PROM, _iso, _prom_alert


def _fire(client, **kw):
    _, tok = make_integration("prometheus", name=f"p-{now().timestamp()}")
    client.post(PROM, json=[_prom_alert(**kw)], headers=bearer(tok))
    return tok


def _open_id(client, token):
    return client.get("/api/v1/alerts", headers=bearer(token)).json[0]["id"]


def test_list_ack_resolve_with_fix(client):
    user = make_user()
    tok = make_token(user, ["alerts:read", "alerts:write"])
    _fire(client)
    aid = _open_id(client, tok)

    r = client.post(f"/api/v1/alerts/{aid}/ack", headers=bearer(tok))
    assert r.status_code == 200 and r.json["status"] == "acknowledged"
    assert r.json["acked_by"] == user.email

    r = client.post(f"/api/v1/alerts/{aid}/resolve", headers=bearer(tok),
                    json={"category": "restarted", "note": "restarted the consumer"})
    assert r.json["status"] == "resolved" and r.json["resolved_how"] == "manual"
    kinds = [e["kind"] for e in r.json["events"]]
    assert kinds == ["fired", "acknowledged", "resolved"]


def test_history_shows_previous_fix_on_next_occurrence(client):
    tok = make_token(make_user(), ["alerts:read", "alerts:write"])
    prom = _fire(client, pod="api-1")
    aid = _open_id(client, tok)
    client.post(f"/api/v1/alerts/{aid}/resolve", headers=bearer(tok),
                json={"category": "restarted", "note": "restarted the consumer"})
    # the same fault on another pod, later
    client.post(PROM, json=[_prom_alert(pod="api-9")], headers=bearer(prom))
    new_id = _open_id(client, tok)
    assert new_id != aid
    detail = client.get(f"/api/v1/alerts/{new_id}", headers=bearer(tok)).json
    assert detail["history"]["total"] == 1
    assert detail["history"]["recent_fixes"][0]["resolution_note"] == "restarted the consumer"


def test_fix_can_be_recorded_after_the_sender_resolved(client):
    tok = make_token(make_user(), ["alerts:read", "alerts:write"])
    prom = _fire(client)
    aid = _open_id(client, tok)
    client.post(PROM, json=[_prom_alert(ends_in=-dt.timedelta(seconds=5))], headers=bearer(prom))
    r = client.post(f"/api/v1/alerts/{aid}/resolve", headers=bearer(tok),
                    json={"category": "scaled", "note": "added a replica"})
    assert r.json["resolved_how"] == "source"
    assert r.json["resolution_note"] == "added a replica"


def test_unknown_resolution_category_is_422(client):
    tok = make_token(make_user(), ["alerts:read", "alerts:write"])
    _fire(client)
    aid = _open_id(client, tok)
    r = client.post(f"/api/v1/alerts/{aid}/resolve", headers=bearer(tok),
                    json={"category": "magic"})
    assert r.status_code == 422


def test_read_only_token_cannot_ack(client):
    tok = make_token(make_user(), ["alerts:read"])
    _fire(client)
    aid = _open_id(client, tok)
    assert client.post(f"/api/v1/alerts/{aid}/ack", headers=bearer(tok)).status_code == 403


def test_token_narrowed_when_user_is_demoted(client):
    user = make_user(role="responder")
    tok = make_token(user, ["alerts:read", "alerts:write"])
    _fire(client)
    aid = _open_id(client, tok)
    from app.models import User
    SessionLocal.get(User, user.id).role = "viewer"
    SessionLocal.commit()
    assert client.post(f"/api/v1/alerts/{aid}/ack", headers=bearer(tok)).status_code == 403
    SessionLocal.get(User, user.id).disabled = True
    SessionLocal.commit()
    assert client.get("/api/v1/alerts", headers=bearer(tok)).status_code == 401


def test_cannot_mint_token_beyond_own_role(client):
    tok = make_token(make_user(role="viewer"), ["alerts:read"])
    r = client.post("/api/v1/tokens", headers=bearer(tok),
                    json={"name": "x", "scopes": ["alerts:write"]})
    assert r.status_code == 403
    r = client.post("/api/v1/tokens", headers=bearer(tok),
                    json={"name": "x", "scopes": ["alerts:read"]})
    assert r.status_code == 201 and r.json["token"].startswith("sh_")


def test_silence_preview_create_mark_and_expire(client):
    tok = make_token(make_user(), ["alerts:read", "alerts:write"])
    _fire(client)
    m = [{"name": "alertname", "value": "CrashLoop"}]
    preview = client.post("/api/v1/silences/preview", headers=bearer(tok), json={"matchers": m})
    assert len(preview.json) == 1

    r = client.post("/api/v1/silences", headers=bearer(tok), json={
        "matchers": m, "ends_at": _iso(now() + dt.timedelta(hours=1)),
        "comment": "deploying the fix"})
    assert r.status_code == 201 and r.json["state"] == "active"
    sid = r.json["id"]
    alerts = client.get("/api/v1/alerts", headers=bearer(tok)).json
    assert alerts[0]["silenced_by"] == [sid]

    r = client.delete(f"/api/v1/silences/{sid}", headers=bearer(tok))
    assert r.json["state"] == "expired"
    assert client.get("/api/v1/alerts", headers=bearer(tok)).json[0]["silenced_by"] == []


def test_silence_needs_comment_and_sane_window(client):
    tok = make_token(make_user(), ["alerts:read", "alerts:write"])
    m = [{"name": "alertname", "value": "X"}]
    r = client.post("/api/v1/silences", headers=bearer(tok), json={
        "matchers": m, "ends_at": _iso(now() + dt.timedelta(days=90)), "comment": "x"})
    assert r.status_code == 409
    r = client.post("/api/v1/silences", headers=bearer(tok), json={
        "matchers": [{"name": "alertname", "value": ".*", "isRegex": True}],
        "ends_at": _iso(now() + dt.timedelta(hours=1)), "comment": "x"})
    assert r.status_code == 409 and "every alert" in r.json["message"]


def test_integration_token_shown_once_and_admin_only(client):
    admin = make_token(make_user(email="boss@example.com", role="admin"), ["admin"])
    responder = make_token(make_user(), ["alerts:read", "alerts:write"])
    body = {"name": "big", "kind": "prometheus", "extra_labels": {"cluster": "big"}}
    assert client.post("/api/v1/integrations", headers=bearer(responder),
                       json=body).status_code == 403
    r = client.post("/api/v1/integrations", headers=bearer(admin), json=body)
    assert r.status_code == 201 and r.json["token"].startswith("shi_")
    listed = client.get("/api/v1/integrations", headers=bearer(admin)).json
    assert "token" not in listed[0]


def test_password_login_and_csrf_on_session_writes(client):
    user = make_user(password_hash=hash_password("correct horse battery"))
    _fire(client)
    r = client.post("/login", data={"email": user.email, "password": "wrong", "next": "/"})
    assert r.status_code == 401
    r = client.post("/login", data={"email": user.email, "password": "correct horse battery",
                                    "next": "//evil.example"})
    assert r.status_code == 302 and r.headers["Location"] == "/"

    me = client.get("/api/v1/me").json
    aid = client.get("/api/v1/alerts").json[0]["id"]
    assert client.post(f"/api/v1/alerts/{aid}/ack").status_code == 401  # no CSRF header
    r = client.post(f"/api/v1/alerts/{aid}/ack", headers={"X-CSRF-Token": me["csrf_token"]})
    assert r.status_code == 200


def test_unknown_alert_is_404(client):
    tok = make_token(make_user(), ["alerts:read"])
    assert client.get("/api/v1/alerts/nope", headers=bearer(tok)).status_code == 404


def test_delete_integration_without_alerts_removes_it(client):
    from app.models import Integration
    admin = make_user(email="del@example.com", role="admin")
    tok = make_token(admin, ["admin"])
    r = client.post("/api/v1/integrations", headers=bearer(tok), json={"name": "tmp", "kind": "webhook"})
    iid = r.json["id"]
    assert client.delete(f"/api/v1/integrations/{iid}", headers=bearer(tok)).json == {
        "deleted": True, "archived": False}
    SessionLocal.expire_all()
    assert SessionLocal.get(Integration, iid) is None


def test_delete_integration_with_alerts_archives_it_and_keeps_history(client):
    from app.models import Integration, Occurrence
    from tests.test_ingest import PROM, _prom_alert
    admin = make_user(email="del2@example.com", role="admin")
    tok = make_token(admin, ["admin"])
    r = client.post("/api/v1/integrations", headers=bearer(tok),
                    json={"name": "prom", "kind": "prometheus"})
    iid, itok = r.json["id"], r.json["token"]
    assert client.post(PROM, json=[_prom_alert()], headers=bearer(itok)).status_code < 300
    r = client.delete(f"/api/v1/integrations/{iid}", headers=bearer(tok))
    assert r.json == {"deleted": True, "archived": True}
    # the token is dead, it is gone from the list, the name is free, the alert remains
    assert client.post(PROM, json=[_prom_alert()], headers=bearer(itok)).status_code == 401
    assert all(i["id"] != iid for i in client.get("/api/v1/integrations", headers=bearer(tok)).json)
    assert any(i["id"] == iid for i in client.get("/api/v1/integrations?archived=1", headers=bearer(tok)).json)
    assert client.post("/api/v1/integrations", headers=bearer(tok),
                       json={"name": "prom", "kind": "prometheus"}).status_code == 201
    SessionLocal.expire_all()
    assert SessionLocal.scalar(select(Occurrence).where(Occurrence.integration_id == iid))
    assert client.patch(f"/api/v1/integrations/{iid}", headers=bearer(tok),
                        json={"enabled": True}).status_code == 404
    assert client.delete(f"/api/v1/integrations/{iid}", headers=bearer(tok)).status_code == 404


def test_openapi_offers_bearer_authorize(client):
    """Swagger's Authorize button takes a personal token — the Tokens page points there."""
    spec = client.get("/openapi.json").json
    assert spec["components"]["securitySchemes"]["bearerAuth"] == {
        **spec["components"]["securitySchemes"]["bearerAuth"], "type": "http", "scheme": "bearer"}
    assert spec["security"] == [{"bearerAuth": []}]
    assert client.get("/docs").status_code == 200
