"""Alerts, history and silences — /api/v1/alerts, /api/v1/silences."""
import datetime as dt

from flask import g
from flask_smorest import Blueprint, abort
from sqlalchemy import select

from .. import alerts as alert_ops
from ..audit import record
from ..auth import require
from ..db import SessionLocal
from ..models import Silence, aware, now
from .schemas import (
    AlertListArgs, HistorySchema, NoteSchema, OccurrenceDetailSchema, OccurrenceSchema,
    ResolveSchema, SilenceCreateSchema, SilenceListArgs, SilencePreviewSchema, SilenceSchema,
)

blp = Blueprint("alerts", __name__, url_prefix="/api/v1",
                description="Alerts (occurrences), their history, and silences")


def _with_silences(rows):
    silences = alert_ops.active_silences()
    for r in rows:
        r.silenced_by = alert_ops.silenced_by(r, silences)
    return rows


def _detail(occ):
    _with_silences([occ])
    occ.history = alert_ops.history(occ.signature, exclude_id=occ.id)
    return occ


def _act(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except LookupError as e:
        abort(404, message=str(e))
    except alert_ops.AlertError as e:
        abort(409, message=str(e))


def _labels(raw: list[str]) -> dict[str, str]:
    out = {}
    for item in raw:
        if "=" not in item:
            abort(422, message=f"label filter '{item}' must be key=value")
        k, v = item.split("=", 1)
        out[k.strip()] = v
    return out


@blp.route("/alerts")
@require("alerts:read")
@blp.arguments(AlertListArgs, location="query")
@blp.response(200, OccurrenceSchema(many=True))
def list_alerts(args):
    """Alerts, newest first. `status=open` (default) is everything not yet resolved."""
    rows = alert_ops.list_occurrences(
        status=args["status"], label_filters=_labels(args["label"]), q=args["q"],
        team_id=args["team_id"], limit=args["limit"])
    return _with_silences(rows)


@blp.route("/alerts/<occurrence_id>")
@require("alerts:read")
@blp.response(200, OccurrenceDetailSchema)
def get_alert(occurrence_id):
    """One alert with its timeline and the history of its signature."""
    return _detail(_act(alert_ops.get, occurrence_id))


@blp.route("/alerts/<occurrence_id>/ack", methods=["POST"])
@require("alerts:write")
@blp.response(200, OccurrenceDetailSchema)
def ack_alert(occurrence_id):
    occ = _act(alert_ops.acknowledge, occurrence_id, g.identity.name)
    record(g.identity.name, "alert.ack", target=occurrence_id)
    return _detail(occ)


@blp.route("/alerts/<occurrence_id>/resolve", methods=["POST"])
@require("alerts:write")
@blp.arguments(ResolveSchema)
@blp.response(200, OccurrenceDetailSchema)
def resolve_alert(payload, occurrence_id):
    """Resolve by hand, optionally saying what fixed it. Also works on an alert the sender
    already resolved: adding the fix afterwards is the point."""
    occ = _act(alert_ops.resolve, occurrence_id, g.identity.name,
               category=payload.get("category"), note=payload.get("note"))
    record(g.identity.name, "alert.resolve", target=occurrence_id,
           detail=payload.get("category") or "")
    return _detail(occ)


@blp.route("/alerts/<occurrence_id>/notes", methods=["POST"])
@require("alerts:write")
@blp.arguments(NoteSchema)
@blp.response(200, OccurrenceDetailSchema)
def note_alert(payload, occurrence_id):
    occ = _act(alert_ops.add_note, occurrence_id, g.identity.name, payload["text"])
    return _detail(occ)


@blp.route("/signatures/<signature>/history")
@require("alerts:read")
@blp.response(200, HistorySchema)
def signature_history(signature):
    """Every time this alert fired before, newest first, and what fixed it."""
    return alert_ops.history(signature, limit=50)


# --- silences ------------------------------------------------------------------------

def _state(s: Silence, t: dt.datetime) -> str:
    if aware(s.ends_at) <= t:
        return "expired"
    return "pending" if aware(s.starts_at) > t else "active"


@blp.route("/silences")
@require("alerts:read")
@blp.arguments(SilenceListArgs, location="query")
@blp.response(200, SilenceSchema(many=True))
def list_silences(args):
    t = now()
    stmt = select(Silence).order_by(Silence.ends_at.desc())
    if args["state"] in ("current", "active", "pending"):
        stmt = stmt.where(Silence.ends_at > t)
    rows = SessionLocal.scalars(stmt.limit(500)).all()
    for s in rows:
        s.state = _state(s, t)
    if args["state"] in ("active", "pending", "expired"):
        rows = [s for s in rows if s.state == args["state"]]
    return rows


@blp.route("/silences", methods=["POST"])
@require("alerts:write")
@blp.arguments(SilenceCreateSchema)
@blp.response(201, SilenceSchema)
def create_silence(payload):
    s = _act(alert_ops.create_silence, payload["matchers"], payload.get("starts_at"),
             payload["ends_at"], payload["comment"], g.identity.name,
             team_id=payload.get("team_id"))
    record(g.identity.name, "silence.create", target=s.id, detail=s.comment)
    s.state = _state(s, now())
    return s


@blp.route("/silences/preview", methods=["POST"])
@require("alerts:read")
@blp.arguments(SilencePreviewSchema)
@blp.response(200, OccurrenceSchema(many=True))
def preview_silence(payload):
    """The open alerts these matchers would mute right now."""
    return _with_silences(_act(alert_ops.preview_silence, payload["matchers"]))


@blp.route("/silences/<silence_id>", methods=["DELETE"])
@require("alerts:write")
@blp.response(200, SilenceSchema)
def expire_silence(silence_id):
    """Expire now. Silences are never deleted — who muted what, and why, stays on record."""
    s = _act(alert_ops.expire_silence, silence_id, g.identity.name)
    record(g.identity.name, "silence.expire", target=silence_id)
    s.state = _state(s, now())
    return s
