import json

from app.models import AuditEntry
from app.db import SessionLocal
from tests.conftest import bearer, make_token, make_user
from tests.test_api import _fire


def _rpc(client, token, method, params=None, msg_id=1):
    body = {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}}
    return client.post("/mcp", json=body, headers=bearer(token))


def _tool(client, token, name, **args):
    r = _rpc(client, token, "tools/call", {"name": name, "arguments": args})
    result = r.json["result"]
    return result, json.loads(result["content"][0]["text"]) if not result["isError"] else None


def test_admin_and_config_tokens_are_refused(client):
    admin = make_token(make_user(email="a@x.io", role="admin"), ["admin"])
    assert _rpc(client, admin, "tools/list").status_code == 403
    cfgtok = make_token(make_user(email="c@x.io", role="admin"), ["alerts:read", "config:write"])
    assert _rpc(client, cfgtok, "tools/list").status_code == 403


def test_read_token_sees_only_read_tools(client):
    tok = make_token(make_user(), ["alerts:read"])
    names = {t["name"] for t in _rpc(client, tok, "tools/list").json["result"]["tools"]}
    assert "list_alerts" in names and "resolve_alert" not in names


def test_initialize_and_session_cookie_ignored(client):
    tok = make_token(make_user(), ["alerts:read"])
    r = _rpc(client, tok, "initialize", {"protocolVersion": "2025-06-18"})
    assert r.json["result"]["serverInfo"]["name"] == "shorthop"
    assert client.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                                     "method": "tools/list"}).status_code == 401


def test_resolve_through_mcp_records_fix_and_audits(client):
    tok = make_token(make_user(), ["alerts:read", "alerts:write"])
    _fire(client)
    _, alerts = _tool(client, tok, "list_alerts")
    aid = alerts[0]["id"]
    result, body = _tool(client, tok, "resolve_alert", alert_id=aid, category="fixed",
                         note="bumped the memory limit")
    assert not result["isError"] and body["resolution_note"] == "bumped the memory limit"
    actions = [a.action for a in SessionLocal.query(AuditEntry).all()]
    assert "mcp.resolve_alert" in actions


def test_mcp_silence_is_capped(client):
    tok = make_token(make_user(), ["alerts:read", "alerts:write"])
    result, _ = _tool(client, tok, "create_silence",
                      matchers=[{"name": "alertname", "value": "X"}],
                      duration_minutes=60 * 48, comment="too long")
    assert result["isError"]
    result, body = _tool(client, tok, "create_silence",
                         matchers=[{"name": "alertname", "value": "X"}],
                         duration_minutes=30, comment="deploying")
    assert not result["isError"] and body["state"] == "active"


def test_unknown_argument_is_refused(client):
    tok = make_token(make_user(), ["alerts:read"])
    result, _ = _tool(client, tok, "list_alerts", statuss="open")
    assert result["isError"] and "unknown argument" in result["content"][0]["text"]


def test_write_tool_refused_for_read_token_even_if_called(client):
    tok = make_token(make_user(), ["alerts:read"])
    result, _ = _tool(client, tok, "acknowledge_alert", alert_id="x")
    assert result["isError"] and "alerts:write" in result["content"][0]["text"]


def test_incident_tools(client):
    tok = make_token(make_user(email="r@x.io"), ["alerts:read", "alerts:write", "oncall:read"])
    admin = make_token(make_user(email="a2@x.io", role="admin"), ["admin"])
    client.post("/api/v1/routes", headers=bearer(admin), json={"name": "all"})
    _fire(client)
    _, rows = _tool(client, tok, "list_incidents")
    assert len(rows) == 1
    result, body = _tool(client, tok, "acknowledge_incident", incident_id=rows[0]["id"])
    assert not result["isError"] and body["status"] == "acknowledged"
    _, routing = _tool(client, tok, "test_routing", labels={"alertname": "X"})
    assert routing["routes"][0]["matched"] is True


def test_oncall_tools(client):
    import datetime as dt
    from app.models import now as _now
    admin = make_token(make_user(email="a3@x.io", role="admin"), ["admin"])
    ann = make_user(email="ann3@x.io")
    s = client.post("/api/v1/schedules", headers=bearer(admin), json={
        "name": "primary", "timezone": "UTC", "layers": [{"users": [ann.id], "rotation_hours": 24,
        "start": (_now() - dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")}]}).json
    bob = make_user(email="bob3@x.io")
    tok = make_token(bob, ["alerts:read", "oncall:read", "oncall:write"])
    _, rows = _tool(client, tok, "who_is_on_call")
    assert rows[0]["user"]["email"] == "ann3@x.io"
    result, body = _tool(client, tok, "take_shift", schedule_id=s["id"], hours=2, reason="covering")
    assert not result["isError"] and body["user"]["email"] == "bob3@x.io"
    _, rows = _tool(client, tok, "who_is_on_call")
    assert rows[0]["user"]["email"] == "bob3@x.io" and rows[0]["source"] == "override"
    _, sched = _tool(client, tok, "get_schedule", schedule_id=s["id"], days=3)
    assert sched["shifts"][0]["user"]["email"] == "bob3@x.io"


def test_incident_note_as_suggestion_is_marked_apart(client):
    tok = make_token(make_user(email="ai@x.io"), ["alerts:read", "alerts:write"])
    admin = make_token(make_user(email="a3@x.io", role="admin"), ["admin"])
    client.post("/api/v1/routes", headers=bearer(admin), json={"name": "all"})
    _fire(client)
    _, rows = _tool(client, tok, "list_incidents")
    iid = rows[0]["id"]
    result, _ = _tool(client, tok, "add_incident_note", incident_id=iid,
                      text="Disk full on node-3; rotate logs", suggestion=True)
    assert not result["isError"]
    result, _ = _tool(client, tok, "add_incident_note", incident_id=iid, text="looking")
    assert not result["isError"]
    _, alerts = _tool(client, tok, "list_alerts")
    events = client.get(f"/api/v1/alerts/{alerts[0]['id']}", headers=bearer(tok)).json["events"]
    kinds = {e["detail"]: e["kind"] for e in events if e["kind"] in ("note", "suggestion")}
    assert kinds == {"Disk full on node-3; rotate logs": "suggestion", "looking": "note"}
    # The string "false" must not be read as true.
    result, _ = _tool(client, tok, "add_incident_note", incident_id=iid, text="x", suggestion="false")
    assert result["isError"]
    assert "mcp.add_incident_note" in [a.action for a in SessionLocal.query(AuditEntry).all()]


def test_rest_note_kind_is_validated(client):
    tok = make_token(make_user(email="r2@x.io"), ["alerts:read", "alerts:write"])
    _fire(client)
    aid = client.get("/api/v1/alerts", headers=bearer(tok)).json[0]["id"]
    r = client.post(f"/api/v1/alerts/{aid}/notes", headers=bearer(tok),
                    json={"text": "guess", "kind": "suggestion"})
    assert r.status_code == 200 and r.json["events"][-1]["kind"] == "suggestion"
    r = client.post(f"/api/v1/alerts/{aid}/notes", headers=bearer(tok),
                    json={"text": "x", "kind": "fix"})
    assert r.status_code == 422
