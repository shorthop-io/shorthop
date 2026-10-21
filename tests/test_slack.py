"""The Slack app and incident levels. Slack itself is faked at `slack.api` (the one seam every
Web API call goes through), so these assert exactly what Shorthop would have said to Slack."""
import datetime as dt

import pytest
from sqlalchemy import select

from app import delivery, incidents, slack
from app import settings as st
from app.core import levels as lv
from app.db import SessionLocal
from app.models import AuditEntry, Channel, Incident, Silence, SlackMessage, User, aware, now
from tests.conftest import bearer, make_user
from tests.test_ingest import PROM, _prom_alert
from tests.test_paging import _incidents, outbox, setup  # noqa: F401 — fixtures

TEAM = "T0SHORT"


class FakeSlack:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.users = {}           # slack user id -> {"email", "team_id", …}
        self.fail: dict[str, str] = {}
        self._ts = 1000

    def __call__(self, method, **params):
        self.calls.append((method, params))
        if method in self.fail:
            raise slack.SlackError(self.fail[method])
        if method == "auth.test":
            return {"ok": True, "team_id": TEAM, "team": "Acme", "user": "shorthop"}
        if method == "users.info":
            u = self.users.get(params["user"])
            if u is None:
                raise slack.SlackError("user_not_found")
            return {"user": {"id": params["user"], "team_id": u.get("team_id", TEAM),
                             "profile": {"email": u["email"]}}}
        if method == "users.lookupByEmail":
            for sid, u in self.users.items():
                if u["email"] == params["email"]:
                    return {"user": {"id": sid}}
            raise slack.SlackError(slack._ERRORS["users_not_found"])
        if method == "conversations.open":
            return {"channel": {"id": "D" + params["users"][1:]}}
        if method == "chat.postMessage":
            self._ts += 1
            return {"ok": True, "channel": params["channel"], "ts": f"{self._ts}.0001"}
        return {"ok": True}

    def of(self, method):
        return [p for m, p in self.calls if m == method]


@pytest.fixture()
def fake(monkeypatch):
    f = FakeSlack()
    monkeypatch.setattr(slack, "api", f)
    with slack._state_lock:
        slack._state.update(team_id=None, team=None)
    st.put({"slack_bot_token": "xoxb-test", "slack_app_token": "xapp-test"}, "test")
    SessionLocal.commit()
    return f


def _slack_channel(policy_id, client, tok):
    ch = client.post("/api/v1/channels", headers=bearer(tok), json={
        "name": "ops-slack", "kind": "slack",
        "config": {"channel_id": "C0OPS", "channel_name": "ops-alerts"}})
    assert ch.status_code == 201, ch.json
    r = client.put(f"/api/v1/escalation-policies/{policy_id}", headers=bearer(tok), json={
        "name": "platform default", "steps": [
            {"delay_minutes": 0, "targets": [{"type": "channel", "id": ch.json["id"]}]},
            {"delay_minutes": 5, "targets": [{"type": "channel", "id": ch.json["id"]}]}]})
    assert r.status_code == 200, r.json
    return ch.json


def _dispatch():
    incidents.run_due()
    delivery.deliver_pending()
    slack.refresh_dirty()
    SessionLocal.remove()


def _fire(client, setup, **labels):
    client.post(PROM, json=[_prom_alert(**labels)], headers=bearer(setup["itok"]))
    (inc,) = [i for i in _incidents() if i.resolved_at is None]
    return inc.id


def _inc(iid):
    SessionLocal.remove()
    return SessionLocal.get(Incident, iid)


def _click(action_id, value, user="U0ANN", **extra):
    return {"type": "block_actions", "user": {"id": user, "team_id": TEAM},
            "trigger_id": "trig", "channel": {"id": "C0OPS"},
            "actions": [{"action_id": action_id, **({"selected_option": {"value": value}}
                                                    if action_id in ("sh_level", "sh_more")
                                                    else {"value": value})}], **extra}


def _submit(callback, iid, values, user="U0ANN"):
    state = {}
    for block, v in values.items():
        if isinstance(v, list):
            state[block] = {"v": {"type": "checkboxes", "selected_options": [{"value": x} for x in v]}}
        elif block in ("category", "team", "policy", "user", "schedule", "minutes"):
            state[block] = {"v": {"type": "static_select", "selected_option": {"value": v} if v else None}}
        else:
            state[block] = {"v": {"type": "plain_text_input", "value": v}}
    return {"type": "view_submission", "user": {"id": user, "team_id": TEAM},
            "view": {"callback_id": callback, "private_metadata": iid, "state": {"values": state}}}


