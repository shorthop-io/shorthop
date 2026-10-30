"""M2 API: routes, escalation policies, channels, inhibitions, incidents, notifications."""
import re
import secrets as _secrets

from flask import g
from flask_smorest import Blueprint, abort
from marshmallow import EXCLUDE, Schema, fields, validate
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from .. import alerts as alert_ops
from .. import delivery, incidents
from ..alerts import RESOLUTION_CATEGORIES
from ..audit import record
from ..auth import require
from ..core import enrich, matchers
from ..db import SessionLocal
from ..models import (
    Channel, EscalationPolicy, Incident, Inhibition, Integration, JiraIssue, LabelRule, Notification, Route,
    Schedule, Team, TeamMember, User,
)
from .schemas import OccurrenceSchema

blp = Blueprint("paging", __name__, url_prefix="/api/v1",
                description="Routing, escalation, channels, inhibitions, incidents and delivery")
db = SessionLocal

LABEL_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
HEADER_NAME = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,100}$")
# The fill-me-in markers the web presets put in a URL, template or header (`<BOT_TOKEN>`,
# `<CHAT_ID>`, `<site>`, `…`). Saved as-is they reach the receiver verbatim, and the
# receiver's answer ("chat not found") names nothing about which field is still empty.
PRESET_PLACEHOLDER = re.compile(r"<(?:[A-Z][A-Z0-9_]*|site|base64 of [^<>]{1,40})>|…")


def _unfilled(field: str, value: str) -> None:
    m = PRESET_PLACEHOLDER.search(value or "")
    if m:
        abort(422, message=f"{field}: replace the placeholder {m.group(0)} with your own value")
MAX_STEPS = 10
MAX_TARGETS = 20


class _In(Schema):
    class Meta:
        unknown = EXCLUDE


def _commit_or_conflict(message: str) -> None:
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        abort(409, message=message)


def _get(model, obj_id, what):
    row = db.get(model, obj_id)
    if row is None:
        abort(404, message=f"{what} not found")
    return row


def _clean_matchers(raw, allow_all: bool) -> list:
    try:
        return matchers.validate(raw or [], allow_match_all=allow_all)
    except matchers.MatcherError as e:
        abort(422, message=str(e))


def _check_ref(model, obj_id, what):
    if obj_id is not None and db.get(model, obj_id) is None:
        abort(422, message=f"{what} {obj_id} does not exist")


class OrderSchema(_In):
    ids = fields.List(fields.Str(), required=True)


# --- label rules ---------------------------------------------------------------------------
# Run at ingest, before routing: add or change labels from labels and/or the alert's text.

class LabelRuleSchema(Schema):
    id = fields.Str()
    position = fields.Int()
    name = fields.Str()
    matchers = fields.List(fields.Dict())
    text_pattern = fields.Str(allow_none=True)
    text_field = fields.Str(allow_none=True)
    set_labels = fields.Dict()
    integration_ids = fields.List(fields.Str(), allow_none=True)
    stop = fields.Bool()
    enabled = fields.Bool()
    created_at = fields.DateTime()


class LabelRuleInSchema(_In):
    name = fields.Str(required=True, validate=validate.Length(min=1, max=200))
    matchers = fields.List(fields.Dict(), load_default=list)
    text_pattern = fields.Str(load_default=None, allow_none=True)
    text_field = fields.Str(load_default=None, allow_none=True)
    set_labels = fields.Dict(required=True)
    integration_ids = fields.List(fields.Str(), load_default=None, allow_none=True)
    stop = fields.Bool(load_default=False)
    enabled = fields.Bool(load_default=True)


def _label_rule_fields(payload) -> dict:
    payload["matchers"] = _clean_matchers(payload.get("matchers"), allow_all=True)
    try:
        payload["text_pattern"], payload["text_field"] = enrich.validate_text(
            payload.get("text_pattern"), payload.get("text_field"))
        payload["set_labels"] = enrich.validate_set_labels(payload.get("set_labels"))
    except enrich.RuleError as e:
        abort(422, message=str(e))
    for iid in payload.get("integration_ids") or []:
        _check_ref(Integration, iid, "integration")
    return payload


@blp.route("/label-rules")
@require("oncall:read")
@blp.response(200, LabelRuleSchema(many=True))
def list_label_rules():
    """In evaluation order: every matching rule applies, later ones overwrite earlier ones."""
    return db.scalars(select(LabelRule).order_by(LabelRule.position, LabelRule.name)).all()


@blp.route("/label-rules", methods=["POST"])
@require("config:write")
@blp.arguments(LabelRuleInSchema)
@blp.response(201, LabelRuleSchema)
def create_label_rule(payload):
    """Added at the END (it wins over the ones above). Move it with PUT /label-rules/order."""
    payload = _label_rule_fields(payload)
    last = db.scalar(select(LabelRule.position).order_by(LabelRule.position.desc()).limit(1))
    row = LabelRule(position=(last or 0) + 10, **payload)
    db.add(row)
    db.commit()
    record(g.identity.name, "label_rule.create", target=row.name)
    return row


