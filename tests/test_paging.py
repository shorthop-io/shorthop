import datetime as dt
import hashlib
import hmac
import json

import pytest
from sqlalchemy import select

from app import alerts as alert_ops
from app import config as cfg
from app import delivery, incidents
from app.db import SessionLocal
from app.models import Incident, Notification, Occurrence, aware, now
from tests.conftest import bearer, make_integration, make_token, make_user
from tests.test_ingest import PROM, _iso, _prom_alert


@pytest.fixture()
def outbox(monkeypatch):
    """Capture what would be sent instead of sending it."""
    sent = {"email": [], "webhook": []}
    monkeypatch.setattr(delivery, "send_email",
                        lambda to, subject, body: sent["email"].append((to, subject, body)))
    monkeypatch.setattr(delivery, "send_webhook",
                        lambda config, body, secret: sent["webhook"].append((config["url"], body, secret)))
    return sent


@pytest.fixture()
def setup(client):
    admin = make_user(email="boss@example.com", role="admin")
    ann = make_user(email="ann@example.com")
    bob = make_user(email="bob@example.com")
    tok = make_token(admin, ["admin"])
    team = client.post("/api/v1/teams", headers=bearer(tok),
                       json={"slug": "platform", "name": "Platform"}).json
    for u in (ann, bob):
        client.post(f"/api/v1/teams/{team['id']}/members", headers=bearer(tok), json={"user_id": u.id})
    policy = client.post("/api/v1/escalation-policies", headers=bearer(tok), json={
        "name": "platform default", "steps": [
            {"delay_minutes": 0, "targets": [{"type": "user", "id": ann.id}]},
            {"delay_minutes": 5, "targets": [{"type": "team", "id": team["id"]}]},
        ]}).json
    route = client.post("/api/v1/routes", headers=bearer(tok), json={
        "name": "crashloops", "matchers": [{"name": "alertname", "value": "CrashLoop"}],
        "team_id": team["id"], "escalation_policy_id": policy["id"],
        "group_by": ["alertname", "namespace"]}).json
    integ, itok = make_integration("prometheus")
    return {"tok": tok, "itok": itok, "integ": integ, "team": team, "policy": policy,
            "route": route, "ann": ann, "bob": bob}


def _incidents():
    SessionLocal.expire_all()
    return SessionLocal.scalars(select(Incident).order_by(Incident.number)).all()


def _notes(event=None):
    SessionLocal.expire_all()
    q = select(Notification).order_by(Notification.created_at)
    if event:
        q = q.where(Notification.event == event)
    return SessionLocal.scalars(q).all()


def _due_now(inc_id):
    inc = SessionLocal.get(Incident, inc_id)
    inc.next_step_at = now() - dt.timedelta(seconds=1)
    SessionLocal.commit()


def test_alerts_with_same_group_labels_are_one_incident(client, setup):
    client.post(PROM, json=[_prom_alert(pod="a"), _prom_alert(pod="b")], headers=bearer(setup["itok"]))
    (inc,) = _incidents()
    assert len(inc.alerts) == 2 and inc.status == "triggered"
    assert inc.group_labels == {"alertname": "CrashLoop", "namespace": "service"}
    client.post(PROM, json=[_prom_alert(pod="c", namespace="other")], headers=bearer(setup["itok"]))
    assert len(_incidents()) == 2


def test_escalation_runs_step_by_step_until_acknowledged(client, setup, outbox):
    client.post(PROM, json=[_prom_alert(pod="a")], headers=bearer(setup["itok"]))
    incidents.run_due()
    delivery.deliver_pending()
    assert [to for to, *_ in outbox["email"]] == ["ann@example.com"]
    subject, body = outbox["email"][0][1], outbox["email"][0][2]
    assert subject.startswith("[FIRING] #1") and "First time this alert has fired." in body

    (inc,) = _incidents()
    incidents.run_due()                          # step 2 is not due for 5 min
    assert len(_notes()) == 1
    _due_now(inc.id)
    incidents.run_due()
    delivery.deliver_pending()
    assert sorted(to for to, *_ in outbox["email"][1:]) == ["ann@example.com", "bob@example.com"]
    assert outbox["email"][1][1].startswith("[STILL FIRING]")

    r = client.post(f"/api/v1/incidents/{inc.id}/ack", headers=bearer(setup["tok"]))
    assert r.json["status"] == "acknowledged" and r.json["next_step_at"] is None
    assert all(a["status"] == "acknowledged" for a in r.json["alerts"])


