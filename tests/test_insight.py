import datetime as dt

from app.core import insight as core
from app.db import SessionLocal
from app.models import Incident, Occurrence, Team, now
from tests.conftest import bearer, make_integration, make_token, make_user
from tests.test_mcp import _tool

T0 = dt.datetime(2026, 10, 1, 12, 0, tzinfo=dt.timezone.utc)
M = dt.timedelta(minutes=1)


def ep(sig, start, end=None, *, fp=None, acked=None, how="source", cat=None, name=None):
    return core.Episode(signature=sig, fingerprint=fp or sig, alertname=name or sig, severity="warning",
                        started_at=start, resolved_at=end, acked_at=acked,
                        resolved_how=how if end else None, resolution_category=cat)


# --- pure rules ------------------------------------------------------------------------------

def test_percentile_interpolates_and_is_none_on_nothing():
    assert core.percentile([], 0.5) is None
    assert core.percentile([7], 0.9) == 7
    assert core.percentile([1, 2, 3, 4], 0.5) == 2.5          # percentile_cont, not nearest rank
    assert core.percentile([10, 0, 5], 0.5) == 5               # order of input does not matter


def test_flap_is_judged_per_series_not_per_alert():
    eps = [
        # one series re-firing 5 min after it resolved, twice → 2 flaps
        ep("A", T0, T0 + 2 * M), ep("A", T0 + 7 * M, T0 + 9 * M), ep("A", T0 + 14 * M, T0 + 16 * M),
        # another pod of the SAME alert starting right after → a different series, not a flap
        ep("B", T0, T0 + 2 * M, fp="b1"), ep("B", T0 + 3 * M, T0 + 4 * M, fp="b2"),
        ep("B", T0 + 5 * M, T0 + 6 * M, fp="b3"),
    ]
    rows = {r["signature"]: r for r in core.noise(eps, T0 + 60 * M)}
    assert rows["A"]["flaps"] == 2 and "flapping" in rows["A"]["flags"]
    assert rows["B"]["flaps"] == 0 and "flapping" not in rows["B"]["flags"]
    assert rows["B"]["series"] == 3


def test_a_refire_after_the_gap_is_not_a_flap():
    eps = [ep("A", T0, T0 + M), ep("A", T0 + M + core.FLAP_GAP + M, T0 + 30 * M)]
    assert core.noise(eps, T0 + 60 * M)[0]["flaps"] == 0


def test_nobody_acts_needs_unacked_AND_ended_by_itself():
    base = [ep("A", T0 + i * 60 * M, T0 + i * 60 * M + 10 * M) for i in range(4)]
    assert "nobody_acts" in core.noise(base, T0 + 300 * M)[0]["flags"]
    # A person resolving it by hand is a person acting.
    manual = [ep("A", T0 + i * 60 * M, T0 + i * 60 * M + 10 * M, how="manual") for i in range(4)]
    assert "nobody_acts" not in core.noise(manual, T0 + 300 * M)[0]["flags"]
    acked = [ep("A", T0 + i * 60 * M, T0 + i * 60 * M + 10 * M, acked=T0 + i * 60 * M) for i in range(4)]
    assert "nobody_acts" not in core.noise(acked, T0 + 300 * M)[0]["flags"]


def test_no_flags_from_too_little_evidence():
    eps = [ep("A", T0, T0 + M), ep("A", T0 + 2 * M, T0 + 3 * M)]
    assert core.noise(eps, T0 + 10 * M)[0]["flags"] == []


def test_noise_reports_usual_fix_and_open_episodes():
    eps = [ep("A", T0, T0 + 10 * M, cat="restarted"), ep("A", T0 + 60 * M, T0 + 70 * M, cat="restarted"),
           ep("A", T0 + 120 * M, T0 + 130 * M, cat="scaled"), ep("A", T0 + 200 * M)]
    r = core.noise(eps, T0 + 210 * M)[0]
    assert r["usual_fix"] == "restarted" and r["open"] == 1 and r["episodes"] == 4
    assert r["firing_s"] == 3 * 600 + 600  # the open one counts up to now


def test_response_percentiles_and_unacked():
    incs = [core.IncidentTimes(T0, T0 + 2 * M, T0 + 30 * M),
            core.IncidentTimes(T0, T0 + 10 * M, T0 + 60 * M),
            core.IncidentTimes(T0, None, T0 + 5 * M),     # ended by itself, nobody acked
            core.IncidentTimes(T0, None, None)]           # still open
    r = core.response(incs)
    assert r["incidents"] == 4 and r["acknowledged"] == 2 and r["resolved"] == 3
    assert r["resolved_unacked"] == 1 and r["open"] == 1
    assert r["time_to_ack"] == {"n": 2, "p50_s": 360.0, "p90_s": 552.0}
    assert r["time_to_resolve"]["n"] == 3 and r["time_to_resolve"]["p50_s"] == 1800.0
    assert core.response([])["time_to_ack"]["p50_s"] is None


