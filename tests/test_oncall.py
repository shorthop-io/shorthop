import datetime as dt

import pytest

from app import delivery, incidents, schedules
from app.core import oncall
from app.db import SessionLocal
from app.models import Notification, Schedule, now
from tests.conftest import bearer, make_integration, make_token, make_user
from tests.test_ingest import PROM, _prom_alert

UTC = dt.timezone.utc


def T(s):
    return dt.datetime.fromisoformat(s).replace(tzinfo=UTC)


WEEKLY = {"timezone": "Europe/Stockholm", "layers": [
    {"name": "24/7", "users": ["ann", "bob", "cy"], "rotation_hours": 168,
     "start": "2026-10-05T09:00", "restriction": None}]}


def test_weekly_rotation_hands_off_at_local_time():
    # Mon 5 Oct 09:00 Stockholm (CEST, UTC+2) = 07:00 UTC
    assert oncall.on_call(WEEKLY, [], T("2026-10-05T06:59"))["user_id"] is None   # before start
    assert oncall.on_call(WEEKLY, [], T("2026-10-05T07:00"))["user_id"] == "ann"
    assert oncall.on_call(WEEKLY, [], T("2026-10-12T07:00"))["user_id"] == "bob"
    assert oncall.on_call(WEEKLY, [], T("2026-10-19T07:00"))["user_id"] == "cy"


def test_handoff_stays_at_0900_across_the_dst_change():
    # DST ends Sun 25 Oct. Mon 26 Oct 09:00 Stockholm is now CET (UTC+1) = 08:00 UTC.
    # Counting elapsed hours would hand off at 07:00 UTC (= 08:00 local) — an hour early.
    assert oncall.on_call(WEEKLY, [], T("2026-10-26T07:30"))["user_id"] == "cy"
    assert oncall.on_call(WEEKLY, [], T("2026-10-26T08:00"))["user_id"] == "ann"


def test_higher_layer_wins_where_it_covers_and_night_window_wraps():
    sched = {"timezone": "UTC", "layers": [
        {"name": "24/7", "users": ["ann"], "rotation_hours": 168, "start": "2026-10-05T00:00", "restriction": None},
        {"name": "nights", "users": ["bob"], "rotation_hours": 168, "start": "2026-10-05T00:00",
         "restriction": {"days": [0], "start": "22:00", "end": "06:00"}},  # Monday night only
    ]}
    assert oncall.on_call(sched, [], T("2026-10-05T23:00"))["user_id"] == "bob"   # Mon 23:00
    assert oncall.on_call(sched, [], T("2026-10-06T05:00"))["user_id"] == "bob"   # Tue 05:00, Monday's night
    assert oncall.on_call(sched, [], T("2026-10-06T23:00"))["user_id"] == "ann"   # Tue night: not covered
    assert oncall.on_call(sched, [], T("2026-10-05T12:00"))["source"] == "layer:24/7"


def test_override_beats_layers_and_newest_override_wins():
    ov = [{"user_id": "zed", "starts_at": T("2026-10-06T00:00"), "ends_at": T("2026-10-07T00:00"),
           "created_at": T("2026-10-01T00:00")},
          {"user_id": "yan", "starts_at": T("2026-10-06T10:00"), "ends_at": T("2026-10-06T12:00"),
           "created_at": T("2026-10-02T00:00")}]
    assert oncall.on_call(WEEKLY, ov, T("2026-10-06T09:00"))["user_id"] == "zed"
    assert oncall.on_call(WEEKLY, ov, T("2026-10-06T11:00")) == {"user_id": "yan", "source": "override"}


