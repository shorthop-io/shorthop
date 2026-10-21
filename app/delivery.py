"""Sending the notifications the escalation engine queued. Run from the dispatch loop.

Each pending row is one destination. A failure is retried with exponential backoff up to
DELIVERY_MAX_ATTEMPTS, then marked failed with the reason — never dropped silently, because
"we tried to page you and could not" is itself the most important thing to see in the log.

The message is built at SEND time from the incident's current state, so a retry of a
"triggered" page sent after somebody acknowledged it says so.
"""
import datetime as dt
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import smtplib
import socket
import time
from email.message import EmailMessage
from urllib.parse import urlparse

import requests
from prometheus_client import Counter
from sqlalchemy import select

from . import alerts as alert_ops
from . import config as cfg
from . import relay, settings
from .db import SessionLocal
from .models import Channel, Device, Incident, Notification, now

logger = logging.getLogger(__name__)

SENT = Counter("shorthop_notifications_total", "Notification delivery attempts, by kind and outcome.",
               ["kind", "outcome"])
for _k in ("email", "webhook", "push", "slack"):
    for _o in ("sent", "retry", "failed"):
        SENT.labels(_k, _o)

WEBHOOK_TIMEOUT = 10
# Expo's push service fronts FCM and APNs. A FIXED endpoint, never user-supplied.
EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"


class DeliveryError(Exception):
    """Caller-safe reason, stored on the notification row."""


# --- message content -------------------------------------------------------------------

def _incident_url(inc: Incident) -> str:
    return f"{cfg.PUBLIC_URL}/incidents/{inc.id}" if cfg.PUBLIC_URL else ""


def _reassigned_line(inc: Incident) -> str:
    if not inc.reassigned_at:
        return ""
    return f"Moved to this team by {inc.reassigned_by} {inc.reassign_note or ''}".rstrip() + "."


def _history_line(inc: Incident) -> str:
    """The differentiator, in the page itself: what fixed this alert the last time."""
    first = next((o for o in inc.alerts), None)
    if first is None:
        return ""
    h = alert_ops.history(first.signature, exclude_id=first.id, limit=10)
    if not h["total"]:
        return "First time this alert has fired."
    line = f"Seen {h['total']} time(s) before."
    if h["recent_fixes"]:
        f = h["recent_fixes"][0]
        who = f" by {f.resolved_by}" if f.resolved_by else ""
        line += f" Last fixed{who}: {f.resolution_category or ''} {f.resolution_note or ''}".rstrip()
    return line


def payload(inc: Incident, event: str) -> dict:
    alerts = [{"id": o.id, "alertname": o.alertname, "status": o.status, "severity": o.severity,
               "labels": o.labels, "annotations": o.annotations,
               "started_at": o.started_at.isoformat() if o.started_at else None,
               "resolved_at": o.resolved_at.isoformat() if o.resolved_at else None}
              for o in inc.alerts]
    return {
        "version": "1",
        "event": event,
        "incident": {
            "id": inc.id, "number": inc.number, "title": inc.title, "severity": inc.severity,
            "status": inc.status, "url": _incident_url(inc), "group_labels": inc.group_labels,
            "opened_at": inc.opened_at.isoformat() if inc.opened_at else None,
            "acknowledged_by": inc.acked_by,
            "resolved_at": inc.resolved_at.isoformat() if inc.resolved_at else None,
            "history": _history_line(inc),
            "reassigned": _reassigned_line(inc) or None,
        },
        "alerts": alerts,
    }