@blp.route("/label-rules/<rule_id>", methods=["PUT"])
@require("config:write")
@blp.arguments(LabelRuleInSchema)
@blp.response(200, LabelRuleSchema)
def update_label_rule(payload, rule_id):
    """Applies to new alerts and to open ones the next time their sender repeats them."""
    row = _get(LabelRule, rule_id, "label rule")
    for k, v in _label_rule_fields(payload).items():
        setattr(row, k, v)
    db.commit()
    record(g.identity.name, "label_rule.update", target=row.name)
    return row


@blp.route("/label-rules/<rule_id>", methods=["DELETE"])
@require("config:write")
@blp.response(204)
def delete_label_rule(rule_id):
    row = _get(LabelRule, rule_id, "label rule")
    db.delete(row)
    db.commit()
    record(g.identity.name, "label_rule.delete", target=row.name)


@blp.route("/label-rules/order", methods=["PUT"])
@require("config:write")
@blp.arguments(OrderSchema)
@blp.response(200, LabelRuleSchema(many=True))
def order_label_rules(payload):
    rows = {r.id: r for r in db.scalars(select(LabelRule)).all()}
    if set(payload["ids"]) != set(rows):
        abort(422, message="ids must list every label rule exactly once")
    for i, rid in enumerate(payload["ids"]):
        rows[rid].position = (i + 1) * 10
    db.commit()
    record(g.identity.name, "label_rule.order")
    return sorted(rows.values(), key=lambda r: r.position)


class LabelRuleTestSchema(_In):
    labels = fields.Dict(keys=fields.Str(), values=fields.Str(), required=True)
    annotations = fields.Dict(keys=fields.Str(), values=fields.Str(), load_default=dict)
    integration_id = fields.Str(load_default="")


@blp.route("/label-rules/test", methods=["POST"])
@require("oncall:read")
@blp.arguments(LabelRuleTestSchema)
def test_label_rules(payload):
    """What the rules would do to an alert with these labels and this text — every rule's
    verdict, the labels that come out, and which route would then take it."""
    views = [enrich.RuleView(r.id, r.name, r.position, r.matchers or [], r.text_pattern,
                             r.text_field, r.set_labels or {}, r.integration_ids, r.stop,
                             r.enabled)
             for r in db.scalars(select(LabelRule))]
    out = enrich.trace(views, payload["labels"], payload["annotations"], payload["integration_id"])
    out["routing"] = incidents.explain_routing(out["labels"], payload["integration_id"])
    return out


# --- routes --------------------------------------------------------------------------------

class RouteSchema(Schema):
    id = fields.Str()
    position = fields.Int()
    name = fields.Str()
    matchers = fields.List(fields.Dict())
    integration_ids = fields.List(fields.Str(), allow_none=True)
    team_id = fields.Str(allow_none=True)
    escalation_policy_id = fields.Str(allow_none=True)
    group_by = fields.List(fields.Str())
    notify_resolved = fields.Bool()
    enabled = fields.Bool()
    created_at = fields.DateTime()


class RouteInSchema(_In):
    name = fields.Str(required=True, validate=validate.Length(min=1, max=200))
    matchers = fields.List(fields.Dict(), load_default=list)
    integration_ids = fields.List(fields.Str(), load_default=None, allow_none=True)
    team_id = fields.Str(load_default=None, allow_none=True)
    escalation_policy_id = fields.Str(load_default=None, allow_none=True)
    group_by = fields.List(fields.Str(), load_default=lambda: ["alertname"])
    notify_resolved = fields.Bool(load_default=True)
    enabled = fields.Bool(load_default=True)


def _route_fields(payload) -> dict:
    payload["matchers"] = _clean_matchers(payload.get("matchers"), allow_all=True)
    bad = [x for x in payload.get("group_by") or [] if not LABEL_NAME.match(x)]
    if bad:
        abort(422, message=f"group_by: not label names: {', '.join(bad)}")
    _check_ref(Team, payload.get("team_id"), "team")
    _check_ref(EscalationPolicy, payload.get("escalation_policy_id"), "escalation policy")
    for iid in payload.get("integration_ids") or []:
        _check_ref(Integration, iid, "integration")
    return payload


@blp.route("/routes")
@require("oncall:read")
@blp.response(200, RouteSchema(many=True))
def list_routes():
    """Routes in evaluation order: the first enabled match wins."""
    return db.scalars(select(Route).order_by(Route.position, Route.name)).all()


@blp.route("/routes", methods=["POST"])
@require("config:write")
@blp.arguments(RouteInSchema)
@blp.response(201, RouteSchema)
def create_route(payload):
    """Added at the END of the list (above nothing): move it with PUT /routes/order."""
    payload = _route_fields(payload)
    last = db.scalar(select(Route.position).order_by(Route.position.desc()).limit(1))
    row = Route(position=(last or 0) + 10, **payload)
    db.add(row)
    db.commit()
    record(g.identity.name, "route.create", target=row.name)
    return row


@blp.route("/routes/<route_id>", methods=["PUT"])
@require("config:write")
@blp.arguments(RouteInSchema)
@blp.response(200, RouteSchema)
def update_route(payload, route_id):
    row = _get(Route, route_id, "route")
    for k, v in _route_fields(payload).items():
        setattr(row, k, v)
    db.commit()
    record(g.identity.name, "route.update", target=row.name)
    return row


