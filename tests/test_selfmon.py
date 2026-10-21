import datetime as dt

import pytest
from sqlalchemy import select

from app import config as cfg
from app import promclient, selfmon
from app.core import shadow
from app.db import SessionLocal
from app.models import Integration, Occurrence, now
from tests.conftest import bearer, make_integration, make_token, make_user
from tests.test_ingest import PROM, _prom_alert
from tests.test_label_rules import SEV_WARNING

EXT = {"cluster", "env", "region", "prometheus"}


# --- the pure comparison ---------------------------------------------------------------

def test_compare_ignores_external_labels_and_replica():
    prom = [{"alertname": "X", "pod": "a"}]
    mine = [{"alertname": "X", "pod": "a", "cluster": "big", "env": "lab",
             "prometheus_replica": "p-0"}]
    first = shadow.compare(prom, mine, EXT, None)
    assert first["missing_keys"] == [] and first["extra_keys"] == []


def test_difference_counts_only_when_seen_twice():
    prom = [{"alertname": "X", "pod": "a"}, {"alertname": "Y"}]
    mine = [{"alertname": "X", "pod": "a"}]
    first = shadow.compare(prom, mine, EXT, None)
    assert first["missing"] == [] and first["unconfirmed"] == 1
    second = shadow.compare(prom, mine, EXT, first)
    assert [m["alertname"] for m in second["missing"]] == ["Y"]
    # it arrived in between: no longer missing
    third = shadow.compare(prom, mine + [{"alertname": "Y"}], EXT, second)
    assert third["missing"] == []


def test_watchdog_is_left_out_on_both_sides():
    r = shadow.compare([{"alertname": "Watchdog"}], [], EXT, {"missing_keys": []})
    assert r["prometheus_firing"] == 0


# --- the URL allowlist -------------------------------------------------------------------

def test_url_must_be_on_the_allowlist(monkeypatch):
    monkeypatch.setattr(cfg, "PROMETHEUS_ALLOWED_HOSTS", ["prom.monitoring"])
    assert promclient.check_url("http://prom.monitoring:9090/") == "http://prom.monitoring:9090"
    for bad in ("http://169.254.169.254/", "file:///etc/passwd", "http://evil.example/",
                "http://user:pw@prom.monitoring:9090", "gopher://prom.monitoring/"):
        with pytest.raises(promclient.PrometheusError):
            promclient.check_url(bad)


def test_request_layer_refuses_a_host_off_the_list_even_if_stored(monkeypatch):
    monkeypatch.setattr(cfg, "PROMETHEUS_ALLOWED_HOSTS", ["prom.monitoring"])
    called = []
    monkeypatch.setattr(promclient.requests, "get", lambda *a, **k: called.append(a))
    with pytest.raises(promclient.PrometheusError):
        promclient.firing_alerts("http://evil.example")
    assert called == []


def test_api_refuses_disallowed_prometheus_url(client, monkeypatch):
    monkeypatch.setattr(cfg, "PROMETHEUS_ALLOWED_HOSTS", ["prom.monitoring"])
    admin = make_token(make_user(email="boss@example.com", role="admin"), ["admin"])
    integ, _ = make_integration("prometheus")
    r = client.patch(f"/api/v1/integrations/{integ.id}", headers=bearer(admin),
                     json={"prometheus_url": "http://169.254.169.254/latest"})
    assert r.status_code == 422 and "not allowed" in r.json["message"]
    r = client.patch(f"/api/v1/integrations/{integ.id}", headers=bearer(admin),
                     json={"prometheus_url": "http://prom.monitoring:9090"})
    assert r.status_code == 200 and r.json["prometheus_url"] == "http://prom.monitoring:9090"


# --- self-monitoring end to end --------------------------------------------------------------

def _self_alerts(name):
    SessionLocal.expire_all()
    return SessionLocal.scalars(select(Occurrence).where(Occurrence.alertname == name)).all()


@pytest.fixture()
def fake_prom(monkeypatch):
    monkeypatch.setattr(cfg, "PROMETHEUS_ALLOWED_HOSTS", ["prom.monitoring"])
    state = {"firing": []}
    monkeypatch.setattr(promclient, "firing_alerts", lambda base: state["firing"])
    monkeypatch.setattr(promclient, "external_label_names", lambda base: EXT)
    return state