# --- levels (pure) -----------------------------------------------------------------------------

def test_levels_derive_from_label_then_severity():
    L = lv.DEFAULT_LEVELS
    assert lv.derive(L, [None, "p2", "P3"], "critical") == "P2"       # label wins, most urgent
    assert lv.derive(L, [None, "P9"], "warning") == "P3"              # unknown label → severity
    assert lv.derive(L, [], "nonsense") is None                       # never invented
    with pytest.raises(lv.LevelError):
        lv.validate([{"name": "P1", "severity": "critical"}, {"name": "p1", "severity": "high"}])
    with pytest.raises(lv.LevelError):
        lv.validate([{"name": "P 1", "severity": "critical"}])


def test_incident_level_follows_alerts_until_a_person_sets_it(client, setup):
    iid = _fire(client, setup, severity="warning")
    inc = _inc(iid)
    assert (inc.level, inc.severity) == ("P3", "warning")
    r = client.put(f"/api/v1/incidents/{iid}/level", headers=bearer(setup["tok"]), json={"level": "P1"})
    assert r.status_code == 200 and r.json["level"] == "P1" and r.json["severity"] == "critical"
    assert r.json["level_manual"] and r.json["level_set_by"] == "boss@example.com"
    # A later alert joining — with its own level label — does not undo a person's call.
    client.post(PROM, json=[_prom_alert(pod="b", severity="info", level="P4")], headers=bearer(setup["itok"]))
    (inc,) = _incidents()
    assert (inc.level, inc.severity) == ("P1", "critical")
    # …nor does a MORE severe one: lowered by hand to P4, it stays P4/info.
    client.put(f"/api/v1/incidents/{iid}/level", headers=bearer(setup["tok"]), json={"level": "P4"})
    client.post(PROM, json=[_prom_alert(pod="c", severity="critical")], headers=bearer(setup["itok"]))
    (inc,) = _incidents()
    assert (inc.level, inc.severity) == ("P4", "info")
    assert client.put(f"/api/v1/incidents/{iid}/level", headers=bearer(setup["tok"]),
                      json={"level": "P7"}).status_code == 409


def test_level_label_from_a_label_rule_sets_the_level(client, setup):
    iid = _fire(client, setup, severity="warning", level="P2")
    assert _inc(iid).level == "P2"


# --- delivery: one message per incident, kept up to date ---------------------------------------------

def test_channel_message_is_posted_once_then_updated_with_thread_replies(client, setup, fake):
    _slack_channel(setup["policy"]["id"], client, setup["tok"])
    iid = _fire(client, setup)
    _dispatch()
    (post,) = fake.of("chat.postMessage")
    assert post["channel"] == "C0OPS" and "#1" in post["text"]
    actions = [b for b in post["blocks"] if b["type"] == "actions"][0]
    ids = [e["action_id"] for e in actions["elements"]]
    assert ids == ["sh_ack", "sh_resolve", "sh_level", "sh_move", "sh_more"]
    (msg,) = SessionLocal.scalars(select(SlackMessage)).all()
    assert (msg.incident_id, msg.conversation, msg.dirty) == (iid, "C0OPS", False)

    # Acknowledged in the WEB UI: the Slack message shows it, with a reply in its thread.
    client.post(f"/api/v1/incidents/{iid}/ack", headers=bearer(setup["tok"]))
    _dispatch()
    _dispatch()   # a second pass must not post the reply again
    replies = [p for p in fake.of("chat.postMessage") if p.get("thread_ts")]
    assert [r["text"] for r in replies] == [":white_check_mark: Acknowledged by boss@example.com"]
    update = fake.of("chat.update")[-1]
    assert update["ts"] == msg.ts and "Acknowledged" in str(update["blocks"])
    assert "sh_ack" not in str(update["blocks"])     # nothing left to acknowledge