@blp.route("/routes/<route_id>", methods=["DELETE"])
@require("config:write")
@blp.response(204)
def delete_route(route_id):
    """Open incidents keep running on the policy they were opened with."""
    row = _get(Route, route_id, "route")
    db.delete(row)
    db.commit()
    record(g.identity.name, "route.delete", target=row.name)


@blp.route("/routes/order", methods=["PUT"])
@require("config:write")
@blp.arguments(OrderSchema)
@blp.response(200, RouteSchema(many=True))
def order_routes(payload):
    rows = {r.id: r for r in db.scalars(select(Route)).all()}
    if set(payload["ids"]) != set(rows):
        abort(422, message="ids must list every route exactly once")
    for i, rid in enumerate(payload["ids"]):
        rows[rid].position = (i + 1) * 10
    db.commit()
    record(g.identity.name, "route.order")
    return sorted(rows.values(), key=lambda r: r.position)


class RouteTestSchema(_In):
    labels = fields.Dict(keys=fields.Str(), values=fields.Str(), required=True)
    integration_id = fields.Str(load_default="")


@blp.route("/routes/test", methods=["POST"])
@require("oncall:read")
@blp.arguments(RouteTestSchema)
def test_routes(payload):
    """Which route would take an alert with these labels, and why each one did or did not."""
    return incidents.explain_routing(payload["labels"], payload["integration_id"])


# --- escalation policies ---------------------------------------------------------------------

class PolicySchema(Schema):
    id = fields.Str()
    name = fields.Str()
    team_id = fields.Str(allow_none=True)
    steps = fields.List(fields.Dict())
    repeat = fields.Int()
    created_at = fields.DateTime()


class PolicyInSchema(_In):
    name = fields.Str(required=True, validate=validate.Length(min=1, max=200))
    team_id = fields.Str(load_default=None, allow_none=True)
    steps = fields.List(fields.Dict(), required=True)
    repeat = fields.Int(load_default=0, validate=validate.Range(min=0, max=5))


_TARGET_MODELS = {"user": (User, "user"), "team": (Team, "team"), "channel": (Channel, "channel"),
                  "schedule": (Schedule, "schedule")}


def _clean_steps(raw) -> list:
    if not raw:
        abort(422, message="a policy needs at least one step")
    if len(raw) > MAX_STEPS:
        abort(422, message=f"at most {MAX_STEPS} steps")
    out = []
    for i, step in enumerate(raw, 1):
        try:
            delay = float(step.get("delay_minutes", 0))
        except (TypeError, ValueError):
            abort(422, message=f"step #{i}: delay_minutes must be a number")
        if not 0 <= delay <= 1440:
            abort(422, message=f"step #{i}: delay_minutes must be between 0 and 1440")
        if i > 1 and delay == 0:
            abort(422, message=f"step #{i}: a later step needs a delay, or it is the same step")
        targets = step.get("targets") or []
        if not targets or len(targets) > MAX_TARGETS:
            abort(422, message=f"step #{i}: needs 1–{MAX_TARGETS} targets")
        clean = []
        for t in targets:
            typ, tid = (t or {}).get("type"), (t or {}).get("id")
            if typ not in _TARGET_MODELS:
                abort(422, message=f"step #{i}: target type must be user, team, schedule or channel")
            model, what = _TARGET_MODELS[typ]
            _check_ref(model, tid, what)
            clean.append({"type": typ, "id": tid})
        out.append({"delay_minutes": delay if i > 1 else 0, "targets": clean})
    return out


@blp.route("/escalation-policies")
@require("oncall:read")
@blp.response(200, PolicySchema(many=True))
def list_policies():
    return db.scalars(select(EscalationPolicy).order_by(EscalationPolicy.name)).all()


@blp.route("/escalation-policies", methods=["POST"])
@require("config:write")
@blp.arguments(PolicyInSchema)
@blp.response(201, PolicySchema)
def create_policy(payload):
    payload["steps"] = _clean_steps(payload["steps"])
    _check_ref(Team, payload.get("team_id"), "team")
    row = EscalationPolicy(**payload)
    db.add(row)
    _commit_or_conflict("a policy with that name already exists")
    record(g.identity.name, "policy.create", target=row.name)
    return row


@blp.route("/escalation-policies/<policy_id>", methods=["PUT"])
@require("config:write")
@blp.arguments(PolicyInSchema)
@blp.response(200, PolicySchema)
def update_policy(payload, policy_id):
    row = _get(EscalationPolicy, policy_id, "escalation policy")
    payload["steps"] = _clean_steps(payload["steps"])
    _check_ref(Team, payload.get("team_id"), "team")
    for k, v in payload.items():
        setattr(row, k, v)
    _commit_or_conflict("a policy with that name already exists")
    record(g.identity.name, "policy.update", target=row.name)
    return row


@blp.route("/escalation-policies/<policy_id>", methods=["DELETE"])
@require("config:write")
@blp.response(204)
def delete_policy(policy_id):
    row = _get(EscalationPolicy, policy_id, "escalation policy")
    in_use = db.scalars(select(Route.name).where(Route.escalation_policy_id == row.id)).all()
    if in_use:
        abort(409, message=f"used by route(s): {', '.join(in_use)} — change them first")
    db.delete(row)
    db.commit()
    record(g.identity.name, "policy.delete", target=row.name)