def test_acknowledging_one_alert_stops_the_incident_paging(client, setup, outbox):
    client.post(PROM, json=[_prom_alert(pod="a"), _prom_alert(pod="b")], headers=bearer(setup["itok"]))
    occ = SessionLocal.scalars(select(Occurrence)).first()
    alert_ops.acknowledge(occ.id, "ann@example.com")
    (inc,) = _incidents()
    assert inc.status == "acknowledged" and inc.next_step_at is None
    assert incidents.run_due() == 0


def test_resolve_closes_the_incident_and_tells_everyone_once(client, setup, outbox):
    client.post(PROM, json=[_prom_alert(pod="a"), _prom_alert(pod="b")], headers=bearer(setup["itok"]))
    incidents.run_due()
    (inc,) = _incidents()
    _due_now(inc.id)
    incidents.run_due()
    delivery.deliver_pending()
    gone = dt.timedelta(seconds=-5)
    client.post(PROM, json=[_prom_alert(pod="a", ends_in=gone)], headers=bearer(setup["itok"]))
    assert _incidents()[0].status == "triggered"       # one alert still open
    client.post(PROM, json=[_prom_alert(pod="b", ends_in=gone)], headers=bearer(setup["itok"]))
    (inc,) = _incidents()
    assert inc.status == "resolved"
    delivery.deliver_pending()
    resolved = [n.destination for n in _notes("resolved")]
    assert sorted(resolved) == ["ann@example.com", "bob@example.com"]


def test_silenced_incident_is_held_not_skipped(client, setup, outbox):
    alert_ops.create_silence([{"name": "alertname", "value": "CrashLoop"}], None,
                             now() + dt.timedelta(hours=1), "deploying", "boss@example.com")
    client.post(PROM, json=[_prom_alert(pod="a")], headers=bearer(setup["itok"]))
    incidents.run_due()
    (inc,) = _incidents()
    assert _notes() == [] and inc.next_step == 0 and aware(inc.next_step_at) > now()


def test_inhibition_holds_the_target(client, setup, outbox):
    client.post("/api/v1/inhibitions", headers=bearer(setup["tok"]), json={
        "name": "namespace down mutes crashloops",
        "source_matchers": [{"name": "alertname", "value": "NamespaceDown"}],
        "target_matchers": [{"name": "alertname", "value": "CrashLoop"}],
        "equal": ["namespace"]})
    client.post(PROM, json=[{"labels": {"alertname": "NamespaceDown", "namespace": "service"},
                             "endsAt": _iso(now() + dt.timedelta(minutes=4))},
                            _prom_alert(pod="a")], headers=bearer(setup["itok"]))
    incidents.run_due()
    assert _notes() == []


def test_unrouted_alert_is_recorded_but_opens_no_incident(client, setup):
    client.post(PROM, json=[{"labels": {"alertname": "SomethingElse"}}], headers=bearer(setup["itok"]))
    assert _incidents() == []
    assert SessionLocal.scalar(select(Occurrence).where(Occurrence.alertname == "SomethingElse"))


def test_first_match_wins_and_reordering_changes_it(client, setup):
    tok = setup["tok"]
    catch_all = client.post("/api/v1/routes", headers=bearer(tok), json={"name": "everything"}).json
    labels = {"alertname": "CrashLoop", "namespace": "service"}
    r = client.post("/api/v1/routes/test", headers=bearer(tok), json={"labels": labels}).json
    assert r["chosen_route_id"] == setup["route"]["id"]
    client.put("/api/v1/routes/order", headers=bearer(tok),
               json={"ids": [catch_all["id"], setup["route"]["id"]]})
    r = client.post("/api/v1/routes/test", headers=bearer(tok), json={"labels": labels}).json
    assert r["chosen_route_id"] == catch_all["id"]
    assert r["routes"][0]["why"] == "catch-all (no matchers)"


def test_failed_delivery_is_retried_then_marked_failed(client, setup, monkeypatch):
    def boom(*a):
        raise delivery.DeliveryError("SMTP: connection refused")
    monkeypatch.setattr(delivery, "send_email", boom)
    monkeypatch.setattr(cfg, "DELIVERY_MAX_ATTEMPTS", 2)
    client.post(PROM, json=[_prom_alert(pod="a")], headers=bearer(setup["itok"]))
    incidents.run_due()
    delivery.deliver_pending()
    (n,) = _notes()
    assert n.status == "pending" and n.attempts == 1 and "connection refused" in n.error
    n.next_attempt_at = now()
    SessionLocal.commit()
    delivery.deliver_pending()
    (n,) = _notes()
    assert n.status == "failed" and n.attempts == 2


