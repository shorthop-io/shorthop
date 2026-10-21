"""Admin settings: Prometheus hosts and SMTP, overriding the environment."""
import pytest

from app import config as cfg
from app import delivery, promclient
from app import settings as st
from tests.conftest import bearer, make_token, make_user


@pytest.fixture()
def admin_tok():
    return make_token(make_user(email="root@example.com", role="admin"), ["admin"])


def _by_key(resp):
    return {s["key"]: s for s in resp.json["settings"]}


def test_settings_are_admin_only(client):
    resp_tok = make_token(make_user(email="r@example.com"), ["alerts:read", "oncall:read"])
    assert client.get("/api/v1/settings", headers=bearer(resp_tok)).status_code == 403
    assert client.put("/api/v1/settings", headers=bearer(resp_tok),
                      json={"smtp_host": "x.example"}).status_code == 403
    cw = make_token(make_user(email="cw@example.com", role="admin"), ["config:write"])
    assert client.get("/api/v1/settings", headers=bearer(cw)).status_code == 403


def test_override_then_reset_to_environment(client, admin_tok, monkeypatch):
    monkeypatch.setattr(cfg, "PROMETHEUS_ALLOWED_HOSTS", ["env-prom.example"])
    s = _by_key(client.get("/api/v1/settings", headers=bearer(admin_tok)))
    assert s["prometheus_allowed_hosts"] == {**s["prometheus_allowed_hosts"],
                                             "value": ["env-prom.example"], "source": "environment"}
    r = client.put("/api/v1/settings", headers=bearer(admin_tok),
                   json={"prometheus_allowed_hosts": ["Prom.Monitoring.svc", " ", "prom.monitoring.svc"]})
    assert r.status_code == 200 and r.json["changed"] == ["prometheus_allowed_hosts"]
    s = _by_key(r)
    assert s["prometheus_allowed_hosts"]["value"] == ["prom.monitoring.svc"]
    assert s["prometheus_allowed_hosts"]["source"] == "settings"
    # the override is what the Prometheus client actually checks against
    assert promclient.check_url("http://prom.monitoring.svc:9090")
    with pytest.raises(promclient.PrometheusError):
        promclient.check_url("http://env-prom.example")
    # null hands it back to the environment
    s = _by_key(client.put("/api/v1/settings", headers=bearer(admin_tok),
                           json={"prometheus_allowed_hosts": None}))
    assert s["prometheus_allowed_hosts"]["source"] == "environment"
    assert st.prometheus_allowed_hosts() == ["env-prom.example"]


def test_hosts_are_validated(client, admin_tok):
    for bad in (["http://prom"], ["prom:9090"], ["prom/x"], ["bad host"]):
        r = client.put("/api/v1/settings", headers=bearer(admin_tok),
                       json={"prometheus_allowed_hosts": bad})
        assert r.status_code == 422, bad
    r = client.put("/api/v1/settings", headers=bearer(admin_tok),
                   json={"prometheus_allowed_hosts": ["10.0.0.5", "[::1]"]})
    assert _by_key(r)["prometheus_allowed_hosts"]["value"] == ["10.0.0.5", "::1"]


def test_smtp_password_is_write_only_and_used(client, admin_tok, monkeypatch):
    r = client.put("/api/v1/settings", headers=bearer(admin_tok), json={
        "smtp_host": "smtp.example.com", "smtp_port": 465, "smtp_user": "u",
        "smtp_password": "hunter2", "smtp_from": "Shorthop <alerts@example.com>",
        "smtp_starttls": False})
    assert r.status_code == 200, r.json
    pw = _by_key(r)["smtp_password"]
    assert pw["value"] is None and pw["is_set"] is True
    assert "hunter2" not in r.get_data(as_text=True)
    # omitting the password keeps it
    client.put("/api/v1/settings", headers=bearer(admin_tok), json={"smtp_user": "u2"})
    sent = {}

    class FakeSSL:
        def __init__(self, host, port, timeout):
            sent.update(host=host, port=port)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, user, password):
            sent.update(user=user, password=password)

        def send_message(self, msg):
            sent.update(sender=msg["From"], to=msg["To"])
    monkeypatch.setattr(delivery.smtplib, "SMTP_SSL", FakeSSL)
    r = client.post("/api/v1/settings/smtp/test", headers=bearer(admin_tok), json={})
    assert r.json["ok"] is True and r.json["to"] == "root@example.com"
    assert sent == {"host": "smtp.example.com", "port": 465, "user": "u2", "password": "hunter2",
                    "sender": "Shorthop <alerts@example.com>", "to": "root@example.com"}


def test_smtp_values_are_validated(client, admin_tok):
    bad = [{"smtp_host": "smtp://x"}, {"smtp_host": "x.example:25"}, {"smtp_port": 0},
           {"smtp_from": "not an address"}, {"smtp_user": "a\r\nb"},
           {"smtp_password": "a\nb"}]
    for b in bad:
        assert client.put("/api/v1/settings", headers=bearer(admin_tok), json=b).status_code == 422, b


def test_smtp_test_reports_unconfigured(client, admin_tok, monkeypatch):
    monkeypatch.setattr(cfg, "SMTP_HOST", "")
    r = client.post("/api/v1/settings/smtp/test", headers=bearer(admin_tok), json={"to": "x@example.com"})
    assert r.json["ok"] is False and "not configured" in r.json["error"]