def test_slack_failure_keeps_the_replies_and_backs_off(client, setup, fake):
    _slack_channel(setup["policy"]["id"], client, setup["tok"])
    iid = _fire(client, setup)
    _dispatch()
    fake.fail["chat.postMessage"] = "ratelimited"
    client.post(f"/api/v1/incidents/{iid}/notes", headers=bearer(setup["tok"]), json={"text": "looking"})
    _dispatch()
    msg = SessionLocal.scalars(select(SlackMessage)).one()
    assert msg.dirty and msg.attempts == 1 and msg.pending_replies == [":memo: boss@example.com: looking"]
    fake.fail.clear()
    msg.dirty_at = now() - dt.timedelta(seconds=1)
    SessionLocal.commit()
    _dispatch()
    msg = SessionLocal.scalars(select(SlackMessage)).one()
    assert not msg.dirty and msg.pending_replies == [] and msg.error is None


def test_slack_channel_config_is_validated(client, setup):
    r = client.post("/api/v1/channels", headers=bearer(setup["tok"]),
                    json={"name": "x", "kind": "slack", "config": {"channel_id": "#ops"}})
    assert r.status_code == 422


# --- who clicked ---------------------------------------------------------------------------------

def test_button_acks_as_the_shorthop_user_with_that_email(client, setup, fake):
    _slack_channel(setup["policy"]["id"], client, setup["tok"])
    fake.users["U0ANN"] = {"email": "Ann@Example.com"}
    iid = _fire(client, setup)
    _dispatch()
    slack.handle_block_action(_click("sh_ack", iid))
    inc = _inc(iid)
    assert inc.status == "acknowledged" and inc.acked_by == "ann@example.com"
    assert SessionLocal.scalar(select(User).where(User.email == "ann@example.com")).slack_user_id == "U0ANN"
    assert SessionLocal.scalar(select(AuditEntry).where(AuditEntry.action == "incident.ack")).detail == "via Slack"


@pytest.mark.parametrize("who,expected", [
    ({"email": "stranger@elsewhere.com"}, "not a Shorthop user"),
    ({"email": "ann@example.com", "team_id": "T0OTHER"}, "another Slack workspace"),
    ({"email": "viewer@example.com"}, "may not do that"),
])
def test_clicks_that_are_refused_say_why_privately(client, setup, fake, who, expected):
    make_user(email="viewer@example.com", role="viewer")
    fake.users["U0X"] = who
    iid = _fire(client, setup)
    slack.handle_block_action(_click("sh_ack", iid, user="U0X"))
    assert _inc(iid).status == "triggered"
    (eph,) = fake.of("chat.postEphemeral")
    assert expected in eph["text"] and eph["user"] == "U0X"


def test_mapped_user_clicking_from_another_workspace_is_refused(client, setup, fake):
    ann = SessionLocal.get(User, setup["ann"].id)
    ann.slack_user_id = "U0ANN"       # already known: users.info is not asked again
    SessionLocal.commit()
    iid = _fire(client, setup)
    click = _click("sh_ack", iid)
    click["user"]["team_id"] = "T0OTHER"
    slack.handle_block_action(click)
    assert _inc(iid).status == "triggered"
    assert "another Slack workspace" in fake.of("chat.postEphemeral")[0]["text"]


def test_escalation_is_said_in_the_thread_of_the_existing_message(client, setup, fake):
    _slack_channel(setup["policy"]["id"], client, setup["tok"])
    iid = _fire(client, setup)
    _dispatch()
    inc = _inc(iid)
    inc.next_step_at = now() - dt.timedelta(seconds=1)   # step 2 is due
    SessionLocal.commit()
    _dispatch()
    assert len([p for p in fake.of("chat.postMessage") if not p.get("thread_ts")]) == 1
    replies = [p["text"] for p in fake.of("chat.postMessage") if p.get("thread_ts")]
    assert replies == [":rotating_light: Escalated — step 2: channel ops-slack"]


def test_disabled_user_is_refused_even_when_mapped(client, setup, fake):
    ann = SessionLocal.get(User, setup["ann"].id)
    ann.slack_user_id, ann.disabled = "U0ANN", True
    SessionLocal.commit()
    iid = _fire(client, setup)
    slack.handle_block_action(_click("sh_ack", iid))
    assert "disabled" in fake.of("chat.postEphemeral")[0]["text"]


# --- buttons, menus and modals ---------------------------------------------------------------------

def test_level_menu_and_escalate_now(client, setup, fake):
    fake.users["U0ANN"] = {"email": "ann@example.com"}
    iid = _fire(client, setup, severity="warning")
    incidents.run_due()                      # step 1 ran; step 2 is due in 5 min
    SessionLocal.remove()
    slack.handle_block_action(_click("sh_level", f"P1|{iid}"))
    slack.handle_block_action(_click("sh_more", f"escalate|{iid}"))
    inc = _inc(iid)
    assert (inc.level, inc.severity, inc.level_manual) == ("P1", "critical", True)
    assert aware(inc.next_step_at) <= now()


