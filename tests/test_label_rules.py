"""Label rules: add/change labels on incoming alerts from labels and alert text."""
import datetime as dt

import pytest
from sqlalchemy import select

from app.core import enrich
from app.db import SessionLocal
from app.models import Incident, Occurrence
from tests.conftest import bearer, make_integration, make_token, make_user
from tests.test_ingest import PROM, _prom_alert


def _rule(name, pos, set_labels, matchers=(), text=None, field=None, stop=False, **kw):
    return enrich.RuleView(name, name, pos, list(matchers), text, field, set_labels, stop=stop, **kw)


SEV_WARNING = {"name": "severity", "value": "warning", "isRegex": False, "isEqual": True}


# --- pure ------------------------------------------------------------------------------------

def test_later_rule_overrides_earlier_one():
    rules = [_rule("warn->P2", 10, {"level": "P2"}, [SEV_WARNING]),
             _rule("silent socket->P1", 20, {"level": "P1"}, [SEV_WARNING], text="silent socket")]
    plain = enrich.apply(rules, {"alertname": "X", "severity": "warning"}, {"summary": "disk full"}, "i")
    assert plain.labels["level"] == "P2" and plain.set_by == {"level": "warn->P2"}
    hit = enrich.apply(rules, {"alertname": "X", "severity": "warning"},
                       {"description": "AIS feed: Silent Socket for 15 min"}, "i")
    assert hit.labels["level"] == "P1" and hit.set_by == {"level": "silent socket->P1"}
    assert hit.applied == ["warn->P2", "silent socket->P1"]
    crit = enrich.apply(rules, {"alertname": "X", "severity": "critical"},
                        {"summary": "silent socket"}, "i")
    assert "level" not in crit.labels


def test_stop_and_chaining_and_text_field():
    rules = [_rule("a", 10, {"team": "db"}, text="mysql", stop=True),
             _rule("b", 20, {"team": "other"})]
    assert enrich.apply(rules, {"alertname": "X"}, {"summary": "MySQL down"}, "i").labels["team"] == "db"
    # a rule sees what the rules above it set
    chain = [_rule("a", 10, {"level": "P1"}), _rule("b", 20, {"page": "yes"},
             [{"name": "level", "value": "P1", "isRegex": False, "isEqual": True}])]
    assert enrich.apply(chain, {"alertname": "X"}, {}, "i").labels["page"] == "yes"
    # text_field limits the search to one annotation
    only = [_rule("a", 10, {"x": "1"}, text="boom", field="summary")]
    assert "x" not in enrich.apply(only, {"alertname": "X"}, {"description": "boom"}, "i").labels
    assert enrich.apply(only, {"alertname": "X"}, {"summary": "BOOM"}, "i").labels["x"] == "1"


def test_validation():
    with pytest.raises(enrich.RuleError):
        enrich.validate_set_labels({"alertname": "renamed"})
    with pytest.raises(enrich.RuleError):
        enrich.validate_set_labels({})
    with pytest.raises(enrich.RuleError):
        enrich.validate_text("(unclosed", None)
    with pytest.raises(enrich.RuleError):
        enrich.validate_set_labels({"level": "P1\nX-Injected: 1"})


# --- through the API and ingest --------------------------------------------------------------

@pytest.fixture()
def admin(client):
    return make_token(make_user(email="boss@example.com", role="admin"), ["admin"])


def _occ():
    SessionLocal.expire_all()
    return SessionLocal.scalars(select(Occurrence)).all()