class _Resp:
    status_code = 204
    text = ""


def _capture(monkeypatch):
    captured = {}

    def fake_request(method, url, data, headers, timeout, allow_redirects):
        captured.update(method=method, url=url, data=data, headers=headers,
                        redirects=allow_redirects)
        return _Resp()
    monkeypatch.setattr(delivery.requests, "request", fake_request)
    return captured


def test_webhook_channel_public_host_and_signature(client, setup, monkeypatch):
    tok = setup["tok"]
    r = client.post("/api/v1/channels", headers=bearer(tok), json={
        "name": "bad", "kind": "webhook", "config": {"url": "ftp://hooks.example.com/x"}})
    assert r.status_code == 422
    # Any PUBLIC host (internal addresses are refused — tests/test_exposure.py).
    r = client.post("/api/v1/channels", headers=bearer(tok), json={
        "name": "ops", "kind": "webhook", "config": {"url": "https://anything.example.org/in"}})
    assert r.status_code == 201 and r.json["secret"].startswith("shs_")
    secret = r.json["secret"]
    assert "secret" not in client.get("/api/v1/channels", headers=bearer(tok)).json[0]

    captured = _capture(monkeypatch)
    delivery.send_webhook({"url": "https://anything.example.org/in"}, {"event": "test"}, secret)
    expected = hmac.new(secret.encode(), captured["headers"]["X-Shorthop-Timestamp"].encode()
                        + b"." + captured["data"], hashlib.sha256).hexdigest()
    assert captured["headers"]["X-Shorthop-Signature"] == f"sha256={expected}"
    assert captured["method"] == "POST" and captured["redirects"] is False


def test_webhook_method_headers_and_template(client, setup, monkeypatch):
    tok = setup["tok"]
    cfg_in = {"url": "https://api.telegram.org/bot123:SECRET/sendMessage", "method": "put",
              "headers": {"Authorization": "Bearer xyz"},
              "body_template": '{"chat_id": "42", "text": "{{text}}"}',
              "events": ["triggered", "resolved"]}
    r = client.post("/api/v1/channels", headers=bearer(tok),
                    json={"name": "tg", "kind": "webhook", "config": cfg_in})
    assert r.status_code == 201, r.json
    ch = r.json
    assert ch["config"]["method"] == "PUT" and ch["config"]["headers"] == {"Authorization": "••••••"}

    # The masked value read back keeps the stored one on update.
    upd = dict(cfg_in, headers={"Authorization": "••••••", "X-Extra": "1"})
    r = client.put(f"/api/v1/channels/{ch['id']}", headers=bearer(tok),
                   json={"name": "tg", "kind": "webhook", "config": upd})
    assert r.status_code == 200, r.json
    from app.models import Channel
    row = SessionLocal.get(Channel, ch["id"])
    SessionLocal.refresh(row)
    assert row.config["headers"] == {"Authorization": "Bearer xyz", "X-Extra": "1"}

    captured = _capture(monkeypatch)
    delivery.send_webhook(row.config, {"event": "triggered", "incident": {
        "number": 7, "title": 'Disk "full"', "severity": "critical"}, "alerts": []}, None)
    assert captured["method"] == "PUT"
    assert captured["headers"]["Authorization"] == "Bearer xyz"
    body = json.loads(captured["data"])
    assert body["chat_id"] == "42" and body["text"] == '[FIRING] #7 [critical] Disk "full"'


def test_webhook_config_is_validated(client, setup):
    tok = setup["tok"]
    bad = [
        {"url": "https://h.example/x", "method": "DELETE"},
        {"url": "https://h.example/x", "headers": {"Bad Name": "x"}},
        {"url": "https://h.example/x", "headers": {"X-A": "a\r\nX-B: b"}},
        {"url": "https://h.example/x", "headers": {"X-Shorthop-Signature": "forged"}},
        {"url": "https://h.example/x", "headers": {"X-New": "••••••"}},
        {"url": "https://h.example/x", "body_template": '{"text": "{{nope}}"}'},
        {"url": "https://h.example/x", "body_template": '{"text": {{text}}}'},
        {"url": "https://h.example/x", "events": ["acknowledged"]},
        # A preset saved without filling it in.
        {"url": "https://api.telegram.org/bot<BOT_TOKEN>/sendMessage"},
        {"url": "https://h.example/x", "body_template": '{"chat_id": "<CHAT_ID>", "text": "{{text}}"}'},
        {"url": "https://hooks.slack.com/services/…"},
        {"url": "https://h.example/x", "headers": {"Authorization": "Basic <base64 of email:api-token>"}},
    ]
    for i, c in enumerate(bad):
        r = client.post("/api/v1/channels", headers=bearer(tok),
                        json={"name": f"b{i}", "kind": "webhook", "config": c})
        assert r.status_code == 422, c


