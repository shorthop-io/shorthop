"""Browser sign-in (server-side) and the web UI shell.

Login is deliberately server-rendered even though the UI will be a React app: the Google
flow is a confidential-client exchange that must happen here, and the break-glass password
form must work when nothing else does. Everything after sign-in talks to /api/v1 with the
session cookie plus the CSRF token from /api/v1/me.
"""
import logging
from html import escape
from pathlib import Path

from flask import Blueprint, Response, redirect, request, send_from_directory, url_for

from . import config as cfg
from . import google_oidc, ratelimit
from .clientip import client_ip
from .auth import begin_session, find_user_by_email, password_login, session_identity
from .audit import record

logger = logging.getLogger(__name__)

web = Blueprint("web", __name__)
_LOGIN_WINDOW = 900

WEB_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"


def _safe_next(target: str) -> str:
    # OX Agent: Open Redirect prevented — only same-site relative paths are followed
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return "/"


_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Shorthop · sign in</title>
<style>
:root{{--bg:#f5f6f8;--card:#fff;--ink:#14181f;--muted:#5b6472;--accent:#1f6f66;--err:#a3271d;--line:#dfe3e8}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0f1216;--card:#171b21;--ink:#e7eaee;--muted:#9aa3ae;--accent:#4fb3a6;--err:#ff8b80;--line:#2a3039}}}}
body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,sans-serif;display:grid;place-items:center;min-height:100vh}}
main{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:28px;width:min(360px,calc(100vw - 32px));box-sizing:border-box}}
h1{{font-size:1.3rem;margin:0 0 4px}}p{{color:var(--muted);margin:0 0 20px}}
a.btn,button{{display:block;width:100%;box-sizing:border-box;padding:10px;border-radius:8px;font:inherit;font-weight:600;text-align:center;text-decoration:none;cursor:pointer}}
a.btn{{background:var(--accent);color:#fff;border:0}}
button{{background:transparent;color:var(--ink);border:1px solid var(--line)}}
label{{display:block;margin:10px 0 4px;color:var(--muted);font-size:.85rem}}
input{{width:100%;box-sizing:border-box;padding:9px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink);font:inherit}}
details{{margin-top:20px}}summary{{color:var(--muted);cursor:pointer;font-size:.9rem}}
.err{{color:var(--err);margin:0 0 14px}}
</style></head><body><main>
<h1>Shorthop</h1><p>Alerts and on-call.</p>
{error}{google}
<details{open}><summary>Sign in with a password (break-glass)</summary>
<form method="post" action="{action}">
<label for="email">Email</label><input id="email" name="email" type="email" autocomplete="username" required>
<label for="password">Password</label><input id="password" name="password" type="password" autocomplete="current-password" required>
<input type="hidden" name="next" value="{next}">
<button type="submit" style="margin-top:14px">Sign in</button></form></details>
</main></body></html>"""


def _login_page(error: str = "", next_path: str = "/") -> Response:
    google = ""
    if google_oidc.enabled():
        google = (f'<a class="btn" href="{url_for("web.google_login", next=next_path)}">'
                  "Sign in with Google</a>")
    html = _PAGE.format(
        # OX Agent: Cross-Site Scripting prevented — every interpolated value is HTML-escaped
        error=f'<p class="err" role="alert">{escape(error)}</p>' if error else "",
        google=google,
        open=" open" if not google or error else "",
        action=url_for("web.login"),
        next=escape(next_path),
    )
    resp = Response(html, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@web.get("/login")
def login_form():
    return _login_page(next_path=_safe_next(request.args.get("next", "/")))


@web.post("/login")
def login():
    email = request.form.get("email", "")
    next_path = _safe_next(request.form.get("next", "/"))
    key_email = email.strip().lower()
    addr = client_ip()
    # Failures are counted per address AND per email: per address stops one source guessing
    # many accounts, per email stops many sources guessing one. Only FAILURES count, so a
    # person who types their password right is never slowed down. (The cost: somebody can
    # lock an email's break-glass sign-in for 15 minutes — Google sign-in is unaffected.)
    if (ratelimit.count("login_ip", addr, _LOGIN_WINDOW) >= cfg.LOGIN_FAILURES_PER_IP
            or ratelimit.count("login_email", key_email, _LOGIN_WINDOW) >= cfg.LOGIN_FAILURES_PER_EMAIL):
        record(key_email, "login.password", status="error", detail="rate limited")
        return _login_page("Too many failed sign-ins — wait 15 minutes and try again.",
                           next_path), 429
    user = password_login(email, request.form.get("password", ""))
    if user is None:
        ratelimit.hit("login_ip", addr, cfg.LOGIN_FAILURES_PER_IP, _LOGIN_WINDOW)
        ratelimit.hit("login_email", key_email, cfg.LOGIN_FAILURES_PER_EMAIL, _LOGIN_WINDOW)
        record(key_email, "login.password", status="error")
        # One message for unknown user, wrong password and disabled account alike.
        return _login_page("Wrong email or password.", next_path), 401
    record(user.email, "login.password")
    return redirect(next_path)


@web.get("/auth/google")
def google_login():
    if not google_oidc.enabled():
        return redirect(url_for("web.login_form"))
    return redirect(google_oidc.begin(_safe_next(request.args.get("next", "/"))))


@web.get("/auth/google/callback")
def google_callback():
    try:
        email, next_path = google_oidc.complete(request.args)
    except google_oidc.GoogleAuthError as e:
        return _login_page(str(e)), 400
    user = find_user_by_email(email)
    if user is None:
        record(email, "login.google", status="error", detail="no such user")
        return _login_page(f"{email} has no Shorthop account. Ask an admin to invite you."), 403
    begin_session(user)
    record(user.email, "login.google")
    return redirect(_safe_next(next_path))


@web.post("/logout")
def logout():
    from flask import session

    session.clear()
    return redirect(url_for("web.login_form"))


@web.get("/", defaults={"path": ""})
@web.get("/<path:path>")
def spa(path):
    """The React app once built into web/dist; a plain landing page until then."""
    if path.startswith(("api/", "ingest/", "mcp", "metrics", "docs", "openapi", "calendar/")):
        return Response("not found\n", status=404, mimetype="text/plain")
    if session_identity() is None:
        return redirect(url_for("web.login_form", next="/" + path))
    if WEB_DIST.is_dir():
        candidate = WEB_DIST / path
        if path and candidate.is_file():
            # OX Agent: Path Traversal prevented — send_from_directory confines to WEB_DIST
            return send_from_directory(WEB_DIST, path)
        return send_from_directory(WEB_DIST, "index.html")
    return Response(
        "<!doctype html><meta charset=utf-8><title>Shorthop</title>"
        "<p>Signed in. The web UI is not built yet — the API is at "
        "<a href=/docs>/docs</a>.</p>"
        "<form method=post action=/logout><button>Sign out</button></form>",
        mimetype="text/html")
