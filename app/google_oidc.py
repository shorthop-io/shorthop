"""Google SSO for Shorthop — OIDC straight to Google, no broker. Copied from dbdeck.

A **confidential** client: the authorization code is exchanged server-side with the client
secret, the ID token is verified once to learn who the caller is and thrown away, and only
Shorthop's own session cookie lives in the browser.

* **No auto-provisioning.** The output is a verified email; `auth.find_user_by_email` must
  already know it. Google answers *who*, the users row answers *what they may do*.
* **The break-glass password stays.** Shorthop is what pages people during an outage; if
  Google, the consent screen or a rotated secret breaks, an SSO-only paging tool locks
  everybody out at exactly the wrong moment.
"""
import base64
import hashlib
import hmac
import logging
import secrets as _secrets
from urllib.parse import urlencode

import requests
from flask import session, url_for

from . import config as cfg

logger = logging.getLogger(__name__)

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"

# Minimum that answers "who is this". `profile` is not needed: the user row carries the name.
SCOPES = "openid email"

TOKEN_TIMEOUT = 10

# Session keys holding the in-flight flow's one-time values.
_STATE = "g_oauth_state"
_NONCE = "g_oauth_nonce"
_VERIFIER = "g_oauth_verifier"
_NEXT = "g_oauth_next"


class GoogleAuthError(Exception):
    """A caller-safe failure: the message is rendered on the login page verbatim, so it must
    never carry protocol detail. The diagnosis goes to the log instead."""


def enabled() -> bool:
    """Both halves must be present — an id with no secret cannot complete the exchange, and
    a half-configured button that always fails is worse than no button."""
    return bool(cfg.GOOGLE_CLIENT_ID and cfg.GOOGLE_CLIENT_SECRET)


def redirect_uri() -> str:
    """The URI Google sends the browser back to. Must match a registered one exactly."""
    return cfg.GOOGLE_REDIRECT_URL or url_for("web.google_callback", _external=True)


def begin(next_path: str = "") -> str:
    """Start the flow: stash the one-time values in the session, return the Google URL.

    `state` binds the callback to this browser session (CSRF), `nonce` binds the ID token to
    this request (replay), and PKCE binds the code to this process. PKCE is not strictly
    required for a confidential client, but it is ~6 lines and closes code interception even
    if the client secret ever leaks."""
    verifier = _secrets.token_urlsafe(64)
    # S256 per RFC 7636 — a protocol transform, not password hashing.
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")

    state = _secrets.token_urlsafe(32)
    nonce = _secrets.token_urlsafe(32)
    session[_STATE] = state
    session[_NONCE] = nonce
    session[_VERIFIER] = verifier
    session[_NEXT] = next_path or ""

    return AUTH_ENDPOINT + "?" + urlencode({
        "client_id": cfg.GOOGLE_CLIENT_ID,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        # Always ask which account. An admin console is precisely where silently reusing
        # whichever Google session the browser happens to hold is the wrong default.
        "prompt": "select_account",
        # No refresh token wanted: the ID token is used once and discarded, and Shorthop never
        # calls a Google API on the user's behalf.
        "access_type": "online",
    })


def _take(key: str) -> str:
    """Pop a one-time session value — consumed whether or not the flow then succeeds, so a
    replayed callback cannot reuse it."""
    return str(session.pop(key, "") or "")


def complete(args) -> tuple[str, str]:
    """Finish the flow from the callback's query args.

    Returns `(verified_lowercased_email, next_path)`; raises `GoogleAuthError` with a
    user-facing message on every failure path."""
    state = _take(_STATE)
    nonce = _take(_NONCE)
    verifier = _take(_VERIFIER)
    next_path = _take(_NEXT)

    if args.get("error"):
        # access_denied is the ordinary "user clicked cancel", not an incident.
        logger.info("google sign-in returned error=%s", args.get("error"))
        raise GoogleAuthError("Google sign-in was cancelled.")

    got_state = str(args.get("state", "") or "")
    if not state or not got_state or not hmac.compare_digest(state, got_state):
        logger.warning("google callback state mismatch (session=%s, query=%s)",
                       bool(state), bool(got_state))
        raise GoogleAuthError(
            "Sign-in could not be verified (the request did not match this browser "
            "session). Please try again."
        )

    code = str(args.get("code", "") or "")
    if not code:
        raise GoogleAuthError("Google did not return an authorization code.")

    claims = _verify_id_token(_exchange(code, verifier))

    # Nonce is checked here, not by the library: verify_oauth2_token covers signature, iss,
    # aud and exp but knows nothing about our per-request nonce.
    if not nonce or not hmac.compare_digest(str(claims.get("nonce", "") or ""), nonce):
        logger.warning("google id_token nonce mismatch")
        raise GoogleAuthError("Sign-in could not be verified. Please try again.")

    if claims.get("email_verified") is not True:
        # An unverified address proves nothing about who controls it.
        raise GoogleAuthError("That Google account's email address is not verified.")

    email = str(claims.get("email", "") or "").strip().lower()
    if "@" not in email:
        raise GoogleAuthError("Google did not return an email address.")

    if cfg.GOOGLE_ALLOWED_DOMAINS:
        if email.rsplit("@", 1)[-1] not in cfg.GOOGLE_ALLOWED_DOMAINS:
            logger.warning("google sign-in refused for out-of-domain address")
            raise GoogleAuthError("That email domain may not sign in to Shorthop.")

    return email, next_path


def _exchange(code: str, verifier: str) -> str:
    """Swap the authorization code for an ID token. Returns the raw JWT."""
    try:
        resp = requests.post(
            TOKEN_ENDPOINT,
            data={
                "code": code,
                "client_id": cfg.GOOGLE_CLIENT_ID,
                "client_secret": cfg.GOOGLE_CLIENT_SECRET,
                "redirect_uri": redirect_uri(),
                "grant_type": "authorization_code",
                "code_verifier": verifier,
            },
            timeout=TOKEN_TIMEOUT,
        )
    except requests.RequestException:
        logger.exception("google token exchange: transport failure")
        raise GoogleAuthError("Could not reach Google to complete sign-in.") from None

    if resp.status_code != 200:
        # The body names the real cause (redirect_uri_mismatch, invalid_client, …) and is
        # the single most useful line when wiring this up — log it, never show it.
        logger.warning("google token exchange failed: HTTP %s %s",
                       resp.status_code, resp.text[:500])
        raise GoogleAuthError("Google rejected the sign-in attempt.")

    raw = (resp.json() or {}).get("id_token", "")
    if not raw:
        logger.warning("google token response carried no id_token")
        raise GoogleAuthError("Google's response was incomplete.")
    return raw


def _verify_id_token(raw: str) -> dict:
    """Verify signature, issuer, audience and expiry via google-auth (lazy-imported, like
    the other google clients in this service)."""
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token as google_id_token

    try:
        return google_id_token.verify_oauth2_token(
            raw, google_requests.Request(), audience=cfg.GOOGLE_CLIENT_ID,
        )
    except Exception:  # noqa: BLE001 — any verification failure is one answer: refuse.
        logger.exception("google id_token verification failed")
        raise GoogleAuthError("Could not verify Google's response.") from None