def test_shifts_are_contiguous_and_merged():
    ov = [{"user_id": "zed", "starts_at": T("2026-10-06T00:00"), "ends_at": T("2026-10-06T12:00"),
           "created_at": T("2026-10-01T00:00")}]
    segs = oncall.shifts(WEEKLY, ov, T("2026-10-05T00:00"), T("2026-10-13T00:00"))
    assert segs[0]["start"] == T("2026-10-05T00:00") and segs[-1]["end"] == T("2026-10-13T00:00")
    for a, b in zip(segs, segs[1:]):
        assert a["end"] == b["start"] and (a["user_id"], a["source"]) != (b["user_id"], b["source"])
    assert [s["user_id"] for s in segs] == [None, "ann", "zed", "ann", "bob"]


@pytest.mark.parametrize("bad", [
    [], [{"users": [], "rotation_hours": 24, "start": "2026-10-05T09:00"}],
    [{"users": ["a"], "rotation_hours": 0, "start": "2026-10-05T09:00"}],
    [{"users": ["a"], "rotation_hours": 24, "start": "next monday"}],
    [{"users": ["a"], "rotation_hours": 24, "start": "2026-10-05T09:00", "restriction": {"start": "9"}}],
])
def test_layer_validation(bad):
    with pytest.raises(ValueError):
        oncall.validate_layers(bad)


# --- API and integration -----------------------------------------------------------------

def _schedule(client, tok, users, **kw):
    body = {"name": kw.pop("name", "primary"), "timezone": "UTC", "layers": [
        {"name": "daily", "users": [u.id for u in users], "rotation_hours": 24,
         # On the hour, so a shift change never lands on a half-hour window a test opens
         # (otherwise a run between hh:30 and hh:31 fails).
         "start": (now() - dt.timedelta(days=10)).strftime("%Y-%m-%dT%H:00")}], **kw}
    r = client.post("/api/v1/schedules", headers=bearer(tok), json=body)
    assert r.status_code == 201, r.json
    return r.json


def test_schedule_api_validation(client):
    tok = make_token(make_user(email="boss@example.com", role="admin"), ["admin"])
    bad_tz = {"name": "x", "timezone": "Mars/Olympus", "layers": [{"users": ["a"], "rotation_hours": 24, "start": "2026-10-05T09:00"}]}
    assert client.post("/api/v1/schedules", headers=bearer(tok), json=bad_tz).status_code == 422
    unknown = {"name": "x", "timezone": "UTC", "layers": [{"users": ["nobody"], "rotation_hours": 24, "start": "2026-10-05T09:00"}]}
    assert client.post("/api/v1/schedules", headers=bearer(tok), json=unknown).status_code == 422


def test_take_a_shift_yourself_but_not_for_someone_else(client):
    admin = make_token(make_user(email="boss@example.com", role="admin"), ["admin"])
    ann, bob = make_user(email="ann@example.com"), make_user(email="bob@example.com")
    s = _schedule(client, admin, [ann])
    bob_tok = make_token(bob, ["oncall:read", "oncall:write"])
    window = {"starts_at": now().isoformat(), "ends_at": (now() + dt.timedelta(hours=8)).isoformat()}
    r = client.post(f"/api/v1/schedules/{s['id']}/overrides", headers=bearer(bob_tok), json=window)
    assert r.status_code == 201
    on = client.get("/api/v1/oncall", headers=bearer(bob_tok)).json
    assert on[0]["user"]["email"] == "bob@example.com" and on[0]["source"] == "override"
    r = client.post(f"/api/v1/schedules/{s['id']}/overrides", headers=bearer(bob_tok),
                    json={**window, "user_id": ann.id})
    assert r.status_code == 403
    mine = client.get("/api/v1/me/shifts", headers=bearer(bob_tok)).json
    assert mine and mine[0]["source"] == "override"


