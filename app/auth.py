"""Authentication and authorisation.

Three kinds of caller, three credentials:

* **people in a browser** — a session cookie, from Google SSO or the break-glass password;
* **people's tools** (scripts, the MCP client, later the native app) — a personal access
  token `sh_…`, which acts AS its user, narrowed to the scopes it was minted with;
* **senders** (Prometheus, Alertmanager, webhooks) — an integration token `shi_…`, which can
  do exactly one thing: deliver alerts to its own integration. See `integration_from_request`.

Tokens are shown once; only their sha256 is stored.

⚠️ A token's effective scopes are re-intersected with its user's role on EVERY request, so
demoting or disabling a person narrows or kills their tokens immediately — a token minted
while someone was admin does not stay admin.
"""
import functools
import hashlib
import hmac
import logging
import secrets as _secrets

import bcrypt
from flask import abort, g, jsonify, request, session
from sqlalchemy import func

from . import config as cfg
from . import exposure, ratelimit
from .db import SessionLocal
from .models import ApiToken, Device, Integration, User, aware, now

logger = logging.getLogger(__name__)

TOKEN_PREFIX = "sh_"
INTEGRATION_TOKEN_PREFIX = "shi_"
DEVICE_TOKEN_PREFIX = "shd_"
# A phone answers pages; it never reconfigures Shorthop or manages people, whatever its owner's role.
DEVICE_SCOPES = ["alerts:read", "alerts:write", "oncall:read", "oncall:write"]

SCOPES = {
    "alerts:read": "Read alerts, occurrences, history and silences",
    "alerts:write": "Acknowledge, resolve and annotate alerts; create and expire silences",
    "oncall:read": "Read teams, schedules and who is on call",
    "oncall:write": "Change schedules (overrides, taking a shift)",
    "config:write": "Integrations, routes, escalation policies, teams",
    "admin": "Users, tokens of other users, audit log — and everything above",
}

ROLES: dict[str, list[str]] = {
    "viewer": ["alerts:read", "oncall:read"],
    "responder": ["alerts:read", "alerts:write", "oncall:read", "oncall:write"],
    "admin": ["admin"],
}


def role_scopes(role: str) -> list[str]:
    return list(ROLES.get(role, []))


def scopes_allow(granted: list[str], needed: str) -> bool:
    return "admin" in granted or needed in granted


def validate_scopes(requested: list[str] | None) -> tuple[list[str], str]:
    """(clean, error). Unknown scopes are refused rather than stored and silently ignored."""
    clean, unknown = [], []
    for raw in requested or []:
        s = (raw or "").strip()
        if not s:
            continue
        if s not in SCOPES:
            unknown.append(s)
        elif s not in clean:
            clean.append(s)
    if unknown:
        return [], f"unknown scope(s): {', '.join(sorted(unknown))}. Valid: {', '.join(SCOPES)}"
    if not clean:
        return [], "at least one scope is required"
    return clean, ""


class Identity:
    def __init__(self, user: User, scopes: list[str], via: str, token_id: str | None = None,
                 device_id: str | None = None):
        self.user = user
        self.user_id = user.id
        self.name = user.email
        self.scopes = scopes
        self.via = via  # session | token | device
        self.token_id = token_id
        self.device_id = device_id

    def can(self, scope: str) -> bool:
        return scopes_allow(self.scopes, scope)


def hash_token(plaintext: str) -> str:
    # OX Agent: Sensitive Data Exposure prevented — only a SHA-256 of the opaque token is stored
    return hashlib.sha256(plaintext.encode()).hexdigest()


def new_token(prefix: str) -> tuple[str, str, str]:
    """(plaintext, hash, display prefix)."""
    plaintext = prefix + _secrets.token_urlsafe(32)
    return plaintext, hash_token(plaintext), plaintext[: len(prefix) + 6]


def _bearer() -> str:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("X-Api-Key", "").strip()


def _effective_token_scopes(token_scopes: list[str], user: User) -> list[str]:
    user_scopes = role_scopes(user.role)
    if "admin" in user_scopes:
        return token_scopes
    return [s for s in token_scopes if s in user_scopes]


def device_identity(plaintext: str) -> Identity | None:
    db = SessionLocal
    row = db.query(Device).filter(Device.token_hash == hash_token(plaintext),
                                  Device.revoked.is_(False)).first()
    if row is None or row.user is None or row.user.disabled:
        return None
    # A phone on the internet gets a request budget of its own: a stolen device token, or a
    # buggy app in a retry loop, is bounded without touching anyone else's phone.
    if exposure.is_public() and ratelimit.hit("device", row.id, cfg.DEVICE_REQUESTS_PER_MINUTE, 60):
        abort(429)
    row.last_seen_at = now()
    db.commit()
    role = role_scopes(row.user.role)
    # OX Agent: Privilege Escalation prevented — a phone gets its owner's operational scopes only
    scopes = [s for s in DEVICE_SCOPES if "admin" in role or s in role]
    return Identity(row.user, scopes, "device", device_id=row.id)


