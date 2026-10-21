import datetime as dt

from sqlalchemy import select

from app import alerts as alert_ops
from app.db import SessionLocal
from app.models import Occurrence, now
from tests.conftest import bearer, make_integration

PROM = "/ingest/prometheus/api/v2/alerts"


def _iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _prom_alert(pod="api-1", ends_in=dt.timedelta(minutes=4), **labels):
    t = now()
    return {"labels": {"alertname": "CrashLoop", "namespace": "service", "pod": pod, **labels},
            "annotations": {"summary": "pod crash looping"},
            "startsAt": _iso(t - dt.timedelta(minutes=1)),
            "endsAt": _iso(t + ends_in),
            "generatorURL": "http://prometheus/graph"}


def _occurrences():
    SessionLocal.expire_all()
    return SessionLocal.scalars(select(Occurrence).order_by(Occurrence.started_at)).all()


def test_prometheus_push_opens_then_updates_one_row(client):
    _, tok = make_integration("prometheus")
    r = client.post(PROM, json=[_prom_alert()], headers=bearer(tok))
    assert r.status_code == 200 and r.json["opened"] == 1
    r = client.post(PROM, json=[_prom_alert()], headers=bearer(tok))
    assert r.json["updated"] == 1
    rows = _occurrences()
    assert len(rows) == 1 and rows[0].send_count == 2 and rows[0].status == "firing"


def test_endsat_in_past_resolves_from_source(client):
    _, tok = make_integration("prometheus")
    client.post(PROM, json=[_prom_alert()], headers=bearer(tok))
    r = client.post(PROM, json=[_prom_alert(ends_in=-dt.timedelta(seconds=5))],
                    headers=bearer(tok))
    assert r.json["resolved"] == 1
    (occ,) = _occurrences()
    assert occ.status == "resolved" and occ.resolved_how == "source"


def test_refiring_opens_second_occurrence_with_same_signature(client):
    _, tok = make_integration("prometheus")
    client.post(PROM, json=[_prom_alert()], headers=bearer(tok))
    client.post(PROM, json=[_prom_alert(ends_in=-dt.timedelta(seconds=5))], headers=bearer(tok))
    client.post(PROM, json=[_prom_alert(pod="api-2")], headers=bearer(tok))
    first, second = _occurrences()
    assert first.id != second.id
    assert first.signature == second.signature
    assert first.fingerprint != second.fingerprint


def test_wrong_kind_or_missing_token_is_refused(client):
    _, wh_tok = make_integration("webhook")
    assert client.post(PROM, json=[_prom_alert()], headers=bearer(wh_tok)).status_code == 401
    assert client.post(PROM, json=[_prom_alert()]).status_code == 401
    assert client.post(PROM, json=[_prom_alert()],
                       headers=bearer("shi_nonsense")).status_code == 401


def test_watchdog_is_recorded_not_opened(client):
    integ, tok = make_integration("prometheus")
    client.post(PROM, json=[{"labels": {"alertname": "Watchdog"}}], headers=bearer(tok))
    assert _occurrences() == []
    from app.models import Integration
    assert SessionLocal.get(Integration, integ.id).last_watchdog_at is not None


def test_malformed_rows_are_skipped_not_fatal(client):
    _, tok = make_integration("prometheus")
    batch = [_prom_alert(), "nonsense", {"labels": {"no": "alertname"}},
             {"labels": ["not", "a", "map"]}]
    r = client.post(PROM, json=batch, headers=bearer(tok))
    assert r.status_code == 200
    assert r.json["opened"] == 1 and r.json["ignored"] == 3


def test_sweep_resolves_alerts_that_stopped_arriving(client):
    _, tok = make_integration("prometheus")
    client.post(PROM, json=[_prom_alert()], headers=bearer(tok))
    (occ,) = _occurrences()
    occ.expires_at = now() - dt.timedelta(seconds=1)
    SessionLocal.commit()
    assert alert_ops.sweep_expired() == 1
    (occ,) = _occurrences()
    assert occ.resolved_how == "timeout"


def test_extra_labels_tag_every_alert(client):
    _, tok = make_integration("prometheus", extra_labels={"cluster": "big"})
    client.post(PROM, json=[_prom_alert()], headers=bearer(tok))
    assert _occurrences()[0].labels["cluster"] == "big"


def test_alertmanager_webhook(client):
    _, tok = make_integration("alertmanager")
    payload = {"version": "4", "status": "firing", "alerts": [
        {"status": "firing", "labels": {"alertname": "DiskFull", "instance": "db1"},
         "annotations": {}, "startsAt": _iso(now()), "endsAt": "0001-01-01T00:00:00Z"}]}
    assert client.post("/ingest/alertmanager", json=payload,
                       headers=bearer(tok)).json["opened"] == 1
    payload["alerts"][0]["status"] = "resolved"
    assert client.post("/ingest/alertmanager", json=payload,
                       headers=bearer(tok)).json["resolved"] == 1


def test_generic_webhook_single_object(client):
    _, tok = make_integration("webhook")
    r = client.post("/ingest/webhook", headers=bearer(tok), json={
        "name": "BackupFailed", "severity": "critical", "summary": "nightly backup failed",
        "labels": {"service": "billing"}})
    assert r.json["opened"] == 1
    occ = _occurrences()[0]
    assert occ.alertname == "BackupFailed" and occ.severity == "critical"
    assert occ.annotations["summary"] == "nightly backup failed"


def test_batch_cap(client, monkeypatch):
    from app import config as cfg
    monkeypatch.setattr(cfg, "MAX_ALERTS_PER_REQUEST", 2)
    _, tok = make_integration("prometheus")
    r = client.post(PROM, json=[_prom_alert(pod=str(i)) for i in range(3)], headers=bearer(tok))
    assert r.status_code == 413
