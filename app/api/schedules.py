"""M3 API: on-call schedules, overrides, and "who is on call"."""
import datetime as dt
from zoneinfo import ZoneInfo, available_timezones

from flask import g
from flask_smorest import Blueprint, abort
from marshmallow import EXCLUDE, Schema, fields, validate
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .. import schedules as svc
from ..audit import record
from ..auth import require
from ..core import oncall
from ..db import SessionLocal
from ..models import EscalationPolicy, Schedule, ScheduleOverride, Team, User, aware, now

blp = Blueprint("schedules", __name__, url_prefix="/api/v1",
                description="On-call schedules, overrides and who is on call")
db = SessionLocal
MAX_WINDOW_DAYS = 62
_TIMEZONES = available_timezones()


class _In(Schema):
    class Meta:
        unknown = EXCLUDE


def _get(model, obj_id, what):
    row = db.get(model, obj_id)
    if row is None:
        abort(404, message=f"{what} not found")
    return row


def _user_ref(uid):
    u = db.get(User, uid) if uid else None
    return {"id": u.id, "email": u.email, "name": u.name} if u else None


class ScheduleSchema(Schema):
    id = fields.Str()
    name = fields.Str()
    team_id = fields.Str(allow_none=True)
    timezone = fields.Str()
    layers = fields.List(fields.Dict())
    handover_notify = fields.Bool()
    created_at = fields.DateTime()
    on_call = fields.Method("_on_call")

    def _on_call(self, obj):
        t = now()
        who = svc.on_call_now(obj, t)
        until = svc.next_change(obj, t)
        return {"user": _user_ref(who["user_id"]), "source": who["source"],
                "until": until.isoformat() if until else None}


class OverrideSchema(Schema):
    id = fields.Str()
    user_id = fields.Str()
    user = fields.Method("_user")
    starts_at = fields.DateTime()
    ends_at = fields.DateTime()
    reason = fields.Str()
    created_by = fields.Str()
    created_at = fields.DateTime()

    def _user(self, obj):
        return _user_ref(obj.user_id)


class ScheduleDetailSchema(ScheduleSchema):
    overrides = fields.Method("_overrides")

    def _overrides(self, obj):
        t = now()
        rows = [o for o in obj.overrides if aware(o.ends_at) > t]
        return OverrideSchema(many=True).dump(sorted(rows, key=lambda o: aware(o.starts_at)))


class ScheduleInSchema(_In):
    name = fields.Str(required=True, validate=validate.Length(min=1, max=200))
    team_id = fields.Str(load_default=None, allow_none=True)
    timezone = fields.Str(load_default="UTC")
    layers = fields.List(fields.Dict(), required=True)
    handover_notify = fields.Bool(load_default=True)


def _clean(payload) -> dict:
    if payload["timezone"] not in _TIMEZONES:
        abort(422, message=f"unknown timezone '{payload['timezone']}' — use an IANA name like Europe/Stockholm")
    try:
        payload["layers"] = oncall.validate_layers(payload["layers"])
    except ValueError as e:
        abort(422, message=str(e))
    for layer in payload["layers"]:
        for uid in layer["users"]:
            if db.get(User, uid) is None:
                abort(422, message=f"layer '{layer['name']}': user {uid} does not exist")
    if payload.get("team_id") and db.get(Team, payload["team_id"]) is None:
        abort(422, message="team does not exist")
    return payload


@blp.route("/schedules")
@require("oncall:read")
@blp.response(200, ScheduleSchema(many=True))
def list_schedules():
    return db.scalars(select(Schedule).order_by(Schedule.name)).all()


@blp.route("/schedules", methods=["POST"])
@require("config:write")
@blp.arguments(ScheduleInSchema)
@blp.response(201, ScheduleDetailSchema)
def create_schedule(payload):
    row = Schedule(**_clean(payload))
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        abort(409, message="a schedule with that name already exists")
    record(g.identity.name, "schedule.create", target=row.name)
    return row


@blp.route("/schedules/<schedule_id>")
@require("oncall:read")
@blp.response(200, ScheduleDetailSchema)
def get_schedule(schedule_id):
    return _get(Schedule, schedule_id, "schedule")


@blp.route("/schedules/<schedule_id>", methods=["PUT"])
@require("config:write")
@blp.arguments(ScheduleInSchema)
@blp.response(200, ScheduleDetailSchema)
def update_schedule(payload, schedule_id):
    row = _get(Schedule, schedule_id, "schedule")
    for k, v in _clean(payload).items():
        setattr(row, k, v)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        abort(409, message="a schedule with that name already exists")
    record(g.identity.name, "schedule.update", target=row.name)
    return row


@blp.route("/schedules/<schedule_id>", methods=["DELETE"])
@require("config:write")
@blp.response(204)
def delete_schedule(schedule_id):
    row = _get(Schedule, schedule_id, "schedule")
    used = [p.name for p in db.scalars(select(EscalationPolicy)).all()
            if any(t.get("type") == "schedule" and t.get("id") == row.id
                   for s in p.steps or [] for t in s.get("targets", []))]
    if used:
        abort(409, message=f"used by escalation policy(ies): {', '.join(used)}")
    db.delete(row)
    db.commit()
    record(g.identity.name, "schedule.delete", target=row.name)