# --- channels ----------------------------------------------------------------------------------

MASK = "••••••"


class ChannelSchema(Schema):
    id = fields.Str()
    name = fields.Str()
    kind = fields.Str()
    team_id = fields.Str(allow_none=True)
    config = fields.Method("_config")
    has_secret = fields.Method("_has_secret")
    enabled = fields.Bool()
    created_at = fields.DateTime()

    def _has_secret(self, obj):
        return bool(obj.secret)

    def _config(self, obj):
        """Header VALUES are write-only (they carry API keys), and the URL — often the
        credential itself — is shown in full only to those who may edit the channel."""
        c = dict(obj.config or {})
        if c.get("headers"):
            c["headers"] = {k: MASK for k in c["headers"]}
        if c.get("api_token"):
            c["api_token"] = MASK   # a Jira token is write-only, like header values
        if "url" in c and not g.identity.can("config:write"):
            c["url"] = delivery.redact_url(c["url"])
        return c


class ChannelCreatedSchema(ChannelSchema):
    secret = fields.Str(allow_none=True)


class ChannelInSchema(_In):
    name = fields.Str(required=True, validate=validate.Length(min=1, max=200))
    kind = fields.Str(required=True, validate=validate.OneOf(["email", "webhook", "slack", "jira"]))
    team_id = fields.Str(load_default=None, allow_none=True)
    config = fields.Dict(required=True)
    enabled = fields.Bool(load_default=True)


def _clean_config(kind: str, config: dict, old: dict | None = None) -> dict:
    if kind == "jira":
        from .. import jira
        try:
            return jira.clean_config(config, old, MASK)
        except ValueError as e:
            abort(422, message=str(e))
    if kind == "slack":
        # The Slack app posts here (Settings → Slack). The id is what Slack addresses; the
        # name is for people reading the channel list and the delivery log.
        cid = str(config.get("channel_id") or "").strip()
        if not re.fullmatch(r"[CG][A-Z0-9]{4,30}", cid):
            abort(422, message="config.channel_id: a Slack channel id (C… or G…) — pick one from the list")
        name = str(config.get("channel_name") or "").strip().lstrip("#")[:80]
        return {"channel_id": cid, "channel_name": name}
    if kind == "email":
        to = config.get("to")
        if isinstance(to, str):
            to = [x.strip() for x in to.split(",") if x.strip()]
        if not to or not isinstance(to, list) or len(to) > 20:
            abort(422, message="config.to: 1–20 email addresses")
        bad = [a for a in to if not isinstance(a, str) or not EMAIL.match(a)]
        if bad:
            abort(422, message=f"config.to: not email addresses: {', '.join(map(str, bad))}")
        return {"to": [a.lower() for a in to]}

    # Webhook. Public destinations only, unless the host is in WEBHOOK_PRIVATE_HOSTS
    # (delivery.webhook_destination_error says why — it is re-checked before every send).
    url = str(config.get("url") or "").strip()
    if len(url) > 1000 or not delivery.webhook_url_ok(url):
        abort(422, message="config.url must be an http(s) URL")
    _unfilled("config.url", url)
    err = delivery.webhook_destination_error(url)
    if err:
        abort(422, message=f"config.url: {err}")
    out: dict = {"url": url}

    method = str(config.get("method") or "POST").upper()
    if method not in delivery.WEBHOOK_METHODS:
        abort(422, message=f"config.method: one of {', '.join(delivery.WEBHOOK_METHODS)}")
    out["method"] = method

    headers = config.get("headers") or {}
    if not isinstance(headers, dict) or len(headers) > 20:
        abort(422, message="config.headers: an object of up to 20 name: value pairs")
    old_headers = (old or {}).get("headers") or {}
    clean_h = {}
    for k, v in headers.items():
        name = str(k).strip()
        # OX Agent: Header Injection prevented — names are tokens, values have no CR/LF
        if not HEADER_NAME.match(name):
            abort(422, message=f"config.headers: '{name}' is not a valid header name")
        if name.lower() in delivery.RESERVED_HEADERS:
            abort(422, message=f"config.headers: '{name}' is set by Shorthop")
        value = str(v)
        if value == MASK:  # the masked value read back from GET = keep what is stored
            if name not in old_headers:
                abort(422, message=f"config.headers: '{name}' has no stored value to keep")
            value = old_headers[name]
        if "\r" in value or "\n" in value or len(value) > 2000:
            abort(422, message=f"config.headers: the value of '{name}' is not allowed")
        _unfilled(f"config.headers: '{name}'", value)
        clean_h[name] = value
    if clean_h:
        out["headers"] = clean_h

    template = config.get("body_template")
    if template:
        if not isinstance(template, str) or len(template) > 10000:
            abort(422, message="config.body_template: a string of up to 10000 characters")
        _unfilled("config.body_template", template)
        try:  # render against a sample so a broken template fails on save, not at 3 am
            # A bare {{name}} may be any label a label rule sets (e.g. {{level}}).
            known = {k for sl in db.scalars(select(LabelRule.set_labels)).all() for k in (sl or {})}
            delivery.render_template(template, {"event": "test", "channel": "sample"}, known)
        except delivery.DeliveryError as e:
            abort(422, message=f"config.body_template: {e}")
        out["body_template"] = template

    events = config.get("events")
    if events:
        if not isinstance(events, list) or any(e not in delivery.WEBHOOK_EVENTS for e in events):
            abort(422, message=f"config.events: a list of {', '.join(delivery.WEBHOOK_EVENTS)}")
        out["events"] = [e for e in delivery.WEBHOOK_EVENTS if e in events]
    return out


