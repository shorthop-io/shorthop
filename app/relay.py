"""Client for the Shorthop push relay (relay.shorthop.io; repo backend/shorthop-relay).

A self-hosted Shorthop cannot push to the published app on its own: the FCM/APNs keys belong
to whoever publishes the app. So when a relay is configured (Settings → Push relay, or the
RELAY_URL / RELAY_KEY environment), phones are registered with it and pages go through it.
With no relay configured, pages go straight to Expo with this install's own credentials —
how our own lab runs, and how anyone publishing their own build of the app runs.

What the relay receives, and nothing more: the phone's Expo push token, its platform and name
(shown on the bill), and per page an opaque incident id + the event. **Never alert content.**

⚠️ The key is a credential: never logged, never returned by the settings API.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass
from urllib.parse import urlparse

import requests

from . import settings

logger = logging.getLogger(__name__)

TIMEOUT = 10
KEY_RE = re.compile(r"^shr_[0-9a-f-]{36}\.[A-Za-z0-9_-]{20,128}$")
# The relay's event vocabulary. Anything else Shorthop pages about (e.g. a reassignment) is
# "an incident needs your attention" to the phone.
RELAY_EVENTS = ("triggered", "escalated", "resolved", "test")


class RelayError(Exception):
    """Caller-safe reason (no key, no URL secrets)."""


@dataclass
class Registration:
    status: str                 # registered / refused / error
    device_id: str | None = None
    message: str | None = None


def valid_url(url: str) -> bool:
    """https://host[:port] with no path, query or credentials. The relay is an outside service
    on the internet, so plain http would send the key in clear."""
    try:
        p = urlparse(url)
    except ValueError:
        return False
    return (p.scheme == "https" and bool(p.hostname) and not p.username and not p.password
            and p.path in ("", "/") and not p.query and not p.fragment)


def config() -> tuple[str, str] | None:
    """(base url, key), or None when no relay is configured."""
    url = (settings.get("relay_url") or "").strip().rstrip("/")
    key = (settings.get("relay_key") or "").strip()
    if not url or not key:
        return None
    return url, key


def configured() -> bool:
    return config() is not None


def _call(method: str, path: str, json: dict | None = None) -> requests.Response:
    conf = config()
    if conf is None:
        raise RelayError("no push relay is configured")
    url, key = conf
    try:
        return requests.request(method, f"{url}{path}", json=json, timeout=TIMEOUT,
                                allow_redirects=False,
                                headers={"Authorization": f"Bearer {key}",
                                         "Accept": "application/json"})
    except requests.RequestException as e:
        raise RelayError(f"could not reach the push relay ({type(e).__name__})") from None


def _message(resp: requests.Response) -> str:
    try:
        body = resp.json() or {}
    except ValueError:
        body = {}
    return str(body.get("message") or f"the push relay answered HTTP {resp.status_code}")[:500]


def _errors(resp: requests.Response) -> dict:
    try:
        return (resp.json() or {}).get("errors") or {}
    except ValueError:
        return {}


def _auth_problem(resp: requests.Response) -> str | None:
    if resp.status_code == 401:
        return "the push relay rejected this install's key — check Settings → Push relay"
    if resp.status_code == 403:
        return _message(resp)   # a disabled installation says why
    return None


def register(push_token: str, platform: str = "", label: str = "") -> Registration:
    """Sign a phone in at the relay. Never raises: a failure is a status to show."""
    body = {"push_token": push_token, "label": (label or "")[:128] or None}
    if platform in ("ios", "android"):
        body["platform"] = platform
    try:
        resp = _call("POST", "/v1/devices", body)
    except RelayError as e:
        return Registration("error", message=str(e))
    if resp.status_code in (200, 201):
        return Registration("registered", device_id=(resp.json() or {}).get("id"))
    if resp.status_code == 402:
        return Registration("refused", message=_message(resp))
    return Registration("error", message=_auth_problem(resp) or _message(resp))


def installation() -> dict:
    """What the relay says about this install (SHOR-14): seats used, free, card, and the
    customer account page to manage them. Never raises — `ok: False` + `error` instead.
    The account URL becomes a link in the admin UI, so only an https:// URL is passed on."""
    if not configured():
        return {"configured": False, "ok": False, "error": None}
    try:
        resp = _call("GET", "/v1/installation")
    except RelayError as e:
        return {"configured": True, "ok": False, "error": str(e)}
    if resp.status_code != 200:
        return {"configured": True, "ok": False, "error": _auth_problem(resp) or _message(resp)}
    try:
        body = resp.json() or {}
    except ValueError:
        return {"configured": True, "ok": False, "error": "the push relay did not answer JSON"}
    url = str(body.get("account_url") or "")
    return {"configured": True, "ok": True, "error": None,
            "name": str(body.get("name") or "")[:128],
            "signed_in": int(body.get("signed_in") or 0), "free": int(body.get("free") or 0),
            "card_on_file": bool(body.get("card_on_file")),
            "can_add_device": bool(body.get("can_add_device", True)),
            "account_url": url if url.startswith("https://") and len(url) <= 300 else None}