def test_resolve_button_opens_a_modal_and_its_submit_resolves_with_the_fix(client, setup, fake):
    fake.users["U0ANN"] = {"email": "ann@example.com"}
    iid = _fire(client, setup)
    slack.handle_block_action(_click("sh_resolve", iid))
    (opened,) = fake.of("views.open")
    assert opened["trigger_id"] == "trig" and opened["view"]["callback_id"] == "sh_resolve"
    assert slack.handle_view_submission(_submit("sh_resolve", iid, {
        "category": "restarted", "note": "bounced the consumer"})) is None
    inc = _inc(iid)
    assert inc.status == "resolved" and inc.resolved_by == "ann@example.com"
    assert inc.alerts[0].resolution_note == "bounced the consumer"
    # "What fixed it?" afterwards still records it.
    assert slack.handle_view_submission(_submit("sh_resolve", iid, {
        "category": "config_change", "note": "raised the memory limit"})) is None
    assert _inc(iid).alerts[0].resolution_note == "raised the memory limit"


def test_move_team_modal_and_errors_stay_in_the_modal(client, setup, fake):
    from tests.test_paging import _team_with_policy
    fake.users["U0ANN"] = {"email": "ann@example.com"}
    db_team, _ = _team_with_policy(client, setup["tok"], "databases", setup["bob"])
    iid = _fire(client, setup)
    out = slack.handle_view_submission(_submit("sh_move", iid, {"team": setup["team"]["id"],
                                                                   "policy": setup["policy"]["id"]}))
    assert out["response_action"] == "errors" and "already with that team" in out["errors"]["team"]
    assert slack.handle_view_submission(_submit("sh_move", iid, {
        "team": db_team["id"], "note": "it is the database", "page": ["page"]})) is None
    assert _inc(iid).team_id == db_team["id"]


def test_snooze_silences_the_incident_group_for_the_chosen_time(client, setup, fake):
    fake.users["U0ANN"] = {"email": "ann@example.com"}
    iid = _fire(client, setup)
    assert slack.handle_view_submission(_submit("sh_snooze", iid, {
        "minutes": "120", "comment": "deploy in progress"})) is None
    s = SessionLocal.scalars(select(Silence)).one()
    assert {m["name"]: m["value"] for m in s.matchers} == {"alertname": "CrashLoop", "namespace": "service"}
    assert dt.timedelta(minutes=119) < s.ends_at - s.starts_at <= dt.timedelta(minutes=120)
    assert s.created_by == "ann@example.com"


def test_add_responder_pages_by_slack_dm(client, setup, fake):
    fake.users["U0ANN"] = {"email": "ann@example.com"}
    fake.users["U0BOB"] = {"email": "bob@example.com"}
    bob = SessionLocal.get(User, setup["bob"].id)
    bob.contact_rules = [{"method": "slack", "delay_minutes": 0, "severities": None}]
    SessionLocal.commit()
    iid = _fire(client, setup)
    assert slack.handle_view_submission(_submit("sh_responder", iid, {
        "user": setup["bob"].id, "note": "need db eyes"})) is None
    _dispatch()
    dm = [p for p in fake.of("chat.postMessage") if p["channel"] == "D0BOB"]
    assert len(dm) == 1 and "#1" in dm[0]["text"]
    assert fake.of("conversations.open") == [{"users": "U0BOB"}]
    assert SessionLocal.get(User, setup["bob"].id).slack_user_id == "U0BOB"


def test_responder_modal_needs_exactly_one_target(client, setup, fake):
    fake.users["U0ANN"] = {"email": "ann@example.com"}
    iid = _fire(client, setup)
    out = slack.handle_view_submission(_submit("sh_responder", iid, {"user": None, "schedule": None}))
    assert out["response_action"] == "errors"


# --- the slash command ------------------------------------------------------------------------------

def _cmd(text, user="U0ANN"):
    return slack.handle_command({"text": text, "user_id": user, "team_id": TEAM, "channel_id": "C0OPS"})