@blp.route("/channels")
@require("oncall:read")
@blp.response(200, ChannelSchema(many=True))
def list_channels():
    return db.scalars(select(Channel).order_by(Channel.name)).all()


@blp.route("/channels", methods=["POST"])
@require("config:write")
@blp.arguments(ChannelInSchema)
@blp.response(201, ChannelCreatedSchema)
def create_channel(payload):
    """A webhook channel gets a signing secret, shown ONCE: the receiver verifies
    X-Shorthop-Signature = sha256=HMAC(secret, "<X-Shorthop-Timestamp>.<body>")."""
    payload["config"] = _clean_config(payload["kind"], payload["config"])
    _check_ref(Team, payload.get("team_id"), "team")
    row = Channel(**payload)
    if row.kind == "webhook":
        row.secret = "shs_" + _secrets.token_urlsafe(32)
    db.add(row)
    _commit_or_conflict("a channel with that name already exists")
    record(g.identity.name, "channel.create", target=row.name, detail=row.kind)
    return row


@blp.route("/channels/<channel_id>", methods=["PUT"])
@require("config:write")
@blp.arguments(ChannelInSchema)
@blp.response(200, ChannelSchema)
def update_channel(payload, channel_id):
    row = _get(Channel, channel_id, "channel")
    if payload["kind"] != row.kind:
        abort(422, message="a channel's kind cannot change — create a new one")
    payload["config"] = _clean_config(row.kind, payload["config"], row.config)
    _check_ref(Team, payload.get("team_id"), "team")
    for k, v in payload.items():
        setattr(row, k, v)
    _commit_or_conflict("a channel with that name already exists")
    record(g.identity.name, "channel.update", target=row.name)
    return row


@blp.route("/channels/<channel_id>", methods=["DELETE"])
@require("config:write")
@blp.response(204)
def delete_channel(channel_id):
    row = _get(Channel, channel_id, "channel")
    used = [p.name for p in db.scalars(select(EscalationPolicy)).all()
            if any(t.get("type") == "channel" and t.get("id") == row.id
                   for s in p.steps or [] for t in s.get("targets", []))]
    if used:
        abort(409, message=f"used by escalation policy(ies): {', '.join(used)}")
    db.delete(row)
    db.commit()
    record(g.identity.name, "channel.delete", target=row.name)


@blp.route("/channels/<channel_id>/rotate-secret", methods=["POST"])
@require("config:write")
@blp.response(200, ChannelCreatedSchema)
def rotate_channel_secret(channel_id):
    row = _get(Channel, channel_id, "channel")
    if row.kind != "webhook":
        abort(422, message="only webhook channels have a signing secret")
    row.secret = "shs_" + _secrets.token_urlsafe(32)
    db.commit()
    record(g.identity.name, "channel.rotate", target=row.name)
    return row


@blp.route("/channels/<channel_id>/test", methods=["POST"])
@require("config:write")
def test_channel(channel_id):
    """Send a test message now and report what happened — the answer to "will this reach
    anybody?" before an incident asks it."""
    row = _get(Channel, channel_id, "channel")
    try:
        if row.kind == "slack":
            from .. import slack
            try:
                slack.send_test(row, g.identity.name)
            except slack.SlackError as e:
                raise delivery.DeliveryError(f"Slack: {e}") from None
        elif row.kind == "jira":
            from .. import jira
            try:
                detail = jira.send_test(row)
            except jira.JiraError as e:
                raise delivery.DeliveryError(f"Jira: {e}") from None
            record(g.identity.name, "channel.test", target=row.name)
            return {"ok": True, "error": None, "detail": detail}
        elif row.kind == "email":
            for addr in (row.config or {}).get("to", []):
                delivery.send_email(addr, "[TEST] Shorthop channel " + row.name,
                                    f"A test from Shorthop for channel '{row.name}'.\n"
                                    f"Sent by {g.identity.name}.")
        else:
            delivery.send_webhook(row.config or {},
                                  {"version": "1", "event": "test", "channel": row.name,
                                   "sent_by": g.identity.name}, row.secret)
    except delivery.DeliveryError as e:
        record(g.identity.name, "channel.test", target=row.name, status="error", detail=str(e))
        return {"ok": False, "error": str(e)}
    record(g.identity.name, "channel.test", target=row.name)
    return {"ok": True, "error": None}


# --- inhibitions ---------------------------------------------------------------------------------

class InhibitionSchema(Schema):
    id = fields.Str()
    name = fields.Str()
    source_matchers = fields.List(fields.Dict())
    target_matchers = fields.List(fields.Dict())
    equal = fields.List(fields.Str())
    enabled = fields.Bool()
    created_at = fields.DateTime()


class InhibitionInSchema(_In):
    name = fields.Str(required=True, validate=validate.Length(min=1, max=200))
    source_matchers = fields.List(fields.Dict(), required=True)
    target_matchers = fields.List(fields.Dict(), required=True)
    equal = fields.List(fields.Str(), load_default=list)
    enabled = fields.Bool(load_default=True)