def unregister(relay_device_id: str) -> None:
    """Sign a phone out at the relay, freeing its seat. Best effort: a failure is logged, and
    the seat frees itself later (the relay signs out phones it stops hearing from)."""
    try:
        resp = _call("DELETE", f"/v1/devices/{relay_device_id}")
        if resp.status_code not in (204, 404):
            logger.warning("push relay sign-out answered HTTP %s", resp.status_code)
    except RelayError as e:
        logger.warning("push relay sign-out failed: %s", e)


@dataclass
class PushResult:
    outcome: str                # ok / rejected / unregistered / error / signed_out
    error: str | None = None
    reason: str | None = None   # for signed_out: why the relay signed the phone out


def push(relay_device_id: str, incident_id: str, event: str) -> PushResult:
    """Page one phone. Raises RelayError for anything worth retrying later."""
    ev = event if event in RELAY_EVENTS else "triggered"
    resp = _call("POST", "/v1/push", {"device_id": relay_device_id, "incident_id": incident_id,
                                       "event": ev})
    if resp.status_code == 200:
        body = resp.json() or {}
        return PushResult(body.get("outcome") or "error", body.get("error"))
    if resp.status_code == 410:
        return PushResult("signed_out", reason=_errors(resp).get("reason"))
    if resp.status_code == 404:
        return PushResult("signed_out", reason="unknown to the relay")
    raise RelayError(_auth_problem(resp) or _message(resp))


# --- phones ------------------------------------------------------------------------------
# The three helpers below are the ONLY code that writes a Device's relay_* columns, so the
# devices API, the settings API and delivery cannot disagree about what a status means.

def forget(dev) -> None:
    dev.relay_device_id = dev.relay_status = dev.relay_message = None
    dev.relay_signs_out_at = dev.relay_stale_notified_at = None


def sync_device(dev, force: bool = False) -> None:
    """Register `dev` with the relay if it has a push token and is not registered yet.
    Records the outcome on the row; the caller commits. A no-op with no relay configured.

    ⚠️ A phone the relay signed out for being UNUSED (`signed_out`) is not signed in again by a
    page — that would put a phone in a drawer straight back on the bill. Only the phone itself
    (the app sending its push token: `force=True`) signs it back in."""
    if not configured() or dev.revoked or not dev.push_token:
        return
    if dev.relay_status == "registered" and dev.relay_device_id:
        return
    if dev.relay_status == "signed_out" and not force:
        return
    reg = register(dev.push_token, dev.platform or "", dev.name or "")
    forget(dev)
    dev.relay_status, dev.relay_device_id, dev.relay_message = reg.status, reg.device_id, reg.message


def sign_out_device(dev) -> None:
    """Free `dev`'s seat at the relay and forget the registration. The caller commits."""
    if dev.relay_device_id and configured():
        unregister(dev.relay_device_id)
    forget(dev)