def test_shadow_mismatch_raised_after_two_checks_and_resolved(client, fake_prom):
    integ, tok = make_integration("prometheus", prometheus_url="http://prom.monitoring:9090")
    client.post(PROM, json=[_prom_alert(pod="a")], headers=bearer(tok))
    sent = dict(_prom_alert(pod="a")["labels"])
    fake_prom["firing"] = [sent, {"alertname": "NeverArrived", "severity": "critical"}]

    integ = SessionLocal.get(Integration, integ.id)
    selfmon.check_shadow(integ)
    assert _self_alerts("ShorthopShadowMismatch") == []        # one sighting is not enough
    status = selfmon.check_shadow(SessionLocal.get(Integration, integ.id))
    assert [m["alertname"] for m in status["missing"]] == ["NeverArrived"]
    (mm,) = _self_alerts("ShorthopShadowMismatch")
    assert mm.status == "firing" and mm.labels["integration"] == integ.name

    fake_prom["firing"] = [sent]
    selfmon.check_shadow(SessionLocal.get(Integration, integ.id))
    (mm,) = _self_alerts("ShorthopShadowMismatch")
    assert mm.status == "resolved"


def test_shadow_ignores_labels_set_by_label_rules(client, fake_prom):
    admin = make_token(make_user(email="boss@example.com", role="admin"), ["admin"])
    for body in ({"name": "warn->P2", "matchers": [SEV_WARNING],
                  "set_labels": {"level": "P2"}},
                 {"name": "sev->page", "matchers": [SEV_WARNING],
                  "set_labels": {"severity": "page"}}):
        assert client.post("/api/v1/label-rules", headers=bearer(admin), json=body).status_code == 201
    integ, tok = make_integration("prometheus", prometheus_url="http://prom.monitoring:9090")
    client.post(PROM, json=[_prom_alert(pod="a", severity="warning")], headers=bearer(tok))
    fake_prom["firing"] = [dict(_prom_alert(pod="a", severity="warning")["labels"])]

    selfmon.check_shadow(SessionLocal.get(Integration, integ.id))
    status = selfmon.check_shadow(SessionLocal.get(Integration, integ.id))
    assert status["missing"] == [] and status["extra"] == []
    assert status["missing_keys"] == [] and status["extra_keys"] == []


def test_shadow_check_reports_an_unreachable_prometheus(client, monkeypatch):
    monkeypatch.setattr(cfg, "PROMETHEUS_ALLOWED_HOSTS", ["prom.monitoring"])

    def boom(base):
        raise promclient.PrometheusError("could not reach Prometheus (ConnectionError)")
    monkeypatch.setattr(promclient, "external_label_names", boom)
    integ, _ = make_integration("prometheus", prometheus_url="http://prom.monitoring:9090")
    status = selfmon.check_shadow(SessionLocal.get(Integration, integ.id))
    assert "could not reach" in status["error"]


def test_ingest_silent_raised_when_watchdog_stops_and_cleared(client):
    integ, tok = make_integration("prometheus")
    selfmon.check_watchdogs()
    assert _self_alerts("ShorthopIngestSilent") == []          # never had one: no alarm

    client.post(PROM, json=[{"labels": {"alertname": "Watchdog"}}], headers=bearer(tok))
    row = SessionLocal.get(Integration, integ.id)
    row.last_watchdog_at = now() - dt.timedelta(minutes=cfg.WATCHDOG_STALE_MINUTES + 1)
    SessionLocal.commit()
    selfmon.check_watchdogs()
    (silent,) = _self_alerts("ShorthopIngestSilent")
    assert silent.status == "firing" and silent.severity == "critical"

    client.post(PROM, json=[{"labels": {"alertname": "Watchdog"}}], headers=bearer(tok))
    selfmon.check_watchdogs()
    (silent,) = _self_alerts("ShorthopIngestSilent")
    assert silent.status == "resolved"


def test_healthy_ticks_send_nothing(client):
    _, tok = make_integration("prometheus")
    client.post(PROM, json=[{"labels": {"alertname": "Watchdog"}}], headers=bearer(tok))
    for _ in range(3):
        selfmon.check_watchdogs()
    internal = SessionLocal.scalar(select(Integration).where(Integration.kind == "internal"))
    assert internal is None or internal.last_received_at is None


def test_builtin_integration_cannot_be_changed_or_posted_as(client):
    admin = make_token(make_user(email="boss@example.com", role="admin"), ["admin"])
    internal = selfmon.internal_integration()
    assert client.post(f"/api/v1/integrations/{internal.id}/rotate",
                       headers=bearer(admin)).status_code == 409
    assert client.patch(f"/api/v1/integrations/{internal.id}", headers=bearer(admin),
                        json={"name": "x"}).status_code == 409