def test_channel_url_and_headers_hidden_from_non_admins(client, setup):
    tok = setup["tok"]
    client.post("/api/v1/channels", headers=bearer(tok), json={
        "name": "slack", "kind": "webhook",
        "config": {"url": "https://hooks.slack.com/services/T/B/SECRET",
                   "headers": {"X-Key": "k"}}})
    responder = make_token(setup["ann"], ["oncall:read"])
    (c,) = client.get("/api/v1/channels", headers=bearer(responder)).json
    assert c["config"]["url"] == "https://hooks.slack.com/…"
    assert c["config"]["headers"] == {"X-Key": "••••••"}


def test_webhook_payload_carries_incident_and_history(client, setup, monkeypatch, outbox):
    tok = setup["tok"]
    ch = client.post("/api/v1/channels", headers=bearer(tok), json={
        "name": "ops", "kind": "webhook", "config": {"url": "https://hooks.example.com/in"}}).json
    client.put(f"/api/v1/escalation-policies/{setup['policy']['id']}", headers=bearer(tok), json={
        "name": "platform default", "steps": [{"delay_minutes": 0, "targets": [{"type": "channel", "id": ch["id"]}]}]})
    client.post(PROM, json=[_prom_alert(pod="a")], headers=bearer(setup["itok"]))
    incidents.run_due()
    delivery.deliver_pending()
    (url, body, secret) = outbox["webhook"][0]
    assert body["event"] == "triggered" and body["incident"]["number"] == 1
    assert body["alerts"][0]["labels"]["pod"] == "a" and secret.startswith("shs_")
    json.dumps(body)  # serialisable


def test_policy_validation(client, setup):
    tok, ann = setup["tok"], setup["ann"]
    bad = [
        {"name": "x", "steps": []},
        {"name": "x", "steps": [{"targets": [{"type": "user", "id": ann.id}]},
                                {"delay_minutes": 0, "targets": [{"type": "user", "id": ann.id}]}]},
        {"name": "x", "steps": [{"targets": [{"type": "user", "id": "nope"}]}]},
        {"name": "x", "steps": [{"targets": [{"type": "robot", "id": ann.id}]}]},
    ]
    for body in bad:
        assert client.post("/api/v1/escalation-policies", headers=bearer(tok), json=body).status_code == 422
    r = client.delete(f"/api/v1/escalation-policies/{setup['policy']['id']}", headers=bearer(tok))
    assert r.status_code == 409 and "crashloops" in r.json["message"]


def test_responder_cannot_change_routing(client, setup):
    tok = make_token(setup["ann"], ["alerts:read", "alerts:write", "oncall:read"])
    assert client.post("/api/v1/routes", headers=bearer(tok), json={"name": "x"}).status_code == 403
    assert client.get("/api/v1/routes", headers=bearer(tok)).status_code == 200


def test_webhook_event_filter_suppresses_unwanted_events(client, setup, monkeypatch, outbox):
    tok = setup["tok"]
    ch = client.post("/api/v1/channels", headers=bearer(tok), json={
        "name": "jira", "kind": "webhook",
        "config": {"url": "https://jira.example/rest/api/2/issue", "events": ["triggered"]}}).json
    client.put(f"/api/v1/escalation-policies/{setup['policy']['id']}", headers=bearer(tok), json={
        "name": "platform default", "steps": [{"delay_minutes": 0, "targets": [{"type": "channel", "id": ch["id"]}]}]})
    client.post(PROM, json=[_prom_alert(pod="a")], headers=bearer(setup["itok"]))
    incidents.run_due()
    delivery.deliver_pending()
    assert len(outbox["webhook"]) == 1
    (n,) = _notes()
    assert n.destination == "https://jira.example/…"  # the URL itself is not logged
    client.post(PROM, json=[_prom_alert(pod="a", ends_in=dt.timedelta(seconds=-5))],
                headers=bearer(setup["itok"]))
    delivery.deliver_pending()
    assert len(outbox["webhook"]) == 1  # no "resolved" for a channel that only wants triggered
    resolved = [x for x in _notes() if x.event == "resolved"]
    assert resolved and resolved[0].status == "suppressed"