def _email(inc: Incident, event: str) -> tuple[str, str]:
    open_alerts = [o for o in inc.alerts if o.resolved_at is None]
    verb = {"triggered": "FIRING", "escalated": "STILL FIRING", "resolved": "RESOLVED",
            "test": "TEST"}.get(event, event.upper())
    sev = f"[{inc.severity}] " if inc.severity else ""
    subject = f"[{verb}] #{inc.number} {sev}{inc.title}"
    lines = [f"Incident #{inc.number}: {inc.title}", f"Status: {inc.status}"]
    if inc.acked_by:
        lines.append(f"Acknowledged by {inc.acked_by}")
    url = _incident_url(inc)
    if url:
        lines += ["", f"Open in Shorthop: {url}"]
    if inc.reassigned_at:
        lines += ["", _reassigned_line(inc)]
    lines += ["", _history_line(inc), "", f"Alerts ({len(open_alerts)} open of {len(inc.alerts)}):"]
    for o in inc.alerts[:25]:
        summary = (o.annotations or {}).get("summary", "")
        state = "resolved" if o.resolved_at else o.status
        lines.append(f"- [{state}] {o.alertname}: {summary}")
        lines.append("    " + " ".join(f"{k}={v}" for k, v in sorted((o.labels or {}).items())
                                       if k != "alertname"))
        runbook = (o.annotations or {}).get("runbook_url")
        if runbook:
            lines.append(f"    runbook: {runbook}")
    if len(inc.alerts) > 25:
        lines.append(f"…and {len(inc.alerts) - 25} more")
    return subject[:250], "\n".join(lines)


# --- transports ------------------------------------------------------------------------

def send_email(to: str, subject: str, body: str) -> None:
    smtp = settings.smtp()  # the Settings page, falling back to the environment
    if not smtp.host or not smtp.sender:
        raise DeliveryError("email is not configured (Settings → Email: server and From address)")
    msg = EmailMessage()
    msg["From"] = smtp.sender
    msg["To"] = to
    # OX Agent: Header Injection prevented — CR/LF stripped from the header value
    msg["Subject"] = subject.replace("\r", " ").replace("\n", " ")
    msg.set_content(body)
    try:
        if smtp.port == 465:
            server = smtplib.SMTP_SSL(smtp.host, smtp.port, timeout=20)
        else:
            server = smtplib.SMTP(smtp.host, smtp.port, timeout=20)
            if smtp.starttls:
                server.starttls()
        with server:
            if smtp.user:
                server.login(smtp.user, smtp.password)
            server.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        raise DeliveryError(f"SMTP: {type(e).__name__}: {str(e)[:200]}") from None


# --- webhook channels ------------------------------------------------------------------
#
# A webhook channel is a URL plus, optionally, a method, extra headers and a body template —
# that is what makes one channel type reach Slack, Telegram, Teams, Discord or Jira, each of
# which wants its own JSON shape. Without a template the body is Shorthop's own JSON
# (`payload()`), signed with the channel secret.
#
# The destination must be a PUBLIC address (webhook_destination_error): a channel is a URL
# the server fetches with headers of the author's choosing, so without the check anyone with
# config:write — or their leaked token — could make Shorthop call its own database, the
# cluster API or a cloud metadata endpoint. Hosts in WEBHOOK_PRIVATE_HOSTS (Settings) are
# exempt, for an internal chat server. Also: http(s) only, no redirects, a timeout.

WEBHOOK_METHODS = ("POST", "PUT", "PATCH")
WEBHOOK_EVENTS = ("triggered", "escalated", "resolved")
# Headers Shorthop owns; a channel may not override them (the signature would lie).
RESERVED_HEADERS = {"content-length", "host", "x-shorthop-signature", "x-shorthop-timestamp",
                    "x-shorthop-event"}