def _inhibition_fields(payload):
    payload["source_matchers"] = _clean_matchers(payload["source_matchers"], allow_all=False)
    payload["target_matchers"] = _clean_matchers(payload["target_matchers"], allow_all=False)
    bad = [x for x in payload["equal"] if not LABEL_NAME.match(x)]
    if bad:
        abort(422, message=f"equal: not label names: {', '.join(bad)}")
    return payload


@blp.route("/inhibitions")
@require("oncall:read")
@blp.response(200, InhibitionSchema(many=True))
def list_inhibitions():
    return db.scalars(select(Inhibition).order_by(Inhibition.name)).all()


@blp.route("/inhibitions", methods=["POST"])
@require("config:write")
@blp.arguments(InhibitionInSchema)
@blp.response(201, InhibitionSchema)
def create_inhibition(payload):
    row = Inhibition(**_inhibition_fields(payload))
    db.add(row)
    db.commit()
    record(g.identity.name, "inhibition.create", target=row.name)
    return row


@blp.route("/inhibitions/<inhibition_id>", methods=["PUT"])
@require("config:write")
@blp.arguments(InhibitionInSchema)
@blp.response(200, InhibitionSchema)
def update_inhibition(payload, inhibition_id):
    row = _get(Inhibition, inhibition_id, "inhibition")
    for k, v in _inhibition_fields(payload).items():
        setattr(row, k, v)
    db.commit()
    record(g.identity.name, "inhibition.update", target=row.name)
    return row


@blp.route("/inhibitions/<inhibition_id>", methods=["DELETE"])
@require("config:write")
@blp.response(204)
def delete_inhibition(inhibition_id):
    row = _get(Inhibition, inhibition_id, "inhibition")
    db.delete(row)
    db.commit()
    record(g.identity.name, "inhibition.delete", target=row.name)


# --- incidents & notifications ---------------------------------------------------------------------

class NotificationSchema(Schema):
    id = fields.Str()
    incident_id = fields.Str()
    event = fields.Str()
    step = fields.Int(allow_none=True)
    kind = fields.Str()
    target = fields.Str()
    destination = fields.Str()
    status = fields.Str()
    attempts = fields.Int()
    error = fields.Str(allow_none=True)
    created_at = fields.DateTime()
    sent_at = fields.DateTime(allow_none=True)


class IncidentSchema(Schema):
    id = fields.Str()
    number = fields.Int()
    title = fields.Str()
    severity = fields.Str()
    status = fields.Str()
    route_id = fields.Str(allow_none=True)
    team_id = fields.Str(allow_none=True)
    escalation_policy_id = fields.Str(allow_none=True)
    group_labels = fields.Dict()
    opened_at = fields.DateTime()
    acked_at = fields.DateTime(allow_none=True)
    acked_by = fields.Str(allow_none=True)
    resolved_at = fields.DateTime(allow_none=True)
    resolved_by = fields.Str(allow_none=True)
    next_step = fields.Int()
    next_step_at = fields.DateTime(allow_none=True)
    reassigned_at = fields.DateTime(allow_none=True)
    reassigned_by = fields.Str(allow_none=True)
    reassign_note = fields.Str(allow_none=True)
    level = fields.Str(allow_none=True)
    level_manual = fields.Bool()
    level_set_by = fields.Str(allow_none=True)
    level_set_at = fields.DateTime(allow_none=True)
    # Set on list rows (one bulk lookup) and on the detail view.
    team_name = fields.Str(allow_none=True)
    open_alerts = fields.Method("_open")
    alert_count = fields.Method("_count")

    def _open(self, obj):
        return sum(1 for o in obj.alerts if o.resolved_at is None)

    def _count(self, obj):
        return len(obj.alerts)


class JiraIssueSchema(Schema):
    issue_key = fields.Str()
    issue_url = fields.Str()
    closed_at = fields.DateTime(allow_none=True)
    pending = fields.Method("_pending")
    error = fields.Str(allow_none=True)

    def _pending(self, obj):
        return len(obj.pending_comments or []) + (1 if obj.close_pending else 0)


class IncidentDetailSchema(IncidentSchema):
    alerts = fields.List(fields.Nested(OccurrenceSchema))
    notifications = fields.List(fields.Nested(NotificationSchema))
    jira_issues = fields.List(fields.Nested(JiraIssueSchema))
    route_name = fields.Str(allow_none=True)
    policy_name = fields.Str(allow_none=True)


class IncidentListArgs(_In):
    status = fields.Str(load_default="open", validate=validate.OneOf(
        ["open", "triggered", "acknowledged", "resolved", "all"]))
    # "all" (default), "mine" = the caller's teams plus incidents no team owns, or a team id.
    # Ownership moves on reassign, so "mine" is what makes a handed-off incident leave the
    # list of the team that handed it off.
    team = fields.Str(load_default="all", validate=validate.Length(min=1, max=64))
    limit = fields.Int(load_default=100, validate=validate.Range(min=1, max=500))


class IncidentResolveSchema(_In):
    category = fields.Str(load_default=None, validate=validate.OneOf(RESOLUTION_CATEGORIES))
    note = fields.Str(load_default=None, validate=validate.Length(max=4000))


