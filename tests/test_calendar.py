"""iCal feeds of on-call shifts."""
import datetime as dt

from app.core import ical
from app.db import SessionLocal
from app.models import User
from tests.conftest import bearer, make_token, make_user
from tests.test_oncall import _schedule


def _events(body: str) -> list[dict]:
    """Unfold and split a .ics body into its VEVENTs (property → value)."""
    text = body.replace("\r\n ", "")
    out, cur = [], None
    for line in text.split("\r\n"):
        if line == "BEGIN:VEVENT":
            cur = {}
        elif line == "END:VEVENT":
            out.append(cur)
            cur = None
        elif cur is not None and ":" in line:
            k, v = line.split(":", 1)
            cur[k.split(";")[0]] = v
    return out


def test_ical_escapes_folds_and_uses_crlf():
    t = dt.datetime(2026, 10, 16, 9, tzinfo=dt.timezone.utc)
    body = ical.calendar("Ops; on, call", [{"uid": "u1@x", "start": t, "end": t + dt.timedelta(hours=8),
                                             "summary": "Å" * 60 + ", night\nshift; long"}], t)
    assert body.startswith("BEGIN:VCALENDAR\r\n") and body.endswith("END:VCALENDAR\r\n")
    assert "X-WR-CALNAME:Ops\; on\\, call" in body
    assert all(len(line.encode()) <= 75 for line in body.split("\r\n"))   # folded by octets
    ev = _events(body)[0]
    assert ev["DTSTART"] == "20261016T090000Z" and ev["DTEND"] == "20261016T170000Z"
    assert ev["SUMMARY"] == "Å" * 60 + "\\, night\\nshift\; long"


def test_feed_lifecycle_and_content(client):
    ann = make_user(email="ann@example.com", name="Ann")
    bob = make_user(email="bob@example.com", name="Bob")
    admin = make_token(make_user(email="boss@example.com", role="admin"), ["admin"])
    sched = _schedule(client, admin, [ann, bob], name="platform")
    tok = make_token(ann, ["oncall:read"])

    r = client.post("/api/v1/me/calendar-feeds", headers=bearer(tok), json={"kind": "mine"})
    assert r.status_code == 201 and r.json["url"].endswith(".ics") and r.json["webcal_url"].startswith("webcal://")
    path = "/" + r.json["url"].split("://", 1)[1].split("/", 1)[1]
    assert "url" not in client.get("/api/v1/me/calendar-feeds", headers=bearer(tok)).json[0]  # shown once

    # No session, no header: the URL is the credential.
    feed = client.get(path)
    assert feed.status_code == 200 and feed.mimetype == "text/calendar"
    mine = _events(feed.get_data(as_text=True))
    assert mine and all(e["SUMMARY"].startswith("On call: platform") for e in mine)
    again = _events(client.get(path).get_data(as_text=True))
    assert [e["UID"] for e in again] == [e["UID"] for e in mine]   # stable across fetches

    # The schedule feed shows everyone, alternating Ann and Bob day by day.
    r = client.post("/api/v1/me/calendar-feeds", headers=bearer(tok), json={"kind": "schedule", "schedule_id": sched["id"]})
    spath = "/" + r.json["url"].split("://", 1)[1].split("/", 1)[1]
    names = {e["SUMMARY"] for e in _events(client.get(spath).get_data(as_text=True))}
    assert {"platform: Ann on call", "platform: Bob on call"} <= names

    # Revoking, or the owner losing access, ends the feed — and answers like an unknown token.
    fid = client.get("/api/v1/me/calendar-feeds", headers=bearer(tok)).json[-1]["id"]
    client.delete(f"/api/v1/me/calendar-feeds/{fid}", headers=bearer(tok))
    assert client.get(path).status_code == 404
    SessionLocal.get(User, ann.id).disabled = True
    SessionLocal.commit()
    assert client.get(spath).status_code == 404
    assert client.get("/calendar/shc_nope.ics").status_code == 404


def test_feed_validation_ownership_and_download(client):
    ann = make_user(email="ann@example.com")
    tok = make_token(ann, ["oncall:read"])
    other = make_token(make_user(email="eve@example.com"), ["oncall:read"])
    assert client.post("/api/v1/me/calendar-feeds", headers=bearer(tok),
                       json={"kind": "schedule", "schedule_id": "nope"}).status_code == 422
    fid = client.post("/api/v1/me/calendar-feeds", headers=bearer(tok), json={"kind": "mine"}).json["id"]
    assert client.delete(f"/api/v1/me/calendar-feeds/{fid}", headers=bearer(other)).status_code == 404
    r = client.get("/api/v1/me/calendar.ics", headers=bearer(tok))
    assert r.status_code == 200 and r.mimetype == "text/calendar" and "attachment" in r.headers["Content-Disposition"]


def test_before_the_schedule_starts_is_not_a_gap(client):
    import datetime as _dt
    from app.models import now
    ann = make_user(email="ann@example.com", name="Ann")
    admin = make_token(make_user(email="boss@example.com", role="admin"), ["admin"])
    start = (now() + _dt.timedelta(days=3)).replace(minute=0, second=0, microsecond=0)
    r = client.post("/api/v1/schedules", headers=bearer(admin), json={"name": "later", "timezone": "UTC", "layers": [
        {"name": "d", "users": [ann.id], "rotation_hours": 24, "start": start.strftime("%Y-%m-%dT%H:%M")}]})
    tok = make_token(ann, ["oncall:read"])
    body = client.get(f"/api/v1/me/calendar.ics?schedule_id={r.json['id']}", headers=bearer(tok)).get_data(as_text=True)
    events = _events(body)
    assert events and not any("Nobody" in e["SUMMARY"] for e in events)
    assert events[0]["DTSTART"] == start.strftime("%Y%m%dT%H%M%SZ")