def test_slash_command(client, setup, fake):
    fake.users["U0ANN"] = {"email": "ann@example.com"}
    assert "oncall" in _cmd("")["text"]
    assert "No open incidents" in _cmd("incidents")["text"]
    iid = _fire(client, setup)
    assert "#1 CrashLoop" in _cmd("incidents")["text"]
    assert _cmd("ack 1")["text"] == "#1 acknowledged."
    assert _cmd("level 1 P2")["text"] == "#1 set to P2."
    assert "no incident #9" in _cmd("ack 9")["text"]
    assert "For how long" in _cmd("snooze 1 soon")["text"]
    assert _cmd("show 1")["text"] == "Posted."
    assert SessionLocal.scalars(select(SlackMessage)).one().incident_id == iid
    assert "already in this channel" in _cmd("show 1")["text"]
    assert "No schedules yet" in _cmd("oncall")["text"]


# --- settings ------------------------------------------------------------------------------------

def test_slack_tokens_are_write_only_and_validated(client, setup, monkeypatch):
    monkeypatch.setattr(slack, "reconcile", lambda: None)
    r = client.put("/api/v1/settings", headers=bearer(setup["tok"]), json={"slack_bot_token": "xapp-wrong"})
    assert r.status_code == 422
    r = client.put("/api/v1/settings", headers=bearer(setup["tok"]),
                   json={"slack_bot_token": "xoxb-1-2-abc", "slack_app_token": "xapp-1-A-xyz"})
    assert r.status_code == 200 and "slack" in r.json
    row = {s["key"]: s for s in client.get("/api/v1/settings", headers=bearer(setup["tok"])).json["settings"]}
    assert row["slack_bot_token"]["value"] is None and row["slack_bot_token"]["is_set"]
    assert "xoxb-1-2-abc" not in str(SessionLocal.scalars(select(AuditEntry)).all()[-1].detail)


def test_incident_levels_setting(client, setup):
    r = client.put("/api/v1/settings", headers=bearer(setup["tok"]), json={"incident_levels": [
        {"name": "SEV1", "severity": "critical"}, {"name": "SEV2", "severity": "warning"}]})
    assert r.status_code == 200
    assert [x["name"] for x in client.get("/api/v1/levels", headers=bearer(setup["tok"])).json["levels"]] \
        == ["SEV1", "SEV2"]
    iid = _fire(client, setup, severity="warning")
    assert _inc(iid).level == "SEV2"
    assert client.put("/api/v1/settings", headers=bearer(setup["tok"]),
                      json={"incident_levels": []}).status_code == 422


def test_manifest_is_socket_mode_with_the_scopes_the_code_uses(client, setup):
    m = client.get("/api/v1/slack/manifest", headers=bearer(setup["tok"])).json["manifest"]
    assert m["settings"]["socket_mode_enabled"] and m["settings"]["interactivity"]["is_enabled"]
    assert {"chat:write", "users:read.email", "im:write", "commands"} <= set(m["oauth_config"]["scopes"]["bot"])


def test_new_incident_api_actions(client, setup, outbox):  # noqa: F811
    iid = _fire(client, setup)
    h = bearer(setup["tok"])
    assert client.post(f"/api/v1/incidents/{iid}/notes", headers=h, json={"text": "on it"}).status_code == 200
    r = client.post(f"/api/v1/incidents/{iid}/snooze", headers=h, json={"minutes": 30})
    assert r.status_code == 200 and r.json["silence_id"]
    assert client.post(f"/api/v1/incidents/{iid}/snooze", headers=h,
                       json={"minutes": 3000}).status_code == 422
    r = client.post(f"/api/v1/incidents/{iid}/responders", headers=h, json={"user_id": setup["bob"].id})
    assert r.status_code == 200
    assert any(n["target"].endswith("(added by boss@example.com)") for n in r.json["notifications"])
    client.post(f"/api/v1/incidents/{iid}/ack", headers=h)
    assert client.post(f"/api/v1/incidents/{iid}/escalate", headers=h).status_code == 409
    channel = SessionLocal.scalars(select(Channel)).all()
    assert channel == []


# --- Socket Mode envelopes ---------------------------------------------------------------------

class _Sock:
    def __init__(self):
        self.acks = []

    def send_socket_mode_response(self, resp):
        self.acks.append((resp.envelope_id, resp.payload))


class _Req:
    def __init__(self, type_, payload, envelope_id="E1"):
        self.type, self.payload, self.envelope_id = type_, payload, envelope_id