class WindowArgs(_In):
    start = fields.DateTime(load_default=None)
    days = fields.Int(load_default=14, validate=validate.Range(min=1, max=MAX_WINDOW_DAYS))


def _window(args):
    start = aware(args["start"]) if args.get("start") else now().replace(minute=0, second=0, microsecond=0)
    return start, start + dt.timedelta(days=args["days"])


def _seg_out(seg):
    out = {"user": _user_ref(seg["user_id"]), "source": seg["source"],
           "start": seg["start"].isoformat(), "end": seg["end"].isoformat()}
    if "shift_start" in seg:
        # The whole shift, when the window cut it: what "override this shift" covers.
        out["shift_start"] = seg["shift_start"].isoformat()
        out["shift_end"] = seg["shift_end"].isoformat()
    return out


@blp.route("/schedules/<schedule_id>/shifts")
@require("oncall:read")
@blp.arguments(WindowArgs, location="query")
def schedule_shifts(args, schedule_id):
    """The final on-call timeline over a window (default: the next 14 days), plus each
    layer's own timeline so a calendar can show where the answer comes from."""
    s = _get(Schedule, schedule_id, "schedule")
    start, end = _window(args)
    final = [_seg_out(x) for x in svc.framed_shifts(s, start, end)]
    layers = []
    for layer in s.layers or []:
        only = {"timezone": s.timezone, "layers": [layer]}
        layers.append({"name": layer["name"],
                       "shifts": [_seg_out(x) for x in oncall.framed_shifts(only, [], start, end)]})
    return {"start": start.isoformat(), "end": end.isoformat(), "timezone": s.timezone,
            "final": final, "layers": layers}


class OverrideInSchema(_In):
    user_id = fields.Str(load_default=None)
    starts_at = fields.DateTime(required=True)
    ends_at = fields.DateTime(required=True)
    reason = fields.Str(load_default="", validate=validate.Length(max=500))


@blp.route("/schedules/<schedule_id>/overrides", methods=["POST"])
@require("oncall:write")
@blp.arguments(OverrideInSchema)
@blp.response(201, OverrideSchema)
def create_override(payload, schedule_id):
    """Cover a window. Taking it yourself needs oncall:write; putting SOMEONE ELSE on call
    needs config:write — nobody gets volunteered by a colleague's token."""
    s = _get(Schedule, schedule_id, "schedule")
    uid = payload.get("user_id") or g.identity.user_id
    # OX Agent: Broken Access Control prevented — assigning another person needs config:write
    if uid != g.identity.user_id and not g.identity.can("config:write"):
        abort(403, message="you can take a shift yourself; putting someone else on call needs config:write")
    if db.get(User, uid) is None:
        abort(422, message="user does not exist")
    a, b = aware(payload["starts_at"]), aware(payload["ends_at"])
    if b <= a:
        abort(422, message="ends_at must be after starts_at")
    if b <= now():
        abort(422, message="that window is already over")
    if b - a > dt.timedelta(days=MAX_WINDOW_DAYS):
        abort(422, message=f"an override may last at most {MAX_WINDOW_DAYS} days")
    row = ScheduleOverride(schedule_id=s.id, user_id=uid, starts_at=a, ends_at=b,
                           reason=payload["reason"], created_by=g.identity.name)
    db.add(row)
    db.commit()
    record(g.identity.name, "schedule.override", target=s.name,
           detail=f"{_user_ref(uid)['email']} {a.isoformat()} → {b.isoformat()}")
    return row


@blp.route("/schedules/<schedule_id>/overrides/<override_id>", methods=["DELETE"])
@require("oncall:write")
@blp.response(204)
def delete_override(schedule_id, override_id):
    row = db.get(ScheduleOverride, override_id)
    if row is None or row.schedule_id != schedule_id:
        abort(404, message="override not found")
    mine = row.user_id == g.identity.user_id or row.created_by == g.identity.name
    if not mine and not g.identity.can("config:write"):
        abort(403, message="only the person covering, whoever created it, or config:write may remove it")
    db.delete(row)
    db.commit()
    record(g.identity.name, "schedule.override.delete", target=schedule_id)


@blp.route("/oncall")
@require("oncall:read")
def who_is_on_call():
    """Every schedule, who is on call right now, and until when."""
    out = []
    for s in db.scalars(select(Schedule).order_by(Schedule.name)).all():
        t = now()
        who = svc.on_call_now(s, t)
        until = svc.next_change(s, t)
        out.append({"schedule_id": s.id, "schedule": s.name, "timezone": s.timezone,
                    "user": _user_ref(who["user_id"]), "source": who["source"],
                    "until": until.isoformat() if until else None})
    return out


@blp.route("/me/shifts")
@require("oncall:read")
@blp.arguments(WindowArgs, location="query")
def my_shifts(args):
    """My own upcoming on-call shifts across every schedule."""
    start, end = _window(args)
    out = []
    for s in db.scalars(select(Schedule)).all():
        for seg in svc.shifts(s, start, end):
            if seg["user_id"] == g.identity.user_id:
                out.append({"schedule_id": s.id, "schedule": s.name, "timezone": s.timezone,
                            "source": seg["source"], "start": seg["start"].isoformat(),
                            "end": seg["end"].isoformat()})
    return sorted(out, key=lambda x: x["start"])