def test_correlation_finds_cause_and_symptom_and_stays_within_one():
    eps = []
    for i in range(4):  # node down, and its pods crash within 2 min
        t = T0 + i * 120 * M
        eps += [ep("node", t, name="NodeDown"), ep("pod", t + 2 * M, name="PodCrash")]
    eps += [ep("pod", T0 + 1000 * M, name="PodCrash")]          # a crash on its own
    eps += [ep("disk", T0 + 30 * M), ep("disk", T0 + 400 * M)]  # unrelated
    # five A episodes huddled round ONE B are one coincidence, not five
    eps += [ep("burst", T0 + 2000 * M + i * 10 * dt.timedelta(seconds=1)) for i in range(5)]
    eps += [ep("lone", T0 + 2000 * M), ep("lone", T0 + 3000 * M)]
    pairs = core.correlations(eps)
    top = pairs[0]
    assert {top["a_alertname"], top["b_alertname"]} == {"NodeDown", "PodCrash"}
    assert top["together"] == 4 and top["ratio"] == 1.0
    assert all(p["ratio"] <= 1 for p in pairs)
    assert not any({p["a"], p["b"]} == {"burst", "lone"} for p in pairs)
    assert not any("disk" in (p["a"], p["b"]) for p in pairs)
    only = core.correlations(eps, signature="pod")
    assert [p["b"] for p in only] == ["node"]
    assert core.correlations(eps, signature="nope") == []


# --- API and MCP ---------------------------------------------------------------------------

def _seed():
    integ, _ = make_integration("prometheus")
    team = Team(slug="platform", name="Platform")
    SessionLocal.add(team)
    SessionLocal.commit()
    t = now() - dt.timedelta(days=2)
    for i in range(4):
        s = t + dt.timedelta(minutes=30 * i)
        for sig, name in (("s-node", "NodeDown"), ("s-pod", "PodCrash")):
            SessionLocal.add(Occurrence(
                integration_id=integ.id, team_id=team.id, fingerprint=sig, signature=sig,
                alertname=name, severity="critical", labels={"alertname": name},
                signature_labels={"alertname": name}, started_at=s + (dt.timedelta(minutes=1) if sig == "s-pod" else dt.timedelta()),
                last_seen_at=s, expires_at=s, status="resolved", resolved_at=s + dt.timedelta(minutes=3),
                resolved_how="source"))
        SessionLocal.add(Incident(number=i + 1, team_id=team.id, group_key=f"g{i}", title="x",
                                  opened_at=s, acked_at=s + dt.timedelta(minutes=4),
                                  resolved_at=s + dt.timedelta(minutes=20), status="resolved"))
    # history older than the window must not count
    old = now() - dt.timedelta(days=60)
    SessionLocal.add(Occurrence(integration_id=integ.id, fingerprint="s-node", signature="s-node",
                                alertname="NodeDown", started_at=old, last_seen_at=old, expires_at=old,
                                resolved_at=old, resolved_how="source", status="resolved"))
    SessionLocal.commit()


def test_insight_endpoint(client):
    _seed()
    tok = make_token(make_user(), ["alerts:read"])
    r = client.get("/api/v1/insight?days=30", headers=bearer(tok))
    assert r.status_code == 200
    d = r.json
    assert d["episodes"] == 8 and d["truncated"] is False
    node = next(x for x in d["noise"] if x["alertname"] == "NodeDown")
    assert node["episodes"] == 4 and node["team"] == "Platform" and "nobody_acts" in node["flags"]
    assert "T" in node["last_started_at"]                      # ISO, not an RFC-822 string
    assert d["response"]["time_to_ack"]["p50_s"] == 240.0
    assert d["response_by_team"][0]["team"] == "Platform"
    assert d["correlations"][0]["together"] == 4
    assert client.get("/api/v1/insight?days=91", headers=bearer(tok)).status_code == 422
    assert client.get("/api/v1/insight").status_code == 401
    rel = client.get("/api/v1/insight/related?signature=s-pod", headers=bearer(tok)).json
    assert rel["related"][0]["alertname"] == "NodeDown"


def test_insight_needs_alerts_read(client):
    tok = make_token(make_user(), ["oncall:read"])
    assert client.get("/api/v1/insight", headers=bearer(tok)).status_code == 403


def test_insight_through_mcp(client):
    _seed()
    tok = make_token(make_user(), ["alerts:read"])
    res, data = _tool(client, tok, "noise_report", days=30, limit=1)
    assert not res["isError"] and len(data["noise"]) == 1
    res, data = _tool(client, tok, "related_alerts", signature="s-node")
    assert data["related"][0]["alertname"] == "PodCrash"
    res, _ = _tool(client, tok, "related_alerts")
    assert res["isError"]