# {{name}} = a built-in variable (template_vars), or a label when no built-in has that name;
# {{labels.name}} / {{annotations.name}} = always that alert field. Label names follow the
# Prometheus rule ([A-Za-z_][A-Za-z0-9_]*).
_PLACEHOLDER = re.compile(
    r"\{\{\s*((?:labels|annotations)\.[A-Za-z_][A-Za-z0-9_]*|[A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def webhook_url_ok(url: str) -> bool:
    parsed = urlparse(url or "")
    return parsed.scheme in ("http", "https") and bool(parsed.hostname)


def _resolve(host: str, port: int) -> list[str]:
    """Every address `host` resolves to. A seam: the tests replace it."""
    return [ai[4][0] for ai in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]


def _public(addr: str) -> bool:
    ip = ipaddress.ip_address(addr.split("%", 1)[0])
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    # is_global is False for private, loopback, link-local (169.254.x — the cloud metadata
    # address), carrier-grade NAT, reserved and documentation ranges.
    return ip.is_global and not ip.is_multicast


def webhook_destination_error(url: str) -> str | None:
    """Why Shorthop will not call this URL, or None. Checked when a channel is saved AND
    before every send — DNS can change after the channel was saved.

    Known gap: the address checked here and the one `requests` connects to come from two
    lookups, so a DNS server answering differently the second time (rebinding) could slip
    through. Bounded by redirects being off; closing it means pinning the connection."""
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not host:
        return "the webhook URL is not an http(s) URL"
    if host in settings.webhook_private_hosts():
        return None
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        addrs = _resolve(host, port)
    except (OSError, ValueError, UnicodeError):
        return f"{host} does not resolve"
    if not addrs:
        return f"{host} does not resolve"
    if not all(_public(a) for a in addrs):
        return (f"{host} points at a private or internal address — add it to "
                "WEBHOOK_PRIVATE_HOSTS (Settings) if that is intended")
    return None


def redact_url(url: str) -> str:
    """What may be shown to anyone who can read channels or the delivery log. A Slack or
    Discord webhook URL and a Telegram bot URL ARE the credential, so only the origin shows."""
    parsed = urlparse(url or "")
    if not parsed.hostname:
        return ""
    return f"{parsed.scheme}://{parsed.netloc.rsplit('@', 1)[-1]}/…"


def template_vars(body: dict) -> dict[str, str]:
    """The placeholders a body template may use, derived from the Shorthop payload."""
    inc = body.get("incident") or {}
    alerts = body.get("alerts") or []
    event = body.get("event", "")
    verb = {"triggered": "FIRING", "escalated": "STILL FIRING", "resolved": "RESOLVED",
            "test": "TEST"}.get(event, event.upper())
    first = (alerts[0].get("annotations") or {}) if alerts else {}
    open_n = sum(1 for a in alerts if not a.get("resolved_at"))
    title = inc.get("title") or (f"Test from Shorthop channel {body.get('channel')}"
                                 if event == "test" else "")
    sev = inc.get("severity") or ""
    number = str(inc.get("number") or "")
    url = inc.get("url") or ""
    head = f"[{verb}] " + (f"#{number} " if number else "") + (f"[{sev}] " if sev else "") + title
    lines = []
    for a in alerts[:25]:
        summ = (a.get("annotations") or {}).get("summary", "")
        lines.append(f"- [{'resolved' if a.get('resolved_at') else a.get('status')}] "
                     f"{a.get('alertname')}: {summ}".rstrip(": "))
    alerts_text = "\n".join(lines)
    text = "\n".join(x for x in [head, url, inc.get("reassigned") or "", inc.get("history") or "",
                                 alerts_text] if x)
    return {
        "event": event, "verb": verb, "number": number, "title": title, "severity": sev,
        "status": inc.get("status") or "", "url": url, "history": inc.get("history") or "",
        "summary": first.get("summary", ""), "description": first.get("description", ""),
        "alert_count": str(len(alerts)), "open_count": str(open_n),
        "acknowledged_by": inc.get("acknowledged_by") or "", "alerts_text": alerts_text,
        "reassigned": inc.get("reassigned") or "",
        "text": text, "headline": head,
    }


TEMPLATE_VARIABLES = tuple(template_vars({"event": "test"}).keys())


def _alert_fields(body: dict) -> tuple[dict, dict]:
    """The labels and annotations a template reads: the incident's group labels, overlaid by
    the first still-open alert (else the first alert). That alert carries what label rules
    added, since rules run on every alert before routing."""
    alerts = body.get("alerts") or []
    first = next((a for a in alerts if not a.get("resolved_at")), alerts[0] if alerts else {})
    labels = {**((body.get("incident") or {}).get("group_labels") or {}), **(first.get("labels") or {})}
    return labels, dict(first.get("annotations") or {})


def render_template(template: str, body: dict, known_labels: set[str] | None = None) -> bytes:
    """Substitute {{placeholders}} in a JSON template. Values are JSON-string-escaped, so a
    placeholder belongs INSIDE quotes: {"text": "{{text}}"}. The result must be valid JSON.

    `known_labels` = validating on save: a bare {{name}} that is neither a built-in nor in
    this set is refused, so a typo of a built-in fails then rather than sending a blank. At
    send time (None) a label or annotation an alert does not carry renders empty — alerts
    differ, and a page with an empty field beats a page that is never sent."""
    values = template_vars(body)
    labels, annotations = _alert_fields(body)

    def sub(m):
        key = m.group(1)
        if key in values:
            v = values[key]
        elif key.startswith("labels."):
            v = labels.get(key[7:], "")
        elif key.startswith("annotations."):
            v = annotations.get(key[12:], "")
        elif known_labels is None or key in known_labels:
            v = labels.get(key, "")
        else:
            raise DeliveryError(f"unknown placeholder {{{{{key}}}}} — for an alert label "
                                f"use {{{{labels.{key}}}}}")
        return json.dumps(str(v))[1:-1]
    out = _PLACEHOLDER.sub(sub, template)
    try:
        json.loads(out)
    except ValueError as e:
        raise DeliveryError(f"the body template does not render to valid JSON ({e})") from None
    return out.encode()


def send_webhook(config: dict, body: dict, secret: str | None) -> None:
    url = str(config.get("url") or "")
    err = webhook_destination_error(url)
    if err:
        raise DeliveryError(err)
    method = str(config.get("method") or "POST").upper()
    if method not in WEBHOOK_METHODS:
        raise DeliveryError(f"unsupported method {method}")
    template = config.get("body_template")
    raw = (render_template(template, body) if template
           else json.dumps(body, separators=(",", ":"), default=str).encode())
    ts = str(int(time.time()))
    headers = {"Content-Type": "application/json", "User-Agent": "shorthop"}
    for k, v in (config.get("headers") or {}).items():
        if k.lower() not in RESERVED_HEADERS:
            headers[k] = v
    headers.update({"X-Shorthop-Event": body.get("event", ""), "X-Shorthop-Timestamp": ts})
    if secret:
        # Signed over "<timestamp>.<body>" so a captured request cannot be replayed later
        # with a fresh timestamp.
        sig = hmac.new(secret.encode(), ts.encode() + b"." + raw, hashlib.sha256).hexdigest()
        headers["X-Shorthop-Signature"] = f"sha256={sig}"
    try:
        resp = requests.request(method, url, data=raw, headers=headers, timeout=WEBHOOK_TIMEOUT,
                                allow_redirects=False)
    except requests.RequestException as e:
        raise DeliveryError(f"could not reach the webhook ({type(e).__name__})") from None
    if not 200 <= resp.status_code < 300:
        detail = (resp.text or "").strip().replace("\n", " ")[:200]
        raise DeliveryError(f"webhook answered HTTP {resp.status_code}"
                            + (f": {detail}" if detail else ""))


def send_push(db, device_id: str, inc: Incident, event: str) -> None:
    dev = db.get(Device, device_id)
    if dev is None or dev.revoked or not dev.push_token:
        raise DeliveryError("that phone is unpaired or has no push token")
    if relay.configured():
        _send_push_via_relay(dev, inc, event)
        return
    verb = {"triggered": "Firing", "escalated": "Still firing", "resolved": "Resolved",
            "test": "Test"}.get(event, event)
    open_n = sum(1 for o in inc.alerts if o.resolved_at is None)
    message = {
        "to": dev.push_token,
        "title": f"{verb}: #{inc.number} {inc.title}"[:180],
        "body": (f"{inc.severity + ' · ' if inc.severity else ''}{open_n} open alert(s). "
                 + (_reassigned_line(inc) + " " if inc.reassigned_at else "")
                 + _history_line(inc))[:400],
        # The app opens this incident when the notification is tapped, and its
        # Acknowledge action calls the API with the phone's own token.
        "data": {"incident_id": inc.id, "event": event},
        "sound": "default", "priority": "high", "channelId": "alerts",
    }
    if event != "resolved":
        message["categoryId"] = "incident"  # the app's Acknowledge / Open actions
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if cfg.EXPO_ACCESS_TOKEN:
        headers["Authorization"] = f"Bearer {cfg.EXPO_ACCESS_TOKEN}"
    try:
        resp = requests.post(EXPO_PUSH_URL, json=message, headers=headers, timeout=WEBHOOK_TIMEOUT,
                             allow_redirects=False)
    except requests.RequestException as e:
        raise DeliveryError(f"could not reach Expo push ({type(e).__name__})") from None
    if resp.status_code != 200:
        raise DeliveryError(f"Expo push answered HTTP {resp.status_code}")
    # ⚠️ Expo answers 200 even for a rejected message — the verdict is in the ticket.
    ticket = ((resp.json() or {}).get("data") or {})
    if isinstance(ticket, list):
        ticket = ticket[0] if ticket else {}
    if ticket.get("status") == "error":
        err = (ticket.get("details") or {}).get("error") or ticket.get("message") or "rejected"
        if err == "DeviceNotRegistered":
            dev.push_token = None  # the app was removed; stop trying this phone
        raise DeliveryError(f"Expo push: {err}")


def _send_push_via_relay(dev: Device, inc: Incident, event: str) -> None:
    """Page a phone through the push relay. The relay gets the incident id and the event —
    never the title, labels or history; the app fetches those from this server on open.

    Every failure is a DeliveryError, so the outbox retries it with backoff and the Delivery
    log shows the reason. Changes to `dev` are committed with the notification's outcome."""
    relay.sync_device(dev)   # a phone paired while the relay was down registers on its first page
    if dev.relay_status == "signed_out":
        raise DeliveryError("push relay: this phone was signed out after going unused — "
                            "opening the Shorthop app on it signs it in again")
    if dev.relay_status != "registered":
        raise DeliveryError(f"push relay: {dev.relay_message or 'this phone is not registered'}")
    try:
        r = relay.push(dev.relay_device_id, inc.id, event)
    except relay.RelayError as e:
        raise DeliveryError(f"push relay: {e}") from None
    if r.outcome == "ok":
        return
    if r.outcome == "unregistered" or r.reason == "unregistered":
        # The app was removed from the phone; the relay has already freed the seat.
        relay.mark_signed_out_unused(dev, "unregistered")
        raise DeliveryError("Expo push: DeviceNotRegistered")
    if r.outcome == "signed_out" and r.reason == "stale":
        relay.mark_signed_out_unused(dev, "stale")
        raise DeliveryError("push relay: this phone was signed out after going unused — "
                            "opening the Shorthop app on it signs it in again")
    if r.outcome == "signed_out":
        # The relay signed the phone out (e.g. not heard from for 60 days). Forget the
        # registration; the next attempt signs it in again.
        relay.forget(dev)
        raise DeliveryError(f"push relay: the phone was signed out ({r.reason or 'no reason given'}); "
                            "it is signed in again on the next attempt")
    raise DeliveryError(f"push relay: {r.outcome}{': ' + r.error if r.error else ''}")


# --- the loop ----------------------------------------------------------------------------

def _send(db, n: Notification) -> None:
    inc = db.get(Incident, n.incident_id)
    if n.kind == "email":
        subject, body = _email(inc, n.event)
        send_email(n.destination, subject, body)
    elif n.kind == "webhook":
        ch = db.get(Channel, n.channel_id) if n.channel_id else None
        if ch is None:
            raise DeliveryError("the channel no longer exists")
        send_webhook(ch.config or {}, payload(inc, n.event), ch.secret)
    elif n.kind == "push":
        send_push(db, n.destination, inc, n.event)
    elif n.kind == "slack":
        from . import slack
        try:
            slack.deliver(db, n, inc)
        except slack.SlackError as e:
            raise DeliveryError(f"Slack: {e}") from None
    else:
        raise DeliveryError(f"unknown kind {n.kind}")


def _event_not_wanted(db, n: Notification) -> bool:
    """A webhook channel may send only some events — a Jira channel that opens an issue
    wants `triggered`, not a second issue per escalation and a third on resolve."""
    if n.kind != "webhook" or not n.channel_id:
        return False
    ch = db.get(Channel, n.channel_id)
    events = (ch.config or {}).get("events") if ch else None
    return bool(events) and n.event in WEBHOOK_EVENTS and n.event not in events


def _claim_pending(db, t: dt.datetime, skip: set[str]) -> Notification | None:
    """Lock ONE notification that is due, skipping any another replica is sending.

    ⚠️ The row lock is held while the page is SENT and released by the commit that records
    the outcome — so two replicas can never send the same page. If a replica dies mid-send
    the lock goes with its session and the page is sent again: at-least-once, which is the
    right failure for a pager (a duplicate page beats a lost one)."""
    return db.scalar(select(Notification).where(
        Notification.status == "pending", Notification.next_attempt_at <= t,
        Notification.id.not_in(skip or {""}))
        .order_by(Notification.created_at).limit(1).with_for_update(skip_locked=True))


def deliver_pending(limit: int = 50) -> int:
    """Send due notifications, one claimed row per transaction. Safe with several replicas.
    Returns how many rows were handled."""
    db = SessionLocal
    t = now()
    handled: set[str] = set()
    while len(handled) < limit:
        n = _claim_pending(db, t, handled)
        if n is None:
            db.rollback()
            break
        handled.add(n.id)
        try:
            _deliver_one(db, n)
            db.commit()
        except Exception:  # noqa: BLE001 — never let one row stop the queue
            db.rollback()
            logger.exception("notification %s: could not record the outcome", n.id)
    return len(handled)


def _deliver_one(db, n: Notification) -> None:
    if n.until_ack and n.event in ("triggered", "escalated"):
        inc = db.get(Incident, n.incident_id)
        if inc is None or inc.status != "triggered":
            who = (inc.acked_by or inc.resolved_by) if inc else None
            n.status = "suppressed"
            n.error = f"{inc.status if inc else 'gone'}{' by ' + who if who else ''} before this delayed page was due"
            return
    if _event_not_wanted(db, n):
        n.status, n.error = "suppressed", f"the channel does not send '{n.event}' events"
        return
    n.attempts += 1
    try:
        _send(db, n)
        n.status, n.sent_at, n.error = "sent", now(), None
        SENT.labels(n.kind, "sent").inc()
    except DeliveryError as e:
        n.error = str(e)
        if n.attempts >= cfg.DELIVERY_MAX_ATTEMPTS:
            n.status = "failed"
            SENT.labels(n.kind, "failed").inc()
            logger.warning("notification %s failed for good: %s", n.id, e)
        else:
            n.next_attempt_at = now() + dt.timedelta(
                seconds=cfg.DELIVERY_RETRY_BASE_SECONDS * 2 ** (n.attempts - 1))
            SENT.labels(n.kind, "retry").inc()
    except Exception:  # noqa: BLE001
        logger.exception("notification %s: unexpected error", n.id)
        n.error = "unexpected error — see Shorthop's logs"
        n.next_attempt_at = now() + dt.timedelta(seconds=cfg.DELIVERY_RETRY_BASE_SECONDS)
        if n.attempts >= cfg.DELIVERY_MAX_ATTEMPTS:
            n.status = "failed"