def token_identity() -> Identity | None:
    plaintext = _bearer()
    if plaintext.startswith(DEVICE_TOKEN_PREFIX):
        return device_identity(plaintext)
    if not plaintext.startswith(TOKEN_PREFIX) or exposure.is_public():
        # On the public listener only phones authenticate (app/exposure.py): a personal token
        # can carry admin, and must not work from the internet even if it leaks.
        return None
    db = SessionLocal
    row = db.query(ApiToken).filter(
        ApiToken.token_hash == hash_token(plaintext), ApiToken.revoked.is_(False)
    ).first()
    if row is None:
        return None
    if row.expires_at is not None and aware(row.expires_at) <= now():
        return None
    user = row.user
    if user is None or user.disabled:
        return None
    row.last_used_at = now()
    db.commit()
    return Identity(user, _effective_token_scopes(row.scope_list(), user), "token", row.id)


def session_identity() -> Identity | None:
    uid = session.get("user_id")
    if not uid or exposure.is_public():
        return None
    # Re-read the user every request so a disable or demotion takes effect at once.
    user = SessionLocal.query(User).filter(User.id == uid, User.disabled.is_(False)).first()
    if user is None:
        session.clear()
        return None
    return Identity(user, role_scopes(user.role), "session")


def csrf_token() -> str:
    tok = session.get("csrf")
    if not tok:
        tok = _secrets.token_urlsafe(24)
        session["csrf"] = tok
    return tok


def _csrf_ok() -> bool:
    expected = session.get("csrf", "")
    supplied = request.headers.get("X-CSRF-Token", "")
    # OX Agent: CSRF prevented — a cookie-authenticated write must echo the session's token
    return bool(expected) and hmac.compare_digest(expected.encode(), supplied.encode())


def current_identity() -> Identity | None:
    """A bearer token wins over a cookie; a cookie-authenticated WRITE needs the CSRF header."""
    if _bearer():
        return token_identity()
    ident = session_identity()
    if ident and request.method not in ("GET", "HEAD", "OPTIONS") and not _csrf_ok():
        return None
    return ident


def require(scope: str):
    """API decorator: 401 without a credential, 403 without the scope."""

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            ident = current_identity()
            if ident is None:
                return jsonify({"code": 401, "message": "missing or invalid credentials"}), 401
            # OX Agent: Broken Access Control prevented — scope checked on every API request
            if not ident.can(scope):
                return jsonify({"code": 403, "message": f"requires the '{scope}' scope"}), 403
            g.identity = ident
            return fn(*args, **kwargs)

        return wrapper

    return deco


def integration_from_request(kind: str) -> Integration | None:
    """The integration a sender's token belongs to — and only if it is of the kind this
    door serves. A webhook token cannot post to the Prometheus door, so a leaked one is
    bounded to the shape of payload its owner expected."""
    plaintext = _bearer()
    if not plaintext.startswith(INTEGRATION_TOKEN_PREFIX):
        return None
    row = SessionLocal.query(Integration).filter(
        Integration.token_hash == hash_token(plaintext), Integration.enabled.is_(True)
    ).first()
    if row is None or row.kind != kind:
        return None
    return row


# --- people: passwords and sessions -------------------------------------------------

def hash_password(plaintext: str) -> str:
    # OX Agent: Sensitive Data Exposure prevented by bcrypt password hashing
    return bcrypt.hashpw(plaintext.encode(), bcrypt.gensalt()).decode()


def verify_password(plaintext: str, hashed: str | None) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(plaintext.encode(), hashed.encode())
    except (ValueError, TypeError):
        return False


def find_user_by_email(email: str) -> User | None:
    email = (email or "").strip().lower()
    if not email:
        return None
    return SessionLocal.query(User).filter(
        func.lower(User.email) == email, User.disabled.is_(False)
    ).first()


def begin_session(user: User) -> None:
    user.last_login_at = now()
    SessionLocal.commit()
    # OX Agent: Session Fixation prevented — the pre-login session is discarded
    session.clear()
    session["user_id"] = user.id
    session.permanent = True
    csrf_token()


def password_login(email: str, password: str) -> User | None:
    user = find_user_by_email(email)
    if user is None or not verify_password(password, user.password_hash):
        return None
    begin_session(user)
    return user
