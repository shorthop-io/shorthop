"""MCP server — Shorthop's alerts, answerable and actionable from Claude (or any MCP client).

    claude mcp add --transport http shorthop https://oncall.internal.example.com/mcp \\
        --header "Authorization: Bearer sh_…"

Same transport shape as dbdeck's (`app/mcp.py` there): `POST /mcp`, Streamable HTTP in its
simplest legal form — one JSON response per request, no server stream, no session — on the
same waitress/Flask stack as every other route.

Decisions that carry the weight:

⚠️ **Every tool calls the same function the REST API calls and checks the same scope**, so
MCP can never do more than the token behind it. Read tools need `alerts:read`; acting on
an alert (ack, resolve, note, silence) needs `alerts:write`.

⚠️ **A token holding `config:write` or `admin` is refused outright.** An MCP client keeps its
bearer token in a plaintext file on a laptop (`~/.claude.json`); that file must never be a
credential that can rewire routing, mint tokens or manage users. Mint a token with only
`alerts:read` (and `alerts:write` if the assistant may act).

⚠️ **Nothing here configures Shorthop** — no integrations, routes, users or teams — and
nothing deletes history.

⚠️ **Silences made through MCP are capped** at MCP_MAX_SILENCE_HOURS (24 h), tighter than the
UI's, and always carry the comment the model was asked to write.

⚠️ **Every tool call is audited** as `mcp.<tool>` against the token's owner.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlparse

from flask import Blueprint, Response, request
from prometheus_client import Counter
from sqlalchemy import select

from . import alerts as alert_ops
from . import config as cfg
from . import insight
from .api.schemas import (
    HistorySchema, IntegrationSchema, OccurrenceDetailSchema, OccurrenceSchema, SilenceSchema,
    TeamSchema,
)
from .audit import record
from .auth import token_identity
from .db import SessionLocal
from .models import Integration, Silence, Team, aware, now

logger = logging.getLogger(__name__)

bp = Blueprint("mcp", __name__)

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
FORBIDDEN_SCOPES = frozenset({"config:write", "admin"})
MAX_RESULT_CHARS = int(os.getenv("MCP_MAX_RESULT_CHARS", "200000"))

MCP_CALLS = Counter("mcp_tool_calls_total", "MCP tool calls, by tool and outcome.",
                    ["tool", "outcome"])

INSTRUCTIONS = (
    "Shorthop is the alerting and on-call system: it receives alerts from Prometheus and "
    "webhooks, tracks each firing episode (an 'alert' here is one occurrence), groups alerts "
    "into INCIDENTS (what pages people, via routes and escalation policies), and keeps the "
    "history of every alert by its SIGNATURE — the labels that name the problem, ignoring "
    "which pod or instance it was on. Start with list_alerts. Before suggesting a fix, call "
    "get_alert: its `history` says how often this alert fired before and what fixed it "
    "(resolution_category + resolution_note). When you resolve an alert, always pass a "
    "category and a short note saying what fixed it — that note is what the next person "
    "paged for this alert will read. Silences need a comment saying why."
)


class ToolError(Exception):
    """Caller-safe refusal; the message is the tool result."""


@dataclass
class Tool:
    name: str
    title: str
    description: str
    scope: str
    handler: Callable[[dict, Any], Any]
    params: dict[str, dict] = field(default_factory=dict)
    required: tuple[str, ...] = ()

    @property
    def writes(self) -> bool:
        return self.scope.endswith(":write")

    def definition(self) -> dict:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": {"type": "object", "properties": self.params,
                            "required": list(self.required), "additionalProperties": False},
            "annotations": {"title": self.title, "readOnlyHint": not self.writes,
                            "destructiveHint": False, "idempotentHint": not self.writes,
                            "openWorldHint": False},
        }


TOOLS: dict[str, Tool] = {}


def tool(name, title, description, scope, params=None, required=()):
    def deco(fn):
        TOOLS[name] = Tool(name, title, description, scope, fn, params or {}, tuple(required))
        return fn
    return deco


def _str(desc, **kw):
    return {"type": "string", "description": desc, **kw}


def _int(desc, minimum=None, maximum=None, default=None):
    s = {"type": "integer", "description": desc}
    for k, v in (("minimum", minimum), ("maximum", maximum), ("default", default)):
        if v is not None:
            s[k] = v
    return s


MATCHERS = {
    "type": "array",
    "description": "Alertmanager-style matchers, all must match: "
                   '[{"name":"alertname","value":"X","isRegex":false,"isEqual":true}]',
    "items": {"type": "object"},
}


def _validate(t: Tool, args: Any) -> dict:
    """Against the tool's own declared schema. An unknown argument is refused, not ignored:
    a misspelt filter must not silently return the unfiltered answer."""
    args = args or {}
    if not isinstance(args, dict):
        raise ToolError("arguments must be an object")
    unknown = sorted(set(args) - set(t.params))
    if unknown:
        raise ToolError(f"unknown argument(s): {', '.join(unknown)}. "
                        f"Accepted: {', '.join(t.params) or 'none'}")
    out = {}
    for key, spec in t.params.items():
        if key not in args or args[key] is None:
            if key in t.required:
                raise ToolError(f"missing required argument '{key}'")
            if "default" in spec:
                out[key] = spec["default"]
            continue
        v, typ = args[key], spec["type"]
        if typ == "integer" and (not isinstance(v, int) or isinstance(v, bool)):
            raise ToolError(f"'{key}' must be an integer")
        if typ == "array" and not isinstance(v, list):
            raise ToolError(f"'{key}' must be an array")
        if typ == "object" and not isinstance(v, dict):
            raise ToolError(f"'{key}' must be an object")
        if typ == "string":
            if not isinstance(v, str):
                raise ToolError(f"'{key}' must be a string")
            v = v.strip()
            if len(v) > 4000:
                raise ToolError(f"'{key}' is too long")
            if "enum" in spec and v not in spec["enum"]:
                raise ToolError(f"'{key}' must be one of: {', '.join(spec['enum'])}")
            if key in t.required and not v:
                raise ToolError(f"'{key}' must not be empty")
        if typ == "integer":
            if "minimum" in spec and v < spec["minimum"]:
                raise ToolError(f"'{key}' must be at least {spec['minimum']}")
            if "maximum" in spec and v > spec["maximum"]:
                raise ToolError(f"'{key}' must be at most {spec['maximum']}")
        out[key] = v
    return out


def _call(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except LookupError as e:
        raise ToolError(str(e)) from None
    except alert_ops.AlertError as e:
        raise ToolError(str(e)) from None


def _silenced(rows):
    silences = alert_ops.active_silences()
    for r in rows:
        r.silenced_by = alert_ops.silenced_by(r, silences)
    return rows


def _detail(occ):
    _silenced([occ])
    occ.history = alert_ops.history(occ.signature, exclude_id=occ.id)
    out = OccurrenceDetailSchema().dump(occ)
    if cfg.PUBLIC_URL:
        out["url"] = f"{cfg.PUBLIC_URL}/alerts/{occ.id}"
    return out


# --- read tools ----------------------------------------------------------------------

@tool("list_alerts", "List alerts",
      "Alerts newest first. status: open (default: firing + acknowledged), firing, "
      "acknowledged, resolved, all. label filters are key=value strings, all must match.",
      "alerts:read",
      {"status": _str("Which alerts", enum=["open", "firing", "acknowledged", "resolved",
                                            "all"], default="open"),
       "label": {"type": "array", "items": {"type": "string"},
                 "description": "key=value label filters, e.g. [\"namespace=service\"]"},
       "q": _str("Substring of the alert name"),
       "limit": _int("Max rows", 1, 200, 50)})
def _list_alerts(args, ident):
    filters = {}
    for item in args.get("label") or []:
        if not isinstance(item, str) or "=" not in item:
            raise ToolError("each label filter must be a key=value string")
        k, v = item.split("=", 1)
        filters[k.strip()] = v
    rows = alert_ops.list_occurrences(status=args["status"], label_filters=filters,
                                      q=args.get("q") or "", limit=args["limit"])
    return OccurrenceSchema(many=True).dump(_silenced(rows))


@tool("get_alert", "Alert detail with history",
      "One alert: labels, annotations (summary, runbook_url), timeline, and `history` — how "
      "many times this alert's signature fired before and what fixed it. Read this before "
      "proposing a fix.",
      "alerts:read", {"alert_id": _str("Alert (occurrence) id")}, ("alert_id",))
def _get_alert(args, ident):
    return _detail(_call(alert_ops.get, args["alert_id"]))


@tool("alert_history", "Past occurrences of an alert",
      "Every earlier occurrence of an alert's signature, newest first, with how each was "
      "resolved and any note saying what fixed it. Pass an alert_id or a signature.",
      "alerts:read",
      {"alert_id": _str("Any occurrence of the alert"),
       "signature": _str("The signature hash"),
       "limit": _int("Max rows", 1, 100, 20)})
def _alert_history(args, ident):
    sig = args.get("signature")
    if not sig:
        if not args.get("alert_id"):
            raise ToolError("pass alert_id or signature")
        sig = _call(alert_ops.get, args["alert_id"]).signature
    return HistorySchema().dump(alert_ops.history(sig, limit=args["limit"]))


@tool("noise_report", "Noisy alerts and response times",
      "What the history says over the last N days: the noisiest alerts with flags "
      "(`flapping` = re-fires within 15 min of resolving, `nobody_acts` = almost always ends "
      "by itself unacknowledged, `short_lived` = median under 5 min), MTTA/MTTR percentiles "
      "overall and per team, and alerts that fire together. Use it to suggest which alerts "
      "to tune, group or turn into tickets.",
      "alerts:read", {"days": _int("Window in days", 1, 90, 30),
                      "limit": _int("Max noisy alerts", 1, 50, 15)})
def _noise_report(args, ident):
    r = insight.report(args["days"])
    r["noise"] = r["noise"][:args["limit"]]
    return r


@tool("related_alerts", "Alerts that fire together with this one",
      "Alerts whose episodes start within 5 minutes of this alert's, most consistent first — "
      "the likely shared cause or its symptoms. Pass an alert_id or a signature.",
      "alerts:read",
      {"alert_id": _str("Any occurrence of the alert"),
       "signature": _str("The signature hash"),
       "days": _int("Window in days", 1, 90, 30)})
def _related_alerts(args, ident):
    sig = args.get("signature")
    if not sig:
        if not args.get("alert_id"):
            raise ToolError("pass alert_id or signature")
        sig = _call(alert_ops.get, args["alert_id"]).signature
    return insight.related(sig, args["days"])


@tool("list_silences", "Current silences",
      "Silences that are active or scheduled, with who created them and why.", "alerts:read")
def _list_silences(args, ident):
    t = now()
    rows = SessionLocal.scalars(select(Silence).where(Silence.ends_at > t)
                                .order_by(Silence.ends_at)).all()
    for s in rows:
        s.state = "pending" if aware(s.starts_at) > t else "active"
    return SilenceSchema(many=True).dump(rows)


@tool("preview_silence", "Which alerts a silence would mute",
      "The open alerts these matchers match right now. Call before create_silence.",
      "alerts:read", {"matchers": MATCHERS}, ("matchers",))
def _preview(args, ident):
    return OccurrenceSchema(many=True).dump(_call(alert_ops.preview_silence, args["matchers"]))


@tool("list_integrations", "Inbound integrations and their health",
      "Each sender (Prometheus, Alertmanager, webhook) with when it last delivered and when "
      "its Watchdog heartbeat was last seen. A stale one means alerts may not be arriving.",
      "alerts:read")
def _integrations(args, ident):
    rows = SessionLocal.scalars(select(Integration).where(Integration.archived_at.is_(None))
                                .order_by(Integration.name)).all()
    return IntegrationSchema(many=True, exclude=("signature_ignore",)).dump(rows)


@tool("list_teams", "Teams and members", "Teams with their members and roles.", "oncall:read")
def _teams(args, ident):
    rows = SessionLocal.scalars(select(Team).order_by(Team.name)).all()
    return TeamSchema(many=True).dump(rows)


@tool("list_incidents", "List incidents",
      "Incidents (pages): alerts grouped by route and group labels. status: open (default), "
      "triggered, acknowledged, resolved, all.", "alerts:read",
      {"status": _str("Which incidents", enum=["open", "triggered", "acknowledged", "resolved",
                                               "all"], default="open"),
       "limit": _int("Max rows", 1, 200, 50)})
def _list_incidents(args, ident):
    from sqlalchemy import select as _select

    from .api.paging import IncidentSchema
    from .models import Incident

    stmt = _select(Incident)
    if args["status"] == "open":
        stmt = stmt.where(Incident.resolved_at.is_(None))
    elif args["status"] != "all":
        stmt = stmt.where(Incident.status == args["status"])
    rows = SessionLocal.scalars(stmt.order_by(Incident.opened_at.desc()).limit(args["limit"])).all()
    return IncidentSchema(many=True).dump(rows)


@tool("get_incident", "Incident detail",
      "One incident with its alerts and every notification sent for it (who was paged, when, "
      "and whether it arrived).", "alerts:read",
      {"incident_id": _str("Incident id")}, ("incident_id",))
def _get_incident(args, ident):
    from . import incidents
    from .api.paging import IncidentDetailSchema, _incident_detail

    return IncidentDetailSchema().dump(_incident_detail(_call(incidents.get, args["incident_id"])))


@tool("test_routing", "Which route would take an alert",
      "Given labels, which route matches first and why every route did or did not match.",
      "oncall:read",
      {"labels": {"type": "object", "description": "label name → value",
                  "additionalProperties": {"type": "string"}}},
      ("labels",))
def _test_routing(args, ident):
    from . import incidents

    labels = args["labels"]
    if not isinstance(labels, dict) or not all(isinstance(v, str) for v in labels.values()):
        raise ToolError("labels must be an object of strings")
    return incidents.explain_routing(labels, "")


@tool("who_is_on_call", "Who is on call now",
      "Every on-call schedule: who is on call right now, whether by rotation or override, and "
      "until when.", "oncall:read")
def _who(args, ident):
    return _oncall_now()


def _oncall_now():
    from sqlalchemy import select as _select

    from . import schedules as svc
    from .api.schedules import _user_ref
    from .models import Schedule

    out = []
    for sch in SessionLocal.scalars(_select(Schedule).order_by(Schedule.name)).all():
        who = svc.on_call_now(sch)
        until = svc.next_change(sch, now())
        out.append({"schedule_id": sch.id, "schedule": sch.name, "user": _user_ref(who["user_id"]),
                    "source": who["source"], "until": until.isoformat() if until else None})
    return out


@tool("get_schedule", "An on-call schedule's upcoming shifts",
      "Who is on call when, for the next `days` days (default 7), in the final resolved form.",
      "oncall:read",
      {"schedule_id": _str("Schedule id (see who_is_on_call)"), "days": _int("How far ahead", 1, 31, 7)},
      ("schedule_id",))
def _get_schedule(args, ident):
    from . import schedules as svc
    from .api.schedules import _seg_out
    from .models import Schedule

    sch = SessionLocal.get(Schedule, args["schedule_id"])
    if sch is None:
        raise ToolError("schedule not found")
    t = now()
    return {"schedule": sch.name, "timezone": sch.timezone,
            "shifts": [_seg_out(x) for x in svc.shifts(sch, t, t + dt.timedelta(days=args["days"]))]}


# --- write tools ---------------------------------------------------------------------

@tool("acknowledge_alert", "Acknowledge an alert",
      "Mark an alert as being handled, by the token's owner.", "alerts:write",
      {"alert_id": _str("Alert (occurrence) id")}, ("alert_id",))
def _ack(args, ident):
    return _detail(_call(alert_ops.acknowledge, args["alert_id"], ident.name))


@tool("resolve_alert", "Resolve an alert and record the fix",
      "Resolve by hand (or, if the sender already resolved it, just record the fix). Always "
      "say what fixed it: the next person paged for this alert reads it.", "alerts:write",
      {"alert_id": _str("Alert (occurrence) id"),
       "category": _str("What kind of fix", enum=list(alert_ops.RESOLUTION_CATEGORIES)),
       "note": _str("What fixed it, in a sentence or two")},
      ("alert_id", "category", "note"))
def _resolve(args, ident):
    return _detail(_call(alert_ops.resolve, args["alert_id"], ident.name,
                         category=args["category"], note=args["note"]))


@tool("add_note", "Add a note to an alert's timeline",
      "Findings, links, what was tried.", "alerts:write",
      {"alert_id": _str("Alert (occurrence) id"), "text": _str("The note")},
      ("alert_id", "text"))
def _note(args, ident):
    return _detail(_call(alert_ops.add_note, args["alert_id"], ident.name, args["text"]))


@tool("create_silence", "Silence alerts for a while",
      f"Mute matching alerts for duration_minutes (at most {cfg.MCP_MAX_SILENCE_HOURS} h "
      "through MCP). A comment saying why is required. Use preview_silence first.",
      "alerts:write",
      {"matchers": MATCHERS,
       "duration_minutes": _int("How long", 1, cfg.MCP_MAX_SILENCE_HOURS * 60),
       "comment": _str("Why — the next person needs to know")},
      ("matchers", "duration_minutes", "comment"))
def _silence(args, ident):
    t = now()
    s = _call(alert_ops.create_silence, args["matchers"], t,
              t + dt.timedelta(minutes=args["duration_minutes"]), args["comment"], ident.name,
              max_duration=dt.timedelta(hours=cfg.MCP_MAX_SILENCE_HOURS))
    s.state = "active"
    return SilenceSchema().dump(s)


@tool("acknowledge_incident", "Acknowledge an incident",
      "Stop escalation for an incident and acknowledge all its open alerts.", "alerts:write",
      {"incident_id": _str("Incident id")}, ("incident_id",))
def _ack_incident(args, ident):
    from . import incidents
    from .api.paging import IncidentDetailSchema, _incident_detail

    return IncidentDetailSchema().dump(_incident_detail(
        _call(incidents.acknowledge, args["incident_id"], ident.name)))


@tool("resolve_incident", "Resolve an incident and record the fix",
      "Resolve every open alert in the incident by hand. Always say what fixed it.",
      "alerts:write",
      {"incident_id": _str("Incident id"),
       "category": _str("What kind of fix", enum=list(alert_ops.RESOLUTION_CATEGORIES)),
       "note": _str("What fixed it, in a sentence or two")},
      ("incident_id", "category", "note"))
def _resolve_incident(args, ident):
    from . import incidents
    from .api.paging import IncidentDetailSchema, _incident_detail

    return IncidentDetailSchema().dump(_incident_detail(_call(
        incidents.resolve, args["incident_id"], ident.name, category=args["category"],
        note=args["note"])))


@tool("take_shift", "Take an on-call shift yourself",
      "Put the token's owner on call for a schedule for `hours` hours starting now (an "
      "override). Only ever for yourself.", "oncall:write",
      {"schedule_id": _str("Schedule id"), "hours": _int("How long", 1, 72),
       "reason": _str("Why, e.g. covering for Bob")},
      ("schedule_id", "hours"))
def _take_shift(args, ident):
    from .api.schedules import OverrideSchema
    from .models import Schedule, ScheduleOverride

    sch = SessionLocal.get(Schedule, args["schedule_id"])
    if sch is None:
        raise ToolError("schedule not found")
    t = now()
    row = ScheduleOverride(schedule_id=sch.id, user_id=ident.user_id, starts_at=t,
                           ends_at=t + dt.timedelta(hours=args["hours"]),
                           reason=args.get("reason") or "", created_by=ident.name)
    SessionLocal.add(row)
    SessionLocal.commit()
    return OverrideSchema().dump(row)


@tool("expire_silence", "End a silence now", "Expire a silence immediately.", "alerts:write",
      {"silence_id": _str("Silence id")}, ("silence_id",))
def _expire(args, ident):
    s = _call(alert_ops.expire_silence, args["silence_id"], ident.name)
    s.state = "expired"
    return SilenceSchema().dump(s)


# --- transport -----------------------------------------------------------------------

def _json(payload: dict, status: int = 200, headers: dict | None = None) -> Response:
    resp = Response(json.dumps(payload, default=str), status=status,
                    mimetype="application/json")
    # OX Agent: Server Hardening prevented — no caching of alert data, no MIME sniffing
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    for k, v in (headers or {}).items():
        resp.headers[k] = v
    return resp


def _rpc_error(msg_id, code: int, message: str, status: int = 200) -> Response:
    return _json({"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}},
                 status)


def _rpc_result(msg_id, result: dict) -> Response:
    return _json({"jsonrpc": "2.0", "id": msg_id, "result": result})


def _text_result(text: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _encode(value: Any) -> str:
    text = json.dumps(value, default=str, ensure_ascii=False, indent=1)
    if len(text) > MAX_RESULT_CHARS:
        return (text[:MAX_RESULT_CHARS] + f"\n… [truncated at {MAX_RESULT_CHARS} chars; "
                "narrow it with filters or a lower `limit`]")
    return text


def _call_tool(ident, params: dict) -> dict | None:
    name = params.get("name")
    t = TOOLS.get(name) if isinstance(name, str) else None
    if t is None:
        return None
    started = time.monotonic()
    target = ""
    try:
        # OX Agent: Broken Access Control prevented — each tool checks the scope its REST twin needs
        if not ident.can(t.scope):
            raise ToolError(f"this token lacks the '{t.scope}' scope needed by {t.name}")
        args = _validate(t, params.get("arguments"))
        target = args.get("alert_id") or args.get("incident_id") or args.get("silence_id") or ""
        result = _text_result(_encode(t.handler(args, ident)))
        outcome, detail = "ok", ""
    except ToolError as e:
        SessionLocal.rollback()
        result, outcome, detail = _text_result(str(e), True), "refused", str(e)
    except Exception:  # noqa: BLE001
        SessionLocal.rollback()
        logger.exception("MCP tool %s failed", name)
        # OX Agent: Server Hardening prevented — no exception text or traceback to the client
        result = _text_result(f"{name} failed — see Shorthop's logs", True)
        outcome, detail = "error", "unhandled exception"
    MCP_CALLS.labels(name, outcome).inc()
    record(ident.name, f"mcp.{name}", target=str(target),
           status="ok" if outcome == "ok" else "error",
           detail=detail or f"{(time.monotonic() - started) * 1000:.0f} ms")
    return result


def _origin_ok() -> bool:
    """DNS-rebinding guard the spec asks for: a CLI sends no Origin; a page on another
    origin is refused."""
    origin = request.headers.get("Origin")
    return not origin or urlparse(origin).netloc == request.host


@bp.route("/mcp", methods=["GET", "DELETE"])
def mcp_no_stream():
    return Response(status=405, headers={"Allow": "POST"})


@bp.route("/mcp", methods=["POST"])
def mcp_endpoint():
    if not _origin_ok():
        return _json({"error": "origin not allowed"}, 403)
    # OX Agent: Broken Authentication prevented — bearer token only; a browser session is ignored
    ident = token_identity()
    if ident is None:
        return _json({"error": "missing or invalid API token"}, 401,
                     {"WWW-Authenticate": 'Bearer realm="shorthop"'})
    if FORBIDDEN_SCOPES & set(ident.scopes):
        return _json({"error": "the MCP endpoint refuses tokens holding config:write or admin "
                               "— mint one with alerts:read (and alerts:write to act)"}, 403)

    version = request.headers.get("MCP-Protocol-Version")
    if version and version not in PROTOCOL_VERSIONS:
        return _json({"error": f"unsupported MCP-Protocol-Version '{version}'"}, 400)

    msg = request.get_json(silent=True)
    if msg is None:
        return _rpc_error(None, -32700, "parse error", 400)
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _rpc_error(None, -32600, "expected one JSON-RPC 2.0 message", 400)
    if "id" not in msg:
        return Response(status=202)  # a notification
    msg_id, method = msg["id"], msg.get("method")
    params = msg.get("params") or {}
    if not isinstance(params, dict):
        return _rpc_error(msg_id, -32602, "params must be an object")

    if method == "initialize":
        asked = params.get("protocolVersion")
        return _rpc_result(msg_id, {
            "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "shorthop", "title": "Shorthop",
                           "version": os.getenv("APP_VERSION", "") or "dev"},
            "instructions": INSTRUCTIONS,
        })
    if method == "ping":
        return _rpc_result(msg_id, {})
    if method == "tools/list":
        # Only the tools this token can actually use — offering a write tool to a read-only
        # token just produces refusals.
        return _rpc_result(msg_id, {"tools": [t.definition() for t in TOOLS.values()
                                              if ident.can(t.scope)]})
    if method == "tools/call":
        result = _call_tool(ident, params)
        if result is None:
            return _rpc_error(msg_id, -32602, f"unknown tool '{params.get('name')}'")
        return _rpc_result(msg_id, result)
    return _rpc_error(msg_id, -32601, f"method not found: {method}")
