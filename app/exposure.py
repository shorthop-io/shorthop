"""What Shorthop shows to the internet, and how every response protects itself.

## Two doors, one process

Shorthop can listen on two ports. `PORT` is the whole product — the web UI, sign-in, the
ingest doors, MCP, /metrics, /docs — and belongs on the private network (LAN, VPN, the
cluster). `PUBLIC_PORT` is the only one meant to face the internet, and answers exactly what
a phone and a calendar app need:

| Path | Credential |
|---|---|
| `/api/v1/*` | a **device** token (`shd_…`) — the phone app |
| `/api/v1/devices/pair` | none (the one-time code is the credential; rate-limited) |
| `/calendar/<token>.ics` | the feed token in the path |
| `/healthz` | none |

Everything else is a 404 there, cookies are removed before Flask sees them (no web session
can be ridden through it), and a personal (`sh_…`) or integration (`shi_…`) token is refused
— so a leaked admin token cannot be used from outside, and a device token only ever carries
its owner's OPERATIONAL scopes (app/auth.py).

The door is decided by the LISTENING SOCKET (waitress sets SERVER_PORT per listener), never
by a header, so no proxy configuration can widen it. That is what makes it generic: put any
reverse proxy, tunnel or load balancer in front of PUBLIC_PORT and nothing else.

## Rate limits on the public door (database-backed: one budget across replicas)

- per device token: `DEVICE_REQUESTS_PER_MINUTE`
- per client address: `PUBLIC_AUTH_FAILURES_PER_10MIN` failed credentials (401), counted on
  failure only — generous on purpose: phones sit behind carrier NAT, so one address is many
  people, and a revoked phone retrying must not lock its neighbours out
- pairing: per client address (app/api/devices.py)
- calendar feeds: per feed, and unknown feed tokens per client address (app/api/calendar.py)

## Headers

Every response gets nosniff, a referrer policy, frame denial and a Content-Security-Policy.
The web UI is a bundled SPA with no inline script and no third-party origin, so its CSP is
strict; /docs (Swagger UI) needs an inline script and the pinned jsDelivr origin, and gets
exactly that. HSTS is sent on HTTPS deployments, WITHOUT includeSubDomains — Shorthop does
not own the rest of the domain it is installed on.
"""
from flask import Response, jsonify, request

from . import config as cfg
from . import ratelimit
from .clientip import client_ip

PUBLIC_ENV = "shorthop.public"
DEVICE_TOKEN_PREFIX = "shd_"
_PUBLIC_PREFIXES = ("/api/v1/", "/calendar/")
_PUBLIC_EXACT = ("/healthz",)
_AUTH_FAIL_WINDOW = 600

_CSP_APP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; "
            "object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
_CSP_DOCS = ("default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
             "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
             "img-src 'self' data: https://cdn.jsdelivr.net; connect-src 'self'; "
             "object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")


class DoorMiddleware:
    """WSGI: mark requests that arrived on PUBLIC_PORT and strip their cookies."""

    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        if cfg.PUBLIC_PORT and environ.get("SERVER_PORT") == str(cfg.PUBLIC_PORT):
            environ[PUBLIC_ENV] = True
            environ.pop("HTTP_COOKIE", None)
        return self.wsgi_app(environ, start_response)


def is_public() -> bool:
    return bool(request.environ.get(PUBLIC_ENV))


def public_path_allowed(path: str) -> bool:
    return path in _PUBLIC_EXACT or path.startswith(_PUBLIC_PREFIXES)


def _bearer() -> str:
    h = request.headers.get("Authorization", "")
    return h[7:].strip() if h[:7].lower() == "bearer " else ""


def _gate():
    if not is_public():
        return None
    if not public_path_allowed(request.path):
        return Response("not found\n", status=404, mimetype="text/plain")
    if request.path.startswith("/api/v1/"):
        if ratelimit.count("public_auth", client_ip(), _AUTH_FAIL_WINDOW) \
                >= cfg.PUBLIC_AUTH_FAILURES_PER_10MIN:
            return jsonify({"code": 429, "message": "too many failed requests — try later"}), 429
        tok = _bearer()
        if tok and not tok.startswith(DEVICE_TOKEN_PREFIX):
            # Counted by _after like any other 401.
            return jsonify({"code": 401,
                            "message": "only phone (device) tokens are accepted here"}), 401
    return None


def _after(resp):
    if is_public() and resp.status_code == 401 and request.path.startswith("/api/v1/"):
        ratelimit.hit("public_auth", client_ip(), cfg.PUBLIC_AUTH_FAILURES_PER_10MIN,
                      _AUTH_FAIL_WINDOW)
    h = resp.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("Referrer-Policy", "same-origin")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    docs = request.path.startswith(cfg.OPENAPI_SWAGGER_UI_PATH)
    h.setdefault("Content-Security-Policy", _CSP_DOCS if docs else _CSP_APP)
    if cfg.SESSION_COOKIE_SECURE:
        h.setdefault("Strict-Transport-Security", "max-age=31536000")
    return resp


def public_base_url() -> str:
    """Where phones and calendar apps reach this server: the public listener's URL."""
    return cfg.PUBLIC_API_URL or cfg.PUBLIC_URL or request.host_url.rstrip("/")


def init_app(app) -> None:
    app.wsgi_app = DoorMiddleware(app.wsgi_app)
    app.before_request(_gate)
    app.after_request(_after)