def test_ingest_applies_rules_and_identity_ignores_them(client, admin):
    for body in ({"name": "warn->P2", "matchers": [SEV_WARNING], "set_labels": {"level": "P2"}},
                 {"name": "socket->P1", "matchers": [SEV_WARNING], "text_pattern": "silent socket",
                  "set_labels": {"level": "P1"}}):
        assert client.post("/api/v1/label-rules", headers=bearer(admin), json=body).status_code == 201
    _, itok = make_integration("prometheus")
    a = _prom_alert(severity="warning")
    a["annotations"] = {"summary": "AIS silent socket"}
    client.post(PROM, json=[a], headers=bearer(itok))
    (occ,) = _occ()
    assert occ.labels["level"] == "P1" and occ.rule_labels == {"level": "socket->P1"}
    fp, sig = occ.fingerprint, occ.signature
    assert any(e.kind == "labels" for e in occ.events)

    # The text changes on the next send: the label follows, the alert stays the SAME alert.
    a["annotations"] = {"summary": "AIS feed lagging"}
    client.post(PROM, json=[a], headers=bearer(itok))
    (occ,) = _occ()
    assert occ.labels["level"] == "P2"
    assert (occ.fingerprint, occ.signature) == (fp, sig)

    # Deleting every rule: the next send shows the labels as sent, still one alert.
    for r in client.get("/api/v1/label-rules", headers=bearer(admin)).json:
        client.delete(f"/api/v1/label-rules/{r['id']}", headers=bearer(admin))
    client.post(PROM, json=[a], headers=bearer(itok))
    (occ,) = _occ()
    assert "level" not in occ.labels and not occ.rule_labels and occ.fingerprint == fp


def test_routing_sees_rule_labels(client, admin):
    pol = client.post("/api/v1/escalation-policies", headers=bearer(admin), json={
        "name": "p", "steps": [{"delay_minutes": 0, "targets": [
            {"type": "user", "id": make_user(email="ann@example.com").id}]}]}).json
    client.post("/api/v1/routes", headers=bearer(admin), json={
        "name": "P1 pages", "escalation_policy_id": pol["id"],
        "matchers": [{"name": "level", "value": "P1", "isRegex": False, "isEqual": True}]})
    client.post("/api/v1/label-rules", headers=bearer(admin), json={
        "name": "socket->P1", "text_pattern": "silent socket", "set_labels": {"level": "P1"}})
    _, itok = make_integration("prometheus")
    a = _prom_alert()
    a["annotations"] = {"summary": "silent socket"}
    client.post(PROM, json=[a], headers=bearer(itok))
    SessionLocal.expire_all()
    (inc,) = SessionLocal.scalars(select(Incident)).all()
    assert inc.escalation_policy_id == pol["id"]


def test_api_validation_order_and_tester(client, admin):
    bad = [{"name": "x", "set_labels": {}},
           {"name": "x", "set_labels": {"alertname": "y"}},
           {"name": "x", "set_labels": {"a": "b"}, "text_pattern": "("},
           {"name": "x", "set_labels": {"a": "b"}, "matchers": [{"name": "bad name", "value": "v"}]}]
    for b in bad:
        assert client.post("/api/v1/label-rules", headers=bearer(admin), json=b).status_code == 422, b
    r1 = client.post("/api/v1/label-rules", headers=bearer(admin), json={
        "name": "one", "set_labels": {"level": "P2"}}).json
    r2 = client.post("/api/v1/label-rules", headers=bearer(admin), json={
        "name": "two", "set_labels": {"level": "P3"}}).json
    t = client.post("/api/v1/label-rules/test", headers=bearer(admin), json={
        "labels": {"alertname": "X"}, "annotations": {}}).json
    assert t["labels"]["level"] == "P3" and [x["applied"] for x in t["rules"]] == [True, True]
    assert "routing" in t
    client.put("/api/v1/label-rules/order", headers=bearer(admin), json={"ids": [r2["id"], r1["id"]]})
    t = client.post("/api/v1/label-rules/test", headers=bearer(admin), json={"labels": {"alertname": "X"}}).json
    assert t["labels"]["level"] == "P2"
    responder = make_token(make_user(email="r@example.com"), ["alerts:read", "oncall:read"])
    assert client.post("/api/v1/label-rules", headers=bearer(responder),
                       json={"name": "x", "set_labels": {"a": "b"}}).status_code == 403
