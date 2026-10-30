"""The Jira channel. Jira itself is faked at `jira.requests.request` (the one seam every call
goes through), so these assert exactly what Shorthop would have said to Jira."""
import datetime as dt
import json

import pytest
from sqlalchemy import select

from app import delivery, incidents, jira
from app.db import SessionLocal
from app.models import JiraIssue, Notification, now
from tests.conftest import bearer
from tests.test_ingest import PROM, _prom_alert
from tests.test_paging import _incidents, setup  # noqa: F401 — fixture

SITE = "https://acme.atlassian.net"


class Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body
        self.content = b"" if body is None else json.dumps(body).encode()
        self.text = self.content.decode()

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class FakeJira:
    def __init__(self):
        self.calls: list[tuple[str, str, dict | None, dict]] = []
        self.fail: dict[str, tuple[int, dict]] = {}   # path suffix -> (status, body)
        self.next_key = 41
        self.transitions = [{"id": "11", "name": "In Progress",
                             "to": {"statusCategory": {"key": "indeterminate"}}},
                            {"id": "31", "name": "Done", "to": {"statusCategory": {"key": "done"}}}]

    def __call__(self, method, url, json=None, headers=None, timeout=None, allow_redirects=None):
        path = url[len(SITE):]
        self.calls.append((method, path, json, headers))
        for suffix, (status, body) in self.fail.items():
            if path.endswith(suffix):
                return Resp(status, body)
        if method == "POST" and path == "/rest/api/2/issue":
            self.next_key += 1
            return Resp(201, {"id": "100", "key": f"OPS-{self.next_key}"})
        if path.endswith("/transitions") and method == "GET":
            return Resp(200, {"transitions": self.transitions})
        if path.endswith("/transitions") or path.endswith("/comment"):
            return Resp(204 if path.endswith("/transitions") else 201,
                        None if path.endswith("/transitions") else {"id": "1"})
        if path == "/rest/api/2/myself":
            return Resp(200, {"emailAddress": "bot@acme.io"})
        if path.startswith("/rest/api/2/project/"):
            return Resp(200, {"key": "OPS", "name": "Operations",
                              "issueTypes": [{"name": "Task"}, {"name": "Incident"}]})
        return Resp(404, {"errorMessages": ["no such thing"]})

    def of(self, method, suffix=""):
        return [(p, j) for m, p, j, _ in self.calls if m == method and p.endswith(suffix)]


@pytest.fixture()
def fake(monkeypatch):
    f = FakeJira()
    monkeypatch.setattr(jira.requests, "request", f)
    return f


def _jira_channel(client, setup, **extra):
    ch = client.post("/api/v1/channels", headers=bearer(setup["tok"]), json={
        "name": "ops-jira", "kind": "jira",
        "config": {"site": SITE + "/", "email": "Bot@acme.io", "api_token": "tok-123",
                   "project_key": "ops", "issue_type": "Incident", "labels": "prod p1",
                   **extra}})
    assert ch.status_code == 201, ch.json
    r = client.put(f"/api/v1/escalation-policies/{setup['policy']['id']}", headers=bearer(setup["tok"]),
                   json={"name": "platform default", "steps": [
                       {"delay_minutes": 0, "targets": [{"type": "channel", "id": ch.json["id"]}]},
                       {"delay_minutes": 5, "targets": [{"type": "channel", "id": ch.json["id"]}]}]})
    assert r.status_code == 200, r.json
    return ch.json


def _dispatch():
    incidents.run_due()
    delivery.deliver_pending()
    jira.refresh_dirty()
    SessionLocal.remove()


def _fire(client, setup):
    client.post(PROM, json=[_prom_alert()], headers=bearer(setup["itok"]))
    (inc,) = [i for i in _incidents() if i.resolved_at is None]
    return inc.id


def _issue():
    SessionLocal.remove()
    return SessionLocal.scalars(select(JiraIssue)).one()


def test_config_is_cleaned_and_the_token_is_write_only(client, setup):
    ch = _jira_channel(client, setup)
    assert ch["config"]["api_token"] == "••••••"
    assert ch["config"]["site"] == SITE and ch["config"]["project_key"] == "OPS"
    assert ch["config"]["email"] == "bot@acme.io" and ch["config"]["labels"] == ["prod", "p1"]
    # Saving the mask back keeps the stored token.
    r = client.put(f"/api/v1/channels/{ch['id']}", headers=bearer(setup["tok"]), json={
        "name": "ops-jira", "kind": "jira", "config": {**ch["config"]}})
    assert r.status_code == 200
    SessionLocal.remove()
    from app.models import Channel
    assert SessionLocal.get(Channel, ch["id"]).config["api_token"] == "tok-123"
    bad = client.post("/api/v1/channels", headers=bearer(setup["tok"]), json={
        "name": "x", "kind": "jira", "config": {"site": SITE + "/jira/software/projects/OPS/boards/1",
                                                 "api_token": "t", "project_key": "OPS"}})
    assert bad.status_code == 422 and "board" in bad.json["message"]