def _team_with_policy(client, tok, name, member):
    team = client.post("/api/v1/teams", headers=bearer(tok), json={"slug": name, "name": name}).json
    client.post(f"/api/v1/teams/{team['id']}/members", headers=bearer(tok),
                json={"user_id": member.id})
    pol = client.post("/api/v1/escalation-policies", headers=bearer(tok), json={
        "name": f"{name} policy", "team_id": team["id"],
        "steps": [{"delay_minutes": 0, "targets": [{"type": "team", "id": team["id"]}]}]}).json
    return team, pol


def test_reassign_moves_incident_and_pages_the_new_team(client, setup, outbox):
    tok = setup["tok"]
    carl = make_user(email="carl@example.com")
    dba, dba_pol = _team_with_policy(client, tok, "dba", carl)
    client.post(PROM, json=[_prom_alert(pod="a")], headers=bearer(setup["itok"]))
    incidents.run_due()
    (inc,) = _incidents()
    # the old team acknowledges, then realises it is the DBAs'
    client.post(f"/api/v1/incidents/{inc.id}/ack", headers=bearer(tok))
    _due_now(inc.id)
    r = client.post(f"/api/v1/incidents/{inc.id}/reassign", headers=bearer(tok),
                    json={"team_id": dba["id"], "note": "replication, not the app"})
    assert r.status_code == 200, r.json
    assert r.json["team_name"] == "dba" and r.json["status"] == "triggered"
    assert r.json["acked_by"] is None and "replication" in r.json["reassign_note"]
    assert all(a["team_id"] == dba["id"] for a in r.json["alerts"])
    assert all(a["status"] == "firing" for a in r.json["alerts"])
    incidents.run_due()
    delivery.deliver_pending()
    to_carl = [x for x in outbox["email"] if x[0] == "carl@example.com"]
    assert to_carl and "Moved to this team by" in to_carl[0][2]
    assert any(e.kind == "reassigned" for o in _incidents()[0].alerts for e in o.events)
    # a new alert in the same group keeps joining the incident, now the DBAs'
    client.post(PROM, json=[_prom_alert(pod="b")], headers=bearer(setup["itok"]))
    (inc,) = _incidents()
    assert inc.team_id == dba["id"] and len(inc.alerts) == 2


def test_reassign_cancels_pages_queued_for_the_old_team(client, setup, outbox):
    tok = setup["tok"]
    dba, _ = _team_with_policy(client, tok, "dba", make_user(email="carl@example.com"))
    client.post(PROM, json=[_prom_alert(pod="a")], headers=bearer(setup["itok"]))
    incidents.run_due()  # step 1 queued for the old policy, not yet delivered
    (inc,) = _incidents()
    queued = [n for n in _notes() if n.status == "pending"]
    assert queued
    client.post(f"/api/v1/incidents/{inc.id}/reassign", headers=bearer(tok), json={"team_id": dba["id"]})
    old = [n for n in _notes() if n.id in {q.id for q in queued}]
    assert all(n.status == "suppressed" and "reassigned to dba" in n.error for n in old)


def test_reassign_without_paging_keeps_the_ack_and_validation(client, setup):
    tok = setup["tok"]
    dba, dba_pol = _team_with_policy(client, tok, "dba", make_user(email="carl@example.com"))
    empty = client.post("/api/v1/teams", headers=bearer(tok), json={"slug": "nobody", "name": "nobody"}).json
    client.post(PROM, json=[_prom_alert(pod="a")], headers=bearer(setup["itok"]))
    (inc,) = _incidents()
    url = f"/api/v1/incidents/{inc.id}/reassign"
    # a team with no policy of its own: the caller must say who pages them
    r = client.post(url, headers=bearer(tok), json={"team_id": empty["id"]})
    assert r.status_code == 409 and "choose" in r.json["message"]
    assert client.post(url, headers=bearer(tok), json={"team_id": "nope"}).status_code == 409
    client.post(f"/api/v1/incidents/{inc.id}/ack", headers=bearer(tok))
    r = client.post(url, headers=bearer(tok), json={"team_id": dba["id"], "page": False})
    assert r.json["status"] == "acknowledged" and r.json["next_step_at"] is None
    viewer = make_token(make_user(email="v@example.com", role="viewer"), ["alerts:read"])
    assert client.post(url, headers=bearer(viewer), json={"team_id": dba["id"]}).status_code == 403