def test_socket_listener_acks_clicks_first_and_answers_modals_and_commands(client, setup, fake, monkeypatch):
    fake.users["U0ANN"] = {"email": "ann@example.com"}
    iid = _fire(client, setup)
    sock = _Sock()
    order = []
    real = slack.handle_block_action
    monkeypatch.setattr(slack, "handle_block_action", lambda p: (order.append(len(sock.acks)), real(p)))
    slack._listener(sock, _Req("interactive", _click("sh_ack", iid)))
    assert order == [1] and sock.acks == [("E1", None)]          # acked BEFORE the work
    assert _inc(iid).status == "acknowledged"

    slack._listener(sock, _Req("interactive", _submit("sh_note", iid, {"note": ""}), "E2"))
    assert sock.acks[-1][0] == "E2" and sock.acks[-1][1]["response_action"] == "errors"

    slack._listener(sock, _Req("slash_commands", {"text": "incidents", "user_id": "U0ANN",
                                                  "team_id": TEAM}, "E3"))
    assert sock.acks[-1][0] == "E3" and "#1" in sock.acks[-1][1]["text"]


def test_api_turns_slack_errors_into_reasons_without_the_token(client, monkeypatch):
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    st.put({"slack_bot_token": "xoxb-secret-123"}, "test")
    SessionLocal.commit()

    def boom(self, method, **kw):
        raise SlackApiError("xoxb-secret-123 failed", {"ok": False, "error": "not_in_channel"})
    monkeypatch.setattr(WebClient, "api_call", boom)
    with pytest.raises(slack.SlackError) as e:
        slack.api("chat.postMessage", channel="C1", text="x")
    assert "invite it" in str(e.value) and "xoxb" not in str(e.value)


# --- asking a team for help ---------------------------------------------------------------------

def test_ping_opens_an_incident_for_the_team_and_pages_it_now(client, setup, fake, outbox):  # noqa: F811
    fake.users["U0BOB"] = {"email": "bob@example.com"}
    out = slack.handle_command({"text": "ping Platform P2 the payments db is read-only",
                                "user_id": "U0BOB", "team_id": TEAM, "channel_id": "C0WAR",
                                "channel_name": "war-room"})
    assert out["text"].startswith(":sos: Paging *Platform* now (P2)")
    (inc,) = _incidents()
    assert (inc.team_id, inc.escalation_policy_id) == (setup["team"]["id"], setup["policy"]["id"])
    assert (inc.level, inc.severity, inc.status) == ("P2", "high", "triggered")
    assert inc.title == "Platform: the payments db is read-only"
    assert inc.alerts[0].annotations["requested_by"] == "bob@example.com"
    # The live message went into the channel it was asked from; step 1 pages ann by email.
    assert [p["channel"] for p in fake.of("chat.postMessage")] == ["C0WAR"]
    _dispatch()
    assert [to for to, *_ in outbox["email"]] == ["ann@example.com"]
    # It never times out by itself, and resolving it works like any incident.
    assert slack.handle_command({"text": "resolve 1 db failed over", "user_id": "U0BOB",
                                 "team_id": TEAM})["text"] == "#1 resolved."


def test_ping_history_counts_earlier_requests_to_the_same_team(client, setup):
    h = bearer(setup["tok"])
    a = client.post("/api/v1/incidents", headers=h, json={"team_id": setup["team"]["id"], "message": "one"}).json
    b = client.post("/api/v1/incidents", headers=h, json={"team_id": setup["team"]["id"], "message": "two"}).json
    assert a["number"] != b["number"] and a["level"] == "P1"        # separate incidents, most urgent
    assert a["alerts"][0]["signature"] == b["alerts"][0]["signature"]  # one history
    # Snoozing one request does not silence the other.
    client.post(f"/api/v1/incidents/{a['id']}/snooze", headers=h, json={"minutes": 30})
    s = SessionLocal.scalars(select(Silence)).one()
    assert {m["name"]: m["value"] for m in s.matchers}["request"] == a["alerts"][0]["labels"]["request"]


def test_ping_says_what_is_missing(client, setup, fake):
    fake.users["U0ANN"] = {"email": "ann@example.com"}
    assert "Which team?" in _cmd("ping")["text"]
    assert "No team `nope`" in _cmd("ping nope help")["text"] and "`platform`" in _cmd("ping nope x")["text"]
    assert "Say what you need" in _cmd("ping platform P1")["text"]
    client.post("/api/v1/teams", headers=bearer(setup["tok"]), json={"slug": "techops", "name": "TechOps"})
    assert "has no escalation policy" in _cmd("ping techops disk full")["text"]
    assert _incidents() == []
