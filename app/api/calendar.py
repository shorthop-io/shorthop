"""Calendar feeds: manage your iCal subscription links, download a one-off .ics, and the
public feed URL a calendar app fetches.

⚠️ The public door (`GET /calendar/<token>.ics`) has no session and no bearer header —
calendar apps send neither — so the URL's secret is the credential. It is checked on every
fetch against the OWNER's current state: a revoked feed, a disabled owner or an owner whose
role no longer reads schedules all answer 404, exactly like an unknown token (a feed must not
tell a stranger that it once existed).
"""
from flask import Blueprint as FlaskBlueprint, Response, g, request
from flask_smorest import Blueprint, abort
from marshmallow import EXCLUDE, Schema, fields, validate
from sqlalchemy import select

from .. import calendar_feeds as feeds
from .. import config as cfg
from .. import ratelimit
from ..clientip import client_ip
from ..exposure import public_base_url
from ..audit import record
from ..auth import hash_token, new_token, require, role_scopes, scopes_allow
from ..db import SessionLocal
from ..models import CalendarFeed, Schedule, User, now

blp = Blueprint("calendar", __name__, url_prefix="/api/v1", description="Calendar (iCal) feeds of on-call shifts")
public = FlaskBlueprint("calendar_public", __name__)
db = SessionLocal
FEED_PREFIX = "shc_"
MAX_FEEDS = 20


class FeedSchema(Schema):
    id = fields.Str()
    kind = fields.Str()
    schedule_id = fields.Str(allow_none=True)
    name = fields.Str()
    prefix = fields.Str()
    created_at = fields.DateTime()
    last_fetched_at = fields.DateTime(allow_none=True)
    revoked = fields.Bool()


class FeedCreatedSchema(FeedSchema):
    url = fields.Str()
    webcal_url = fields.Str()


class FeedInSchema(Schema):
    class Meta:
        unknown = EXCLUDE

    kind = fields.Str(required=True, validate=validate.OneOf(["mine", "schedule"]))
    schedule_id = fields.Str(load_default=None, allow_none=True)


def _base() -> str:
    # Calendar apps fetch from the PUBLIC listener (PUBLIC_API_URL) when there is one.
    return public_base_url()


@blp.route("/me/calendar-feeds")
@require("oncall:read")
@blp.response(200, FeedSchema(many=True))
def list_feeds():
    return db.scalars(select(CalendarFeed).where(CalendarFeed.user_id == g.identity.user_id)
                      .order_by(CalendarFeed.created_at.desc())).all()


@blp.route("/me/calendar-feeds", methods=["POST"])
@require("oncall:read")
@blp.arguments(FeedInSchema)
@blp.response(201, FeedCreatedSchema)
def create_feed(payload):
    """A subscription URL. Shown ONCE — paste it into the calendar app; make a new one for
    another calendar rather than sharing it."""
    uid = g.identity.user_id
    active = db.scalars(select(CalendarFeed).where(CalendarFeed.user_id == uid,
                                                   CalendarFeed.revoked.is_(False))).all()
    if len(active) >= MAX_FEEDS:
        abort(409, message=f"at most {MAX_FEEDS} calendar links — revoke one you no longer use")
    sched = None
    if payload["kind"] == "schedule":
        sched = db.get(Schedule, payload.get("schedule_id") or "")
        if sched is None:
            abort(422, message="schedule_id: that schedule does not exist")
    plaintext, digest, prefix = new_token(FEED_PREFIX)
    row = CalendarFeed(user_id=uid, kind=payload["kind"], schedule_id=sched.id if sched else None,
                       name=f"On call — {sched.name}" if sched else "My on-call shifts",
                       token_hash=digest, prefix=prefix)
    db.add(row)
    db.commit()
    record(g.identity.name, "calendar_feed.create", target=row.name, detail=prefix)
    row.url = f"{_base()}/calendar/{plaintext}.ics"
    row.webcal_url = "webcal://" + row.url.split("://", 1)[1]
    return row


@blp.route("/me/calendar-feeds/<feed_id>", methods=["DELETE"])
@require("oncall:read")
@blp.response(200, FeedSchema)
def revoke_feed(feed_id):
    row = db.get(CalendarFeed, feed_id)
    # OX Agent: Broken Access Control prevented — only the feed's owner can revoke it
    if row is None or row.user_id != g.identity.user_id:
        abort(404, message="calendar link not found")
    row.revoked = True
    db.commit()
    record(g.identity.name, "calendar_feed.revoke", target=row.name, detail=row.prefix)
    return row


class DownloadArgs(Schema):
    class Meta:
        unknown = EXCLUDE

    schedule_id = fields.Str(load_default=None)


@blp.route("/me/calendar.ics")
@require("oncall:read")
@blp.arguments(DownloadArgs, location="query")
def download(args):
    """A one-off .ics (a snapshot, not a subscription) — for a calendar that cannot reach
    this server, e.g. Google Calendar against a private Shorthop."""
    if args.get("schedule_id"):
        s = db.get(Schedule, args["schedule_id"])
        if s is None:
            abort(404, message="schedule not found")
        body, fname = feeds.schedule(s, f"download:{s.id}"), f"oncall-{s.name}.ics"
    else:
        body, fname = feeds.mine(g.identity.user, f"download:{g.identity.user_id}"), "my-oncall.ics"
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in fname)
    return Response(body, mimetype="text/calendar",
                    headers={"Content-Disposition": f'attachment; filename="{safe}"'})


@public.get("/calendar/<token>.ics")
def feed(token):
    # Two limits, because the fetchers are not people: Google and Outlook poll every feed from
    # a handful of shared addresses, so a per-address cap on SUCCESSFUL fetches would starve
    # every subscriber at once. Instead: misses (unknown tokens) per address, and fetches per
    # feed — which bounds one leaked feed URL without touching anyone else's.
    addr = client_ip()
    if ratelimit.count("feed_miss", addr, 600) >= cfg.FEED_MISSES_PER_10MIN:
        return Response("too many requests\n", status=429, mimetype="text/plain")
    row = db.scalar(select(CalendarFeed).where(CalendarFeed.token_hash == hash_token(token)))
    owner = db.get(User, row.user_id) if row else None
    ok = (row is not None and not row.revoked and owner is not None and not owner.disabled
          and scopes_allow(role_scopes(owner.role), "oncall:read"))
    sched = None
    if ok and row.kind == "schedule":
        sched = db.get(Schedule, row.schedule_id) if row.schedule_id else None
        ok = sched is not None
    if not ok:
        ratelimit.hit("feed_miss", addr, cfg.FEED_MISSES_PER_10MIN, 600)
        return Response("not found\n", status=404, mimetype="text/plain")
    if ratelimit.hit("feed", row.id, cfg.FEED_FETCHES_PER_HOUR, 3600):
        return Response("too many requests\n", status=429, mimetype="text/plain",
                        headers={"Retry-After": "600"})
    row.last_fetched_at = now()
    db.commit()
    # The UIDs are keyed on the FEED id: two feeds of one schedule (two calendars) are two
    # calendars, and must not collide in an app that merges them.
    body = feeds.schedule(sched, row.id) if sched else feeds.mine(owner, row.id)
    return Response(body, mimetype="text/calendar",
                    headers={"Cache-Control": "private, max-age=300",
                             "Content-Disposition": 'inline; filename="oncall.ics"'})