def test_incident_list_mine_follows_ownership_after_reassign(client, setup):
    """A team that hands an incident off stops seeing it under "mine"; the new team does."""
    tok, ann = setup["tok"], setup["ann"]
    carl = make_user(email="carl@example.com")
    dba, _ = _team_with_policy(client, tok, "dba", carl)
    ann_tok, carl_tok = make_token(ann, ["alerts:read"]), make_token(carl, ["alerts:read"])
    client.post(PROM, json=[_prom_alert(pod="a")], headers=bearer(setup["itok"]))
    (inc,) = _incidents()

    def ids(t, team):
        r = client.get(f"/api/v1/incidents?team={team}", headers=bearer(t))
        assert r.status_code == 200, r.json
        return [i["id"] for i in r.json], r.json

    got, rows = ids(ann_tok, "mine")
    assert got == [inc.id] and rows[0]["team_name"] == "Platform"
    assert ids(carl_tok, "mine")[0] == []
    client.post(f"/api/v1/incidents/{inc.id}/reassign", headers=bearer(tok), json={"team_id": dba["id"]})
    assert ids(ann_tok, "mine")[0] == []
    got, rows = ids(carl_tok, "mine")
    assert got == [inc.id] and rows[0]["team_name"] == "dba"
    # "all" is unchanged, and a team id narrows to that team
    assert ids(ann_tok, "all")[0] == [inc.id]
    assert ids(ann_tok, setup["team"]["id"])[0] == []
    assert ids(ann_tok, dba["id"])[0] == [inc.id]


def test_incident_list_mine_includes_unowned(client, setup):
    tok = setup["tok"]
    client.post(PROM, json=[_prom_alert(pod="a")], headers=bearer(setup["itok"]))
    (inc,) = _incidents()
    inc.team_id = None
    SessionLocal.commit()
    loner = make_token(make_user(email="loner@example.com"), ["alerts:read"])
    r = client.get("/api/v1/incidents?team=mine", headers=bearer(loner))
    assert [i["id"] for i in r.json] == [inc.id], r.json
    assert r.json[0]["team_name"] is None, r.json[0]


def test_webhook_template_reads_labels_and_annotations(client, setup, monkeypatch):
    """Labels a label rule adds are usable in a channel template, bare or as labels.x."""
    tok = setup["tok"]
    url = "https://api.telegram.org/bot1:S/sendMessage"

    def save(name, template):
        return client.post("/api/v1/channels", headers=bearer(tok), json={
            "name": name, "kind": "webhook", "config": {"url": url, "body_template": template}})

    # {{level}} is unknown until a label rule sets it; labels.x / annotations.x always save
    assert save("a", '{"text": "{{level}}"}').status_code == 422
    assert "labels.level" in save("a", '{"text": "{{level}}"}').json["message"]
    assert save("b", '{"text": "{{labels.pod}} {{annotations.runbook_url}}"}').status_code == 201
    r = client.post("/api/v1/label-rules", headers=bearer(tok), json={
        "name": "level", "matchers": [], "set_labels": {"level": "L2"}})
    assert r.status_code == 201, r.json
    r = save("c", '{"text": "{{level}} {{labels.level}} {{labels.pod}} {{annotations.summary}} {{labels.nope}}"}')
    assert r.status_code == 201, r.json
    # a typo of a built-in still fails on save
    assert save("d", '{"text": "{{titel}}"}').status_code == 422

    captured = _capture(monkeypatch)
    from app.models import Channel
    row = SessionLocal.get(Channel, r.json["id"])
    body = {"event": "triggered", "incident": {"title": "x", "group_labels": {"pod": "grp"}},
            "alerts": [
                {"labels": {"pod": "gone", "level": "L9"}, "annotations": {}, "resolved_at": "t"},
                {"labels": {"pod": "a", "level": "L2"}, "annotations": {"summary": 'disk "full"'}},
            ]}
    delivery.send_webhook(row.config, body, None)
    # the first OPEN alert wins; a label nobody carries renders empty rather than failing
    assert json.loads(captured["data"])["text"] == 'L2 L2 a disk "full" '
