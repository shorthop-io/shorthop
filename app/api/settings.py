"""Admin settings: the Prometheus host allowlist, the SMTP server and the push relay. Admin scope only —
these decide where Shorthop connects to and what it sends mail as."""
import ipaddress
import re

from flask import g
from flask_smorest import Blueprint, abort
from marshmallow import EXCLUDE, Schema, fields, validate

from .. import delivery, relay, slack
from .. import settings as st
from ..core import levels as lv
from ..audit import record
from ..auth import require
from ..db import SessionLocal

blp = Blueprint("settings", __name__, url_prefix="/api/v1",
                description="Admin settings: Prometheus hosts, email server, push relay")

db = SessionLocal
DNS_NAME = re.compile(r"^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?)*$")


def _host_ok(h: str) -> bool:
    """A bare host: a DNS name or an IP address — what urlparse().hostname yields, which is
    what the allowlist is compared against. No scheme, port or path."""
    if DNS_NAME.match(h):
        return True
    try:
        ipaddress.ip_address(h.strip("[]"))
        return True
    except ValueError:
        return False
EMAIL = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")
SENDER = re.compile(r"^(?:[^<>\r\n]{0,100}<[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+>|[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+)$")


class SettingsInSchema(Schema):
    """Partial: only keys present are written. An explicit null resets a key to the
    environment value. The password: omit to keep it, "" or null to clear the override."""

    class Meta:
        unknown = EXCLUDE

    prometheus_allowed_hosts = fields.List(fields.Str(), allow_none=True,
                                           validate=validate.Length(max=50))
    webhook_private_hosts = fields.List(fields.Str(), allow_none=True,
                                        validate=validate.Length(max=50))
    smtp_host = fields.Str(allow_none=True, validate=validate.Length(max=253))
    smtp_port = fields.Int(allow_none=True, validate=validate.Range(min=1, max=65535))
    smtp_user = fields.Str(allow_none=True, validate=validate.Length(max=254))
    smtp_password = fields.Str(allow_none=True, validate=validate.Length(max=500))
    smtp_from = fields.Str(allow_none=True, validate=validate.Length(max=254))
    smtp_starttls = fields.Bool(allow_none=True)
    relay_url = fields.Str(allow_none=True, validate=validate.Length(max=253))
    relay_key = fields.Str(allow_none=True, validate=validate.Length(max=200))
    relay_send_content = fields.Bool(allow_none=True)
    slack_bot_token = fields.Str(allow_none=True, validate=validate.Length(max=300))
    slack_app_token = fields.Str(allow_none=True, validate=validate.Length(max=300))
    incident_levels = fields.List(fields.Dict(), allow_none=True)


class SmtpTestSchema(Schema):
    class Meta:
        unknown = EXCLUDE

    to = fields.Str(load_default=None, allow_none=True)


def _clean(payload: dict) -> dict:
    out = dict(payload)
    for key in ("prometheus_allowed_hosts", "webhook_private_hosts"):
        if out.get(key) is None:
            continue
        hosts = []
        for raw in out[key]:
            h = (raw or "").strip().lower()
            if not h:
                continue
            if not _host_ok(h):
                abort(422, message=f"'{raw}' — give only a host name or IP, no scheme, port or path")
            h = h.strip("[]")
            if h not in hosts:
                hosts.append(h)
        out[key] = hosts
    for key in ("smtp_host", "smtp_user", "smtp_from", "smtp_password"):
        v = out.get(key)
        if isinstance(v, str):
            # OX Agent: Header Injection prevented — no CR/LF in anything that reaches SMTP
            if "\r" in v or "\n" in v:
                abort(422, message=f"{key}: line breaks are not allowed")
            if key != "smtp_password":
                v = v.strip()
            out[key] = v or None  # blank = back to the environment value
    for key in ("relay_url", "relay_key"):
        v = out.get(key)
        if isinstance(v, str):
            out[key] = v.strip().rstrip("/") if key == "relay_url" else v.strip()
            out[key] = out[key] or None   # blank = back to the environment value
    if out.get("relay_url") and not relay.valid_url(out["relay_url"]):
        abort(422, message="relay_url: https://host — no path, e.g. https://relay.shorthop.io")
    if out.get("relay_key") and not relay.KEY_RE.match(out["relay_key"]):
        abort(422, message="relay_key: the installation key from shorthop.io (shr_…)")
    for key, prefix in (("slack_bot_token", "xoxb-"), ("slack_app_token", "xapp-")):
        v = out.get(key)
        if isinstance(v, str):
            v = v.strip()
            if v and (not v.startswith(prefix) or not re.fullmatch(r"[A-Za-z0-9-]+", v)):
                abort(422, message=f"{key}: the token Slack shows, starting {prefix}")
            out[key] = v or None
    if out.get("incident_levels") is not None:
        try:
            out["incident_levels"] = lv.validate(out["incident_levels"])
        except lv.LevelError as e:
            abort(422, message=f"incident_levels: {e}")
    if out.get("smtp_host") and not _host_ok(out["smtp_host"].lower()):
        abort(422, message="smtp_host: a host name, e.g. smtp.example.com (no scheme or port)")
    if out.get("smtp_from") and not SENDER.match(out["smtp_from"]):
        abort(422, message="smtp_from: an address, or 'Name <address>'")
    return out


