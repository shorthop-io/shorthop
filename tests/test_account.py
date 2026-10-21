"""My account: profile and how I'm paged (contact rules)."""
import datetime as dt

import pytest
from sqlalchemy import select

from app import delivery, incidents
from app.core import contact
from app.db import SessionLocal
from app.models import Incident, Notification, now
from tests.conftest import bearer, make_token, make_user
from tests.test_ingest import PROM, _prom_alert
from tests.test_paging import outbox, setup  # noqa: F401  (fixtures)


def test_rules_pure():
    with pytest.raises(contact.RuleError):
        contact.validate([])
    with pytest.raises(contact.RuleError):
        contact.validate([{"method": "sms"}])
    with pytest.raises(contact.RuleError):
        contact.validate([{"method": "push", "delay_minutes": 999}])
    rules = contact.validate([{"method": "email", "delay_minutes": 5},
                              {"method": "push", "delay_minutes": 0},
                              {"method": "push", "delay_minutes": 0}])  # duplicate collapses
    assert rules == [{"method": "push", "delay_minutes": 0, "severities": None},
                     {"method": "email", "delay_minutes": 5, "severities": None}]
    crit = contact.validate([{"method": "push", "delay_minutes": 0, "severities": ["Critical"]}])
    assert contact.applicable(crit, "critical") and not contact.applicable(crit, "warning")
    assert contact.immediate_gap(crit) and not contact.immediate_gap(rules)
    assert contact.applicable(None, "x") == contact.DEFAULT_RULES


def test_profile_update_and_validation(client):
    tok = make_token(make_user(email="ann@example.com"), ["alerts:read"])
    me = client.get("/api/v1/me", headers=bearer(tok)).json
    assert me["contact_rules_default"] is True and me["contact_rules"] == contact.DEFAULT_RULES
    r = client.put("/api/v1/me", headers=bearer(tok),
                   json={"name": "Ann", "timezone": "Europe/Stockholm", "phone": "+46 70 123 45 67"})
    assert r.status_code == 200 and r.json["timezone"] == "Europe/Stockholm" and r.json["name"] == "Ann"
    assert client.put("/api/v1/me", headers=bearer(tok), json={"timezone": "Mars/Base"}).status_code == 422
    assert client.put("/api/v1/me", headers=bearer(tok), json={"phone": "call me"}).status_code == 422
    # role is not yours to change
    client.put("/api/v1/me", headers=bearer(tok), json={"role": "admin"})
    assert client.get("/api/v1/me", headers=bearer(tok)).json["role"] == "responder"
    r = client.put("/api/v1/me/contact-rules", headers=bearer(tok),
                   json={"rules": [{"method": "email", "delay_minutes": 10}]})
    assert r.json["contact_rules_default"] is False and r.json["contact_gap"] is True
    r = client.put("/api/v1/me/contact-rules", headers=bearer(tok), json={"rules": None})
    assert r.json["contact_rules_default"] is True
    assert client.put("/api/v1/me/contact-rules", headers=bearer(tok), json={"rules": []}).status_code == 422


def _notes():
    SessionLocal.expire_all()
    return SessionLocal.scalars(select(Notification).order_by(Notification.created_at)).all()


def test_delayed_email_goes_only_if_nobody_acknowledged(client, setup, outbox):  # noqa: F811
    ann = setup["ann"]
    ann_tok = make_token(ann, ["alerts:read", "alerts:write"])
    client.put("/api/v1/me/contact-rules", headers=bearer(ann_tok), json={"rules": [
        {"method": "push", "delay_minutes": 0}, {"method": "email", "delay_minutes": 5}]})
    client.post(PROM, json=[_prom_alert(pod="a")], headers=bearer(setup["itok"]))
    incidents.run_due()
    delivery.deliver_pending()
    (n,) = [x for x in _notes() if "ann" in x.target]  # ann has no phone, so: the email only
    assert n.until_ack and n.status == "pending" and "after 5 min" in n.target
    assert outbox["email"] == []                       # not yet — it is 5 minutes away
    # time passes, nobody acknowledged: the email goes
    n.next_attempt_at = now()
    SessionLocal.commit()
    delivery.deliver_pending()
    assert [to for to, *_ in outbox["email"]] == ["ann@example.com"]


def test_delayed_page_dropped_when_acknowledged_first(client, setup, outbox):  # noqa: F811
    ann_tok = make_token(setup["ann"], ["alerts:read", "alerts:write"])
    client.put("/api/v1/me/contact-rules", headers=bearer(ann_tok),
               json={"rules": [{"method": "email", "delay_minutes": 5}]})
    client.post(PROM, json=[_prom_alert(pod="a")], headers=bearer(setup["itok"]))
    incidents.run_due()
    inc = SessionLocal.scalars(select(Incident)).one()
    client.post(f"/api/v1/incidents/{inc.id}/ack", headers=bearer(ann_tok))
    for n in _notes():
        n.next_attempt_at = now()
    SessionLocal.commit()
    delivery.deliver_pending()
    (n,) = _notes()
    assert n.status == "suppressed" and "acknowledged by" in n.error
    assert outbox["email"] == []
    # and resolving does not send a "resolved" for a page that never went out
    client.post(PROM, json=[_prom_alert(pod="a", ends_in=dt.timedelta(seconds=-5))],
                headers=bearer(setup["itok"]))
    delivery.deliver_pending()
    assert outbox["email"] == []


def test_severity_filtered_rule(client, setup, outbox):  # noqa: F811
    ann_tok = make_token(setup["ann"], ["alerts:read"])
    client.put("/api/v1/me/contact-rules", headers=bearer(ann_tok),
               json={"rules": [{"method": "email", "delay_minutes": 0, "severities": ["critical"]}]})
    client.post(PROM, json=[_prom_alert(pod="a", severity="warning")], headers=bearer(setup["itok"]))
    incidents.run_due()
    delivery.deliver_pending()
    assert "ann@example.com" not in [to for to, *_ in outbox["email"]]