def test_escalation_to_a_schedule_pages_whoever_is_on_call(client, monkeypatch):
    sent = []
    monkeypatch.setattr(delivery, "send_email", lambda to, s, b: sent.append(to))
    admin = make_token(make_user(email="boss@example.com", role="admin"), ["admin"])
    ann = make_user(email="ann@example.com")
    s = _schedule(client, admin, [ann])
    pol = client.post("/api/v1/escalation-policies", headers=bearer(admin), json={
        "name": "via schedule", "steps": [{"targets": [{"type": "schedule", "id": s["id"]}]}]}).json
    client.post("/api/v1/routes", headers=bearer(admin), json={"name": "all", "escalation_policy_id": pol["id"]})
    _, itok = make_integration("prometheus")
    client.post(PROM, json=[_prom_alert()], headers=bearer(itok))
    incidents.run_due()
    delivery.deliver_pending()
    assert sent == ["ann@example.com"]
    (n,) = SessionLocal.query(Notification).all()
    assert n.target == "user ann@example.com (on call)"
    r = client.delete(f"/api/v1/schedules/{s['id']}", headers=bearer(admin))
    assert r.status_code == 409


def test_handover_notice_on_change_but_not_on_first_look(client, monkeypatch):
    sent = []
    monkeypatch.setattr(delivery, "send_email", lambda to, s, b: sent.append((to, s)))
    admin = make_token(make_user(email="boss@example.com", role="admin"), ["admin"])
    ann, bob = make_user(email="ann@example.com"), make_user(email="bob@example.com")
    s = _schedule(client, admin, [ann])
    schedules.check_handovers()
    assert sent == []                                   # first observation is not a handover
    bob_tok = make_token(bob, ["oncall:read", "oncall:write"])
    client.post(f"/api/v1/schedules/{s['id']}/overrides", headers=bearer(bob_tok), json={
        "starts_at": (now() - dt.timedelta(minutes=1)).isoformat(),
        "ends_at": (now() + dt.timedelta(hours=2)).isoformat()})
    schedules.check_handovers()
    schedules.check_handovers()
    assert [to for to, _ in sent] == ["bob@example.com"]
    assert sent[0][1].startswith("[ON CALL]")
    assert SessionLocal.get(Schedule, s["id"]).last_on_call == bob.id


def test_framed_shifts_carry_the_whole_shift_a_window_cuts():
    # A 14-day view opening on Wed 14 Oct cuts bob's week (Mon 12 Oct 09:00 → Mon 19 Oct 09:00)
    # at the window edge; picking that shift must mean the whole week.
    segs = oncall.framed_shifts(WEEKLY, [], T("2026-10-14T00:00"), T("2026-10-28T00:00"))
    first, last = segs[0], segs[-1]
    assert first["user_id"] == "bob" and first["start"] == T("2026-10-14T00:00")
    assert first["shift_start"] == T("2026-10-12T07:00") and first["shift_end"] == T("2026-10-19T07:00")
    assert last["user_id"] == "ann" and last["end"] == T("2026-10-28T00:00")
    assert last["shift_end"] == T("2026-11-02T08:00")          # Mon 2 Nov 09:00 CET
    # Drawing is unchanged: the clipped segments are exactly what `shifts` gives.
    plain = oncall.shifts(WEEKLY, [], T("2026-10-14T00:00"), T("2026-10-28T00:00"))
    assert [(s["user_id"], s["start"], s["end"]) for s in segs] == [(s["user_id"], s["start"], s["end"]) for s in plain]


def test_shifts_endpoint_returns_the_whole_shift_of_a_cut_segment(client):
    a, b = make_user(email="a@example.com"), make_user(email="b@example.com")
    tok = make_token(make_user(email="boss@example.com", role="admin"), ["admin"])
    sid = _schedule(client, tok, [a, b])["id"]
    start = now().replace(minute=0, second=0, microsecond=0) + dt.timedelta(minutes=30)
    r = client.get(f"/api/v1/schedules/{sid}/shifts", query_string={"days": 3, "start": start.isoformat()},
                   headers=bearer(tok)).json
    seg = r["final"][0]
    assert dt.datetime.fromisoformat(seg["start"]) == start
    assert dt.datetime.fromisoformat(seg["shift_start"]) < start      # the daily shift began earlier
    assert "shift_start" in r["layers"][0]["shifts"][0]