def test_one_issue_per_incident_commented_then_closed(client, setup, fake):
    _jira_channel(client, setup)
    iid = _fire(client, setup)
    _dispatch()
    (path, body), = fake.of("POST", "/rest/api/2/issue")
    f = body["fields"]
    assert f["project"] == {"key": "OPS"} and f["issuetype"] == {"name": "Incident"}
    assert f["summary"].endswith("CrashLoop") or "#" in f["summary"]
    assert set(f["labels"]) == {"prod", "p1", "shorthop"}
    assert "Closing the issue does not resolve the alerts" in f["description"]
    _, _, _, headers = fake.calls[0]
    assert headers["Authorization"].startswith("Basic ")
    row = _issue()
    assert row.issue_key == "OPS-42" and row.issue_url == f"{SITE}/browse/OPS-42"

    # A note and an AI suggestion become comments, without the Slack emoji.
    h = bearer(setup["tok"])
    client.post(f"/api/v1/incidents/{iid}/notes", headers=h, json={"text": "looking"})
    client.post(f"/api/v1/incidents/{iid}/notes", headers=h,
                json={"text": "likely the 14:02 deploy", "kind": "suggestion"})
    _dispatch()
    comments = [b["body"] for _, b in fake.of("POST", "/comment")]
    assert comments == ["boss@example.com: looking",
                        "Suggestion from boss@example.com (unverified): likely the 14:02 deploy"]

    # The escalation step pages the same channel again: a comment, never a second issue.
    inc = SessionLocal.get(incidents.Incident, iid)
    inc.next_step_at = now() - dt.timedelta(seconds=1)
    SessionLocal.commit()
    _dispatch()
    assert len(fake.of("POST", "/rest/api/2/issue")) == 1
    assert any("Escalated" in b["body"] for _, b in fake.of("POST", "/comment"))

    # Resolving comments, then transitions to the first "done" status.
    r = client.post(f"/api/v1/incidents/{iid}/resolve", headers=h,
                    json={"category": "fixed", "note": "rolled back"})
    assert r.status_code == 200
    _dispatch()
    assert fake.of("POST", "/transitions") == [("/rest/api/2/issue/OPS-42/transitions",
                                                {"transition": {"id": "31"}})]
    last = [b["body"] for _, b in fake.of("POST", "/comment")]
    assert any(c.startswith("Resolved") for c in last) and any("rolled back" in c for c in last)
    row = _issue()
    assert row.closed_at is not None and not row.close_pending and row.pending_comments == []
    detail = client.get(f"/api/v1/incidents/{iid}", headers=h).json
    assert detail["jira_issues"][0]["issue_key"] == "OPS-42"


def test_a_failed_comment_is_kept_and_retried_in_order(client, setup, fake):
    _jira_channel(client, setup)
    iid = _fire(client, setup)
    _dispatch()
    fake.fail["/comment"] = (503, {"errorMessages": ["busy"]})
    h = bearer(setup["tok"])
    client.post(f"/api/v1/incidents/{iid}/notes", headers=h, json={"text": "first"})
    client.post(f"/api/v1/incidents/{iid}/notes", headers=h, json={"text": "second"})
    _dispatch()
    row = _issue()
    assert row.dirty and row.attempts == 1 and "HTTP 503" in row.error and "busy" in row.error
    assert [c.split(": ", 1)[1] for c in row.pending_comments] == ["first", "second"]
    fake.fail.clear()
    row.dirty_at = now() - dt.timedelta(seconds=1)
    SessionLocal.commit()
    _dispatch()
    assert [b["body"].split(": ", 1)[1] for _, b in fake.of("POST", "/comment")
            if b["body"].startswith("boss")][-2:] == ["first", "second"]
    assert not _issue().dirty


def test_a_named_transition_that_does_not_exist_is_reported(client, setup, fake):
    _jira_channel(client, setup, done_transition="Resolve Incident")
    iid = _fire(client, setup)
    _dispatch()
    client.post(f"/api/v1/incidents/{iid}/resolve", headers=bearer(setup["tok"]), json={})
    _dispatch()
    n = SessionLocal.scalars(select(Notification).where(Notification.event == "resolved",
                                                        Notification.kind == "jira")).one()
    assert "no transition named 'Resolve Incident'" in (n.error or "")
    assert "Done" in n.error
    assert fake.of("POST", "/transitions") == []


def test_create_failure_is_retried_and_opens_no_duplicate(client, setup, fake):
    _jira_channel(client, setup)
    fake.fail["/rest/api/2/issue"] = (400, {"errors": {"issuetype": "is not valid"}})
    _fire(client, setup)
    _dispatch()
    n = SessionLocal.scalars(select(Notification).where(Notification.kind == "jira")).first()
    assert n.status == "pending" and "issuetype: is not valid" in n.error
    assert SessionLocal.scalars(select(JiraIssue)).all() == []
    fake.fail.clear()
    n.next_attempt_at = now() - dt.timedelta(seconds=1)
    SessionLocal.commit()
    _dispatch()
    assert _issue().issue_key == "OPS-42"


def test_send_test_checks_without_opening_an_issue(client, setup, fake):
    ch = _jira_channel(client, setup)
    r = client.post(f"/api/v1/channels/{ch['id']}/test", headers=bearer(setup["tok"]))
    assert r.json["ok"] and "bot@acme.io" in r.json["detail"]
    assert fake.of("POST") == []
    fake.fail["/myself"] = (401, {})
    r = client.post(f"/api/v1/channels/{ch['id']}/test", headers=bearer(setup["tok"]))
    assert not r.json["ok"] and "refused" in r.json["error"]


def test_data_center_token_uses_bearer(client, setup, fake):
    ch = client.post("/api/v1/channels", headers=bearer(setup["tok"]), json={
        "name": "dc", "kind": "jira",
        "config": {"site": SITE, "api_token": "pat-1", "project_key": "OPS"}})
    assert ch.status_code == 201
    client.post(f"/api/v1/channels/{ch.json['id']}/test", headers=bearer(setup["tok"]))
    assert fake.calls[0][3]["Authorization"] == "Bearer pat-1"