@blp.route("/settings")
@require("admin")
def get_settings():
    return {"settings": st.describe()}


@blp.route("/settings", methods=["PUT"])
@require("admin")
@blp.arguments(SettingsInSchema)
def put_settings(payload):
    changed = st.put(_clean(payload), g.identity.name)
    db.commit()
    if changed:
        # The password VALUE is never written to the audit log — only that it changed.
        record(g.identity.name, "settings.update", detail=", ".join(changed))
    out = {"settings": st.describe(), "changed": changed}
    if {"slack_bot_token", "slack_app_token"} & set(changed):
        # Connect (or disconnect) this replica now, so the page can say whether the tokens
        # work; the others follow within SLACK_RECONCILE_SECONDS.
        slack.reconcile()
        out["slack"] = slack.status()
    if {"relay_url", "relay_key"} & set(changed):
        # Every phone's registration belonged to the old relay/installation: redo them all now.
        out["relay_phones"] = relay.resync_all(db)
        db.commit()
    return out


@blp.route("/settings/relay/status")
@require("admin")
def relay_status():
    """The relay's view of this install (SHOR-14): seats used and free, card on file, and the
    customer account page where the installation's contact manages phones, card and invoices."""
    return relay.installation()


@blp.route("/settings/smtp/test", methods=["POST"])
@require("admin")
@blp.arguments(SmtpTestSchema)
def test_smtp(payload):
    """Send a test email with the CURRENT settings — to yourself unless an address is given."""
    to = (payload.get("to") or g.identity.user.email or "").strip()
    if not EMAIL.match(to):
        abort(422, message="to: an email address")
    try:
        delivery.send_email(to, "[TEST] Shorthop email settings",
                            f"This is a test from Shorthop, sent by {g.identity.name}.\n"
                            "If you can read it, Shorthop can send email.")
    except delivery.DeliveryError as e:
        record(g.identity.name, "settings.smtp_test", target=to, status="error", detail=str(e))
        return {"ok": False, "error": str(e), "to": to}
    record(g.identity.name, "settings.smtp_test", target=to)
    return {"ok": True, "error": None, "to": to}


# --- the Slack app -----------------------------------------------------------------------------

@blp.route("/slack/status")
@require("admin")
def slack_status():
    """Whether THIS replica is connected to Slack, as which bot, in which workspace."""
    return slack.status()


@blp.route("/slack/manifest")
@require("admin")
def slack_manifest():
    """The app manifest to paste into api.slack.com/apps → Create New App → From a manifest."""
    return {"manifest": slack.manifest()}


@blp.route("/slack/channels")
@require("config:write")
def slack_channels():
    """Channels the Slack app can see — the picker on a Slack channel."""
    try:
        return {"channels": slack.list_channels(), "error": None}
    except slack.SlackError as e:
        return {"channels": [], "error": str(e)}
