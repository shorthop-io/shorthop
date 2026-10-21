"""The public listener, client addresses, the public rate limits, password sign-in limits,
webhook destinations and response headers (app/exposure.py, app/clientip.py)."""
import pytest

from app import config as cfg
from app import delivery
from app.auth import hash_password
from app.db import SessionLocal
from tests.conftest import bearer, make_integration, make_token, make_user

PUB = {"SERVER_PORT": "8081"}


@pytest.fixture()
def public(monkeypatch):
    monkeypatch.setattr(cfg, "PUBLIC_PORT", 8081)


def _device(client, user):
    tok = make_token(user, ["alerts:read"])
    code = client.post("/api/v1/devices/pair-codes", headers=bearer(tok)).json["code"]
    r = client.post("/api/v1/devices/pair", environ_overrides=PUB, json={"code": code})
    assert r.status_code == 200
    return r.json["token"]


# --- the public door -------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/", "/login", "/incidents", "/auth/google", "/mcp",
                                  "/metrics", "/docs", "/openapi.json",
                                  "/ingest/prometheus/api/v2/alerts", "/ingest/webhook"])
def test_public_door_answers_only_the_phone_and_calendar_surface(client, public, path):
    assert client.get(path, environ_overrides=PUB).status_code == 404
    assert client.post(path, environ_overrides=PUB).status_code == 404


def test_private_door_is_unchanged(client, public):
    assert client.get("/login").status_code == 200
    assert client.get("/healthz", environ_overrides=PUB).status_code == 200


def test_no_public_door_when_unset(client):
    # PUBLIC_PORT=0: a request on any port is the private door, as before.
    assert client.get("/login", environ_overrides=PUB).status_code == 200


def test_public_door_takes_device_tokens_only(client, public):
    admin = make_user(email="boss@example.com", role="admin")
    personal = make_token(admin, ["admin"])
    assert client.get("/api/v1/alerts", headers=bearer(personal)).status_code == 200
    r = client.get("/api/v1/alerts", headers=bearer(personal), environ_overrides=PUB)
    assert r.status_code == 401 and "device" in r.json["message"]
    _, itok = make_integration("prometheus")
    assert client.get("/api/v1/alerts", headers=bearer(itok), environ_overrides=PUB).status_code == 401

    dev = _device(client, admin)
    assert client.get("/api/v1/alerts", headers=bearer(dev), environ_overrides=PUB).status_code == 200
    # an admin's phone still has operational scopes only
    r = client.post("/api/v1/routes", headers=bearer(dev), environ_overrides=PUB, json={"name": "x"})
    assert r.status_code == 403


def test_public_door_ignores_the_session_cookie(client, public):
    user = make_user()
    with client.session_transaction() as s:
        s["user_id"] = user.id
    assert client.get("/api/v1/me").status_code == 200
    assert client.get("/api/v1/me", environ_overrides=PUB).status_code == 401