def _incident_detail(inc: Incident) -> Incident:
    silences = alert_ops.active_silences()
    for o in inc.alerts:
        o.silenced_by = alert_ops.silenced_by(o, silences)
    inc.notifications = db.scalars(select(Notification).where(
        Notification.incident_id == inc.id).order_by(Notification.created_at.desc())).all()
    inc.jira_issues = db.scalars(select(JiraIssue).where(
        JiraIssue.incident_id == inc.id).order_by(JiraIssue.created_at)).all()
    route = db.get(Route, inc.route_id) if inc.route_id else None
    policy = db.get(EscalationPolicy, inc.escalation_policy_id) if inc.escalation_policy_id else None
    team = db.get(Team, inc.team_id) if inc.team_id else None
    inc.route_name = route.name if route else None
    inc.policy_name = policy.name if policy else None
    inc.team_name = team.name if team else None
    return inc


def _act(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except LookupError as e:
        abort(404, message=str(e))
    except alert_ops.AlertError as e:
        abort(409, message=str(e))


@blp.route("/incidents")
@require("alerts:read")
@blp.arguments(IncidentListArgs, location="query")
@blp.response(200, IncidentSchema(many=True))
def list_incidents(args):
    stmt = select(Incident)
    st = args["status"]
    if st == "open":
        stmt = stmt.where(Incident.resolved_at.is_(None))
    elif st != "all":
        stmt = stmt.where(Incident.status == st)
    team = args["team"]
    if team == "mine":
        mine = select(TeamMember.team_id).where(TeamMember.user_id == g.identity.user_id)
        # Unowned incidents page nobody's team, so everyone has to be able to see them.
        stmt = stmt.where(or_(Incident.team_id.in_(mine), Incident.team_id.is_(None)))
    elif team != "all":
        stmt = stmt.where(Incident.team_id == team)
    order = Incident.resolved_at.desc() if st == "resolved" else Incident.opened_at.desc()
    rows = db.scalars(stmt.order_by(order).limit(args["limit"])).all()
    tids = {i.team_id for i in rows if i.team_id}
    names = dict(db.execute(select(Team.id, Team.name).where(Team.id.in_(tids))).all()) if tids else {}
    for i in rows:
        i.team_name = names.get(i.team_id)
    return rows


@blp.route("/incidents/<incident_id>")
@require("alerts:read")
@blp.response(200, IncidentDetailSchema)
def get_incident(incident_id):
    return _incident_detail(_act(incidents.get, incident_id))


@blp.route("/incidents/<incident_id>/ack", methods=["POST"])
@require("alerts:write")
@blp.response(200, IncidentDetailSchema)
def ack_incident(incident_id):
    """Stops escalation and acknowledges every open alert in the incident."""
    inc = _act(incidents.acknowledge, incident_id, g.identity.name)
    record(g.identity.name, "incident.ack", target=incident_id)
    return _incident_detail(inc)


@blp.route("/incidents/<incident_id>/resolve", methods=["POST"])
@require("alerts:write")
@blp.arguments(IncidentResolveSchema)
@blp.response(200, IncidentDetailSchema)
def resolve_incident(payload, incident_id):
    """Resolve every open alert in it by hand, with one note saying what fixed it."""
    inc = _act(incidents.resolve, incident_id, g.identity.name,
               category=payload.get("category"), note=payload.get("note"))
    record(g.identity.name, "incident.resolve", target=incident_id)
    return _incident_detail(inc)


class IncidentReassignSchema(_In):
    team_id = fields.Str(required=True)
    # None = the team's own policy, when it has exactly one.
    escalation_policy_id = fields.Str(load_default=None, allow_none=True)
    note = fields.Str(load_default=None, allow_none=True, validate=validate.Length(max=2000))
    # Acknowledged incidents only: page the new team (default) or just move ownership.
    page = fields.Bool(load_default=True)


@blp.route("/incidents/<incident_id>/reassign", methods=["POST"])
@require("alerts:write")
@blp.arguments(IncidentReassignSchema)
@blp.response(200, IncidentDetailSchema)
def reassign_incident(payload, incident_id):
    """Move the incident to another team: it takes the team's escalation policy, the new team
    is paged from step 1, and pages still queued for the old team are cancelled."""
    inc = _act(incidents.reassign, incident_id, g.identity.name, payload["team_id"],
               payload.get("escalation_policy_id"), note=payload.get("note"),
               page=payload["page"])
    record(g.identity.name, "incident.reassign", target=incident_id,
           detail=inc.reassign_note or "")
    return _incident_detail(inc)


class IncidentCreateSchema(_In):
    team_id = fields.Str(required=True)
    message = fields.Str(required=True, validate=validate.Length(min=1, max=2000))
    level = fields.Str(load_default=None, allow_none=True, validate=validate.Length(max=20))
    escalation_policy_id = fields.Str(load_default=None, allow_none=True)


@blp.route("/incidents", methods=["POST"])
@require("alerts:write")
@blp.arguments(IncidentCreateSchema)
@blp.response(201, IncidentDetailSchema)
def create_incident(payload):
    """Ask a team for help: a new incident owned by the team, paged now by its escalation
    policy (its own one when it has exactly one). Level defaults to the most urgent."""
    inc = _act(incidents.page_team, payload["team_id"], g.identity.name, payload["message"],
               level=payload.get("level"), policy_id=payload.get("escalation_policy_id"))
    record(g.identity.name, "incident.ping", target=inc.id, detail=payload["message"][:200])
    return _incident_detail(inc)


class IncidentLevelSchema(_In):
    level = fields.Str(required=True, validate=validate.Length(min=1, max=20))


@blp.route("/incidents/<incident_id>/level", methods=["PUT"])
@require("alerts:write")
@blp.arguments(IncidentLevelSchema)
@blp.response(200, IncidentDetailSchema)
def set_incident_level(payload, incident_id):
    """Set the level (P1 …). The severity follows the level's mapping and both stop following
    the alerts. Applies to every page from here on — Escalate now pages the next step at once."""
    inc = _act(incidents.set_level, incident_id, g.identity.name, payload["level"])
    record(g.identity.name, "incident.level", target=incident_id, detail=inc.level or "")
    return _incident_detail(inc)


@blp.route("/incidents/<incident_id>/escalate", methods=["POST"])
@require("alerts:write")
@blp.response(200, IncidentDetailSchema)
def escalate_incident(incident_id):
    """Run the next escalation step now instead of after its delay (paging incidents only)."""
    inc = _act(incidents.escalate_now, incident_id, g.identity.name)
    record(g.identity.name, "incident.escalate", target=incident_id)
    return _incident_detail(inc)


class IncidentResponderSchema(_In):
    user_id = fields.Str(load_default=None, allow_none=True)
    schedule_id = fields.Str(load_default=None, allow_none=True)
    note = fields.Str(load_default=None, allow_none=True, validate=validate.Length(max=2000))


@blp.route("/incidents/<incident_id>/responders", methods=["POST"])
@require("alerts:write")
@blp.arguments(IncidentResponderSchema)
@blp.response(200, IncidentDetailSchema)
def add_incident_responder(payload, incident_id):
    """Page one more person — or whoever is on call for a schedule — now, by all their contact
    rules at once, outside the escalation policy. Works on an acknowledged incident too."""
    paged = _act(incidents.add_responder, incident_id, g.identity.name,
                 user_id=payload.get("user_id"), schedule_id=payload.get("schedule_id"),
                 note=payload.get("note"))
    record(g.identity.name, "incident.responder", target=incident_id,
           detail=", ".join(d["target"] for d in paged)[:500])
    return _incident_detail(incidents.get(incident_id))


class IncidentNoteSchema(_In):
    text = fields.Str(required=True, validate=validate.Length(min=1, max=4000))
    # "suggestion" = written by an automation (an AI triage agent), shown apart from people's notes.
    kind = fields.Str(load_default="note", validate=validate.OneOf(["note", "suggestion"]))


@blp.route("/incidents/<incident_id>/notes", methods=["POST"])
@require("alerts:write")
@blp.arguments(IncidentNoteSchema)
@blp.response(200, IncidentDetailSchema)
def add_incident_note(payload, incident_id):
    """A note on every open alert's timeline (and the incident's Slack threads)."""
    inc = _act(incidents.add_note, incident_id, g.identity.name, payload["text"],
               kind=payload["kind"])
    record(g.identity.name, f"incident.{payload['kind']}", target=incident_id)
    return _incident_detail(inc)


class IncidentSnoozeSchema(_In):
    minutes = fields.Int(required=True, validate=validate.Range(min=1, max=1440))
    comment = fields.Str(load_default=None, allow_none=True, validate=validate.Length(max=2000))


@blp.route("/incidents/<incident_id>/snooze", methods=["POST"])
@require("alerts:write")
@blp.arguments(IncidentSnoozeSchema)
def snooze_incident(payload, incident_id):
    """Silence this incident's alerts (its group labels) for up to 24 h. Escalation holds and
    resumes when the silence ends; the silence is on the Silences page like any other."""
    s = _act(incidents.snooze, incident_id, g.identity.name, payload["minutes"],
             comment=payload.get("comment"))
    record(g.identity.name, "incident.snooze", target=incident_id, detail=f"{payload['minutes']} min")
    return {"silence_id": s.id, "ends_at": s.ends_at.isoformat(), "matchers": s.matchers}


@blp.route("/levels")
@require("alerts:read")
def list_levels():
    """The incident levels in force, most urgent first (admin Settings → Incident levels)."""
    from .. import settings as st
    return {"levels": st.incident_levels()}


class NotificationArgs(_In):
    status = fields.Str(load_default=None, validate=validate.OneOf(
        ["pending", "sent", "failed", "suppressed"]))
    limit = fields.Int(load_default=200, validate=validate.Range(min=1, max=1000))


@blp.route("/notifications")
@require("alerts:read")
@blp.arguments(NotificationArgs, location="query")
@blp.response(200, NotificationSchema(many=True))
def list_notifications(args):
    """The delivery log: every page attempted, where, and whether it arrived."""
    stmt = select(Notification).order_by(Notification.created_at.desc()).limit(args["limit"])
    if args["status"]:
        stmt = stmt.where(Notification.status == args["status"])
    return db.scalars(stmt).all()