def mark_signed_out_unused(dev, reason: str | None) -> None:
    """The relay signed this phone out (60 days unused, or the app was removed)."""
    forget(dev)
    if reason == "unregistered":
        dev.push_token = None     # the app is gone; nothing to sign in again
        return
    dev.relay_status = "signed_out"
    dev.relay_message = ("signed out of the push relay after going unused — open the Shorthop "
                         "app on this phone to sign it in again")


def resync_all(db) -> dict:
    """After the relay URL or key changes: every registration belongs to the OLD relay or
    installation, so forget them all and register every phone that has a push token again,
    NOW — a refusal (over the free phones, no card) then shows on the Phones page the moment
    the setting is saved, instead of surfacing as a failed page at 3 a.m."""
    from .models import Device

    counts = {"registered": 0, "refused": 0, "error": 0}
    for dev in db.query(Device).filter(Device.revoked.is_(False)).order_by(Device.created_at):
        forget(dev)
        if configured() and dev.push_token:
            sync_device(dev)
            counts[dev.relay_status] = counts.get(dev.relay_status, 0) + 1
    return counts


# --- SHOR-10: unused phones ---------------------------------------------------------------
# The relay keeps the clock (30 days unseen → warning push, 60 → sign-out) because it holds
# the phone; this server knows the PEOPLE, so it reports check-ins and does the emailing.

CHECKIN_WINDOW = dt.timedelta(days=7)


def _parse(ts: str | None) -> dt.datetime | None:
    if not ts:
        return None
    t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def report_seen(db) -> int:
    """Tell the relay when the app last talked to us from each registered phone. Every phone
    seen in the last week is reported each time — idempotent (the relay only moves its clock
    forward), and it covers ticks this server missed."""
    from .models import Device, aware, now

    since = now() - CHECKIN_WINDOW
    items = [{"id": d.relay_device_id, "seen_at": aware(d.last_seen_at).isoformat()}
             for d in db.query(Device).filter(Device.revoked.is_(False),
                                             Device.relay_device_id.is_not(None),
                                             Device.last_seen_at.is_not(None)).all()
             if aware(d.last_seen_at) >= since]
    if not items:
        return 0
    resp = _call("POST", "/v1/devices/seen", {"devices": items[:1000]})
    if resp.status_code != 200:
        raise RelayError(_auth_problem(resp) or _message(resp))
    return int((resp.json() or {}).get("updated") or 0)


def list_signed_in() -> dict[str, dict]:
    resp = _call("GET", "/v1/devices")
    if resp.status_code != 200:
        raise RelayError(_auth_problem(resp) or _message(resp))
    return {d["id"]: d for d in (resp.json() or {}).get("devices") or []}


def housekeeping(db, notify) -> dict:
    """Report check-ins, then reconcile with the relay's view of our phones.

    `notify(dev, kind, when)` emails the phone's owner and the admins; kind is "warning"
    (will be signed out at `when`) or "signed_out". Each happens once per event.
    Never raises: an unreachable relay is logged and tried again next time."""
    from .models import Device, now

    out = {"reported": 0, "warned": 0, "signed_out": 0}
    if not configured():
        return out
    try:
        out["reported"] = report_seen(db)
        remote = list_signed_in()
    except RelayError as e:
        logger.warning("push relay housekeeping skipped: %s", e)
        return out
    for dev in db.query(Device).filter(Device.revoked.is_(False),
                                       Device.relay_status == "registered").all():
        r = remote.get(dev.relay_device_id or "")
        if r is None:
            # Signed out at the relay. Its own sign-outs are stale ones (an uninstalled app
            # surfaces at page time instead), and they are said once.
            mark_signed_out_unused(dev, "stale")
            notify(dev, "signed_out", None)
            out["signed_out"] += 1
        elif r.get("stale_warned_at"):
            dev.relay_signs_out_at = _parse(r.get("signs_out_at"))
            if dev.relay_stale_notified_at is None:
                notify(dev, "warning", dev.relay_signs_out_at)
                dev.relay_stale_notified_at = now()
                out["warned"] += 1
        else:
            dev.relay_signs_out_at = dev.relay_stale_notified_at = None
        db.commit()
    return out