def test_device_request_budget(client, public, monkeypatch):
    dev = _device(client, make_user())
    monkeypatch.setattr(cfg, "DEVICE_REQUESTS_PER_MINUTE", 3)
    codes = [client.get("/api/v1/alerts", headers=bearer(dev), environ_overrides=PUB).status_code
             for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    # the budget is the public door's: the same phone on the private network is not counted
    assert client.get("/api/v1/alerts", headers=bearer(dev)).status_code == 200


def test_failed_credentials_are_limited_per_address(client, public, monkeypatch):
    dev = _device(client, make_user())
    monkeypatch.setattr(cfg, "PUBLIC_AUTH_FAILURES_PER_10MIN", 3)
    for _ in range(3):
        r = client.get("/api/v1/alerts", headers=bearer("shd_wrong"), environ_overrides=PUB)
        assert r.status_code == 401
    # once over, the address is refused even with a good token …
    r = client.get("/api/v1/alerts", headers=bearer(dev), environ_overrides=PUB)
    assert r.status_code == 429
    # … but another address is not
    other = {**PUB, "REMOTE_ADDR": "198.51.100.7"}
    assert client.get("/api/v1/alerts", headers=bearer(dev), environ_overrides=other).status_code == 200


def test_pairing_link_and_feed_url_use_the_public_url(client, monkeypatch):
    monkeypatch.setattr(cfg, "PUBLIC_URL", "https://shorthop.internal")
    monkeypatch.setattr(cfg, "PUBLIC_API_URL", "https://oncall.example.com")
    tok = make_token(make_user(), ["alerts:read", "oncall:read"])
    link = client.post("/api/v1/devices/pair-codes", headers=bearer(tok)).json["link"]
    assert "oncall.example.com" in link and "internal" not in link
    feed = client.post("/api/v1/me/calendar-feeds", headers=bearer(tok), json={"kind": "mine"}).json
    assert feed["url"].startswith("https://oncall.example.com/calendar/")


# --- calendar feeds ----------------------------------------------------------------------

def _feed_path(client):
    tok = make_token(make_user(), ["oncall:read"])
    url = client.post("/api/v1/me/calendar-feeds", headers=bearer(tok), json={"kind": "mine"}).json["url"]
    return url[url.index("/calendar/"):]


def test_feed_is_served_on_the_public_door_and_limited_per_feed(client, public, monkeypatch):
    path = _feed_path(client)
    monkeypatch.setattr(cfg, "FEED_FETCHES_PER_HOUR", 2)
    codes = [client.get(path, environ_overrides=PUB).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_unknown_feed_tokens_are_limited_per_address(client, public, monkeypatch):
    path = _feed_path(client)
    monkeypatch.setattr(cfg, "FEED_MISSES_PER_10MIN", 2)
    for _ in range(2):
        assert client.get("/calendar/shc_nope.ics", environ_overrides=PUB).status_code == 404
    assert client.get("/calendar/shc_nope.ics", environ_overrides=PUB).status_code == 429
    assert client.get(path, environ_overrides=PUB).status_code == 429     # same address
    other = {**PUB, "REMOTE_ADDR": "198.51.100.7"}
    assert client.get(path, environ_overrides=other).status_code == 200


# --- client address ----------------------------------------------------------------------

def _addr(app, remote, **headers):
    from app.clientip import client_ip

    with app.test_request_context("/", environ_base={"REMOTE_ADDR": remote}, headers=headers):
        return client_ip()


def test_client_address_reads_only_what_our_proxies_appended(app, monkeypatch):
    # a trusted proxy: the RIGHTMOST untrusted hop, never the caller-written first one
    assert _addr(app, "10.0.0.2", **{"X-Forwarded-For": "6.6.6.6, 203.0.113.9"}) == "203.0.113.9"
    assert _addr(app, "10.0.0.2", **{"X-Forwarded-For": "203.0.113.9, 10.0.0.9"}) == "203.0.113.9"
    # a public peer: headers are ignored entirely
    assert _addr(app, "198.51.100.1", **{"X-Forwarded-For": "6.6.6.6"}) == "198.51.100.1"
    # garbage ends the walk
    assert _addr(app, "10.0.0.2", **{"X-Forwarded-For": "6.6.6.6, nonsense"}) == "10.0.0.2"
    # CLIENT_IP_HEADER, only from a trusted peer
    monkeypatch.setattr(cfg, "CLIENT_IP_HEADER", "CF-Connecting-IP")
    assert _addr(app, "10.0.0.2", **{"CF-Connecting-IP": "203.0.113.5"}) == "203.0.113.5"
    assert _addr(app, "198.51.100.1", **{"CF-Connecting-IP": "6.6.6.6"}) == "198.51.100.1"


def test_rotating_the_forwarded_header_does_not_reset_the_pairing_limit(client, monkeypatch):
    from app.api import devices as d

    monkeypatch.setattr(d, "_PAIR_LIMIT", 2)
    codes = [client.post("/api/v1/devices/pair", json={"code": "AAAA-BBBB"},
                         headers={"X-Forwarded-For": f"6.6.6.{i}, 203.0.113.9"}).status_code
             for i in range(3)]
    assert codes == [400, 400, 429]


# --- break-glass password sign-in --------------------------------------------------------

def _with_password(email="ann@example.com"):
    u = make_user(email=email)
    u.password_hash = hash_password("right-password-123")
    SessionLocal.commit()
    return u


def test_password_failures_are_limited_per_address(client, monkeypatch):
    _with_password()
    monkeypatch.setattr(cfg, "LOGIN_FAILURES_PER_IP", 2)
    for i in range(2):
        r = client.post("/login", data={"email": f"x{i}@example.com", "password": "no"})
        assert r.status_code == 401
    r = client.post("/login", data={"email": "ann@example.com", "password": "right-password-123"})
    assert r.status_code == 429


def test_password_failures_are_limited_per_email_across_addresses(client, monkeypatch):
    _with_password()
    monkeypatch.setattr(cfg, "LOGIN_FAILURES_PER_EMAIL", 2)
    for i in range(2):
        r = client.post("/login", data={"email": "ann@example.com", "password": "no"},
                        environ_base={"REMOTE_ADDR": f"198.51.100.{i}"})
        assert r.status_code == 401
    r = client.post("/login", data={"email": "ann@example.com", "password": "right-password-123"},
                    environ_base={"REMOTE_ADDR": "198.51.100.99"})
    assert r.status_code == 429


def test_a_correct_password_is_not_counted(client, monkeypatch):
    _with_password()
    monkeypatch.setattr(cfg, "LOGIN_FAILURES_PER_EMAIL", 1)
    for _ in range(3):
        r = client.post("/login", data={"email": "ann@example.com", "password": "right-password-123"})
        assert r.status_code == 302


# --- webhook destinations ----------------------------------------------------------------

@pytest.fixture()
def admin_tok():
    return make_token(make_user(email="boss@example.com", role="admin"), ["admin"])


def _channel(client, tok, url):
    return client.post("/api/v1/channels", headers=bearer(tok),
                       json={"name": "w", "kind": "webhook", "config": {"url": url}})


@pytest.mark.parametrize("addr", ["10.0.0.5", "127.0.0.1", "169.254.169.254", "192.168.1.20",
                                  "100.64.0.1", "::1", "fd00::1", "::ffff:10.0.0.5"])
def test_webhook_to_an_internal_address_is_refused(client, admin_tok, monkeypatch, addr):
    monkeypatch.setattr(delivery, "_resolve", lambda host, port: [addr])
    r = _channel(client, admin_tok, "https://hooks.example.com/x")
    assert r.status_code == 422 and "private" in r.json["message"]


def test_webhook_refused_if_any_address_is_internal(client, admin_tok, monkeypatch):
    monkeypatch.setattr(delivery, "_resolve", lambda host, port: ["93.184.216.34", "10.0.0.5"])
    assert _channel(client, admin_tok, "https://hooks.example.com/x").status_code == 422


def test_webhook_that_does_not_resolve_is_refused(client, admin_tok, monkeypatch):
    def boom(host, port):
        raise OSError("nxdomain")
    monkeypatch.setattr(delivery, "_resolve", boom)
    r = _channel(client, admin_tok, "https://nowhere.example.com/x")
    assert r.status_code == 422 and "resolve" in r.json["message"]


def test_internal_webhook_host_can_be_allowed_in_settings(client, admin_tok, monkeypatch):
    monkeypatch.setattr(delivery, "_resolve", lambda host, port: ["10.0.0.5"])
    assert _channel(client, admin_tok, "https://chat.internal/x").status_code == 422
    r = client.put("/api/v1/settings", headers=bearer(admin_tok),
                   json={"webhook_private_hosts": ["Chat.Internal"]})
    assert r.status_code == 200
    assert _channel(client, admin_tok, "https://chat.internal/x").status_code == 201


def test_send_rechecks_the_destination(monkeypatch):
    # saved while public, DNS now points inside: the send refuses, nothing is requested
    called = []
    monkeypatch.setattr(delivery.requests, "request", lambda *a, **k: called.append(1))
    monkeypatch.setattr(delivery, "_resolve", lambda host, port: ["10.0.0.5"])
    with pytest.raises(delivery.DeliveryError, match="private"):
        delivery.send_webhook({"url": "https://hooks.example.com/x"}, {"event": "test"}, None)
    assert called == []


# --- headers -----------------------------------------------------------------------------

def test_security_headers(client, monkeypatch):
    r = client.get("/login")
    csp = r.headers["Content-Security-Policy"]
    assert "script-src 'self';" in csp and "frame-ancestors 'none'" in csp
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert "Strict-Transport-Security" not in r.headers           # SESSION_COOKIE_SECURE off
    docs = client.get("/docs").headers["Content-Security-Policy"]
    assert "https://cdn.jsdelivr.net" in docs and "cdn.jsdelivr.net" not in csp
    monkeypatch.setattr(cfg, "SESSION_COOKIE_SECURE", True)
    hsts = client.get("/healthz").headers["Strict-Transport-Security"]
    assert "max-age" in hsts and "includeSubDomains" not in hsts
