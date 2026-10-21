"""Shorthop Watch checks, managed from this server (relay SHOR-31 / W7).

A thin proxy to the push relay's `/v1/watch` API, authenticated with the relay key in
Settings → Push relay — the relay decides validity, quota (the first check is free) and
isolation; this module adds only what the relay cannot know: which INTEGRATION forwards its
Watchdog to which check.

    GET    /api/v1/watch/checks                  alerts:read   relay list + forwarded_by
    POST   /api/v1/watch/checks                  config:write  {…, integration_id?}; URL ONCE
    PATCH  /api/v1/watch/checks/<id>             config:write
    DELETE /api/v1/watch/checks/<id>             config:write  (also stops the forwarding)
    POST   /api/v1/watch/checks/<id>/rotate      config:write  new URL ONCE; a bound one is re-stored
    POST   /api/v1/watch/checks/<id>/pause       config:write  {until}
    POST   /api/v1/watch/checks/<id>/resume      config:write
    GET    /api/v1/watch/checks/<id>/events      alerts:read
    POST   /api/v1/integrations/<id>/watch       config:write  {check_id}: forward to an existing check
    DELETE /api/v1/integrations/<id>/watch       config:write  stop forwarding (the check stays)

Relay refusals (402 over the free check, 404, 409, 422) come back with the relay's own words
and status; an unreachable relay or a rejected key is a 502 that says which.
"""
from __future__ import annotations

from flask import g, request
from flask_smorest import Blueprint, abort
from werkzeug.exceptions import HTTPException
from marshmallow import fields, validate
from sqlalchemy import select

from .. import relay, watch
from ..audit import record
from ..auth import require
from ..db import SessionLocal
from ..models import Integration
from .schemas import IntegrationSchema, _In

blp = Blueprint("watch", __name__, url_prefix="/api/v1",
                description="Shorthop Watch: dead man's switch checks on the push relay")
db = SessionLocal

_FIELDS = ("name", "interval_s", "grace_s", "renotify_s", "device_ids")


class CheckCreateSchema(_In):
    name = fields.Str(required=True, validate=validate.Length(min=1, max=128))
    interval_s = fields.Int(load_default=None)
    grace_s = fields.Int(load_default=None)
    renotify_s = fields.Int(allow_none=True)
    device_ids = fields.List(fields.Str())
    # Forward this Prometheus integration's Watchdog to the new check.
    integration_id = fields.Str(load_default=None)


class CheckPatchSchema(_In):
    name = fields.Str(validate=validate.Length(min=1, max=128))
    interval_s = fields.Int()
    grace_s = fields.Int()
    renotify_s = fields.Int(allow_none=True)
    device_ids = fields.List(fields.Str())


class PauseSchema(_In):
    until = fields.Str(required=True)


class BindSchema(_In):
    check_id = fields.Str(required=True, validate=validate.Length(equal=36))


class _PaymentRequired(HTTPException):
    """Werkzeug ships no 402, and the app's aborter is built before this module is imported,
    so `abort(402)` would be a LookupError (a 500) in place of the relay's "add a payment
    method" — the one refusal the person must be able to read. Raised directly instead."""
    code = 402
    description = "Payment required."


def _relay(method: str, path: str, json: dict | None = None, params: dict | None = None):
    """Call the relay; return its JSON (or None on 204), or abort with its own words."""
    try:
        resp = watch.call(method, path, json=json, params=params)
    except relay.RelayError as e:
        abort(502, message=str(e))
    if resp.status_code == 204:
        return None
    if resp.status_code < 300:
        return resp.json()
    if resp.status_code == 401:
        abort(502, message="the push relay rejected this install's key — check Settings → "
                           "Push relay")
    if resp.status_code == 402:
        exc = _PaymentRequired()
        exc.data = {"message": relay._message(resp), "errors": relay._errors(resp) or None}
        raise exc
    if resp.status_code in (403, 404, 409, 422, 429):
        abort(resp.status_code, message=relay._message(resp), errors=relay._errors(resp) or None)
    abort(502, message=f"the push relay answered HTTP {resp.status_code}")


def _prometheus_integration(integration_id: str) -> Integration:
    row = db.get(Integration, integration_id)
    if row is None or row.archived_at is not None:
        abort(404, message="integration not found")
    if row.kind != "prometheus":
        abort(422, message="only a Prometheus integration has a Watchdog to forward")
    return row


def _bound_to(check_id: str) -> Integration | None:
    return db.scalars(select(Integration).where(Integration.watch_check_id == check_id)).first()


def _bind(row: Integration, check_id: str, ping_url: str) -> None:
    if not watch.valid_ping_url(ping_url):
        abort(502, message="the push relay returned a ping URL this server does not accept")
    row.watch_check_id, row.watch_ping_url, row.watch_forwarded_at = check_id, ping_url, None


def _snippets(url: str) -> dict:
    return {
        "curl": f"curl -fsS -m 10 --retry 3 {url}",
        "cron": f"*/5 * * * * your-job && curl -fsS -m 10 --retry 3 {url}",
        "heartbeat_env": f"HEARTBEAT_URL={url}",
    }


@blp.route("/watch/checks")
@require("alerts:read")
def list_checks():
    out = _relay("GET", "/checks")
    bound = {r.watch_check_id: {"id": r.id, "name": r.name}
             for r in db.scalars(select(Integration).where(
                 Integration.watch_check_id.is_not(None))).all()}
    for c in out.get("checks", []):
        c["forwarded_by"] = bound.get(c["id"])
    return out


@blp.route("/watch/checks", methods=["POST"])
@require("config:write")
@blp.arguments(CheckCreateSchema)
def create_check(payload):
    """Create a check; its ping URL is in this response ONCE. With `integration_id`, that
    Prometheus integration's Watchdog is forwarded to it, and the defaults suit a Watchdog:
    a ping a minute, and a grace longer than ShorthopIngestSilent's, so Shorthop pages first."""
    integ = None
    if payload.get("integration_id"):
        integ = _prometheus_integration(payload["integration_id"])
        if integ.watch_check_id:
            abort(409, message=f"{integ.name} already forwards its Watchdog to a Watch check")
        payload["interval_s"] = payload.get("interval_s") or 60
        payload["grace_s"] = payload.get("grace_s") or watch.default_grace_s()
    if not payload.get("interval_s"):
        abort(422, message="interval_s: how often the check is pinged, in seconds")
    # Absent stays absent (the relay's default); renotify_s may be an explicit null (= never).
    body = {k: payload[k] for k in _FIELDS
            if k in payload and (payload[k] is not None or k == "renotify_s")}
    out = _relay("POST", "/checks", json=body)
    if integ is not None:
        _bind(integ, out["id"], out["ping_url"])
        db.commit()
        out["forwarded_by"] = {"id": integ.id, "name": integ.name}
    record(g.identity.name, "watch.create", target=out["name"],
           detail=f"forwards {integ.name}" if integ else "")
    out["snippets"] = _snippets(out["ping_url"])
    return out, 201


@blp.route("/watch/checks/<check_id>", methods=["PATCH"])
@require("config:write")
@blp.arguments(CheckPatchSchema)
def update_check(payload, check_id):
    out = _relay("PATCH", f"/checks/{check_id}", json={k: payload[k] for k in _FIELDS if k in payload})
    record(g.identity.name, "watch.update", target=out["name"])
    return out


@blp.route("/watch/checks/<check_id>", methods=["DELETE"])
@require("config:write")
def delete_check(check_id):
    _relay("DELETE", f"/checks/{check_id}")
    integ = _bound_to(check_id)
    if integ is not None:
        integ.watch_check_id = integ.watch_ping_url = integ.watch_forwarded_at = None
        db.commit()
    record(g.identity.name, "watch.delete", target=check_id)
    return {"deleted": True}


@blp.route("/watch/checks/<check_id>/rotate", methods=["POST"])
@require("config:write")
def rotate_check(check_id):
    """A new ping URL; the old one stops working. A bound integration switches to the new
    one in the same request, so forwarding never pings a dead URL."""
    out = _relay("POST", f"/checks/{check_id}/rotate")
    integ = _bound_to(check_id)
    if integ is not None:
        _bind(integ, check_id, out["ping_url"])
        db.commit()
    record(g.identity.name, "watch.rotate", target=out["name"])
    out["snippets"] = _snippets(out["ping_url"])
    return out


@blp.route("/watch/checks/<check_id>/pause", methods=["POST"])
@require("config:write")
@blp.arguments(PauseSchema)
def pause_check(payload, check_id):
    out = _relay("POST", f"/checks/{check_id}/pause", json={"until": payload["until"]})
    record(g.identity.name, "watch.pause", target=out["name"], detail=f"until {payload['until']}")
    return out


@blp.route("/watch/checks/<check_id>/resume", methods=["POST"])
@require("config:write")
def resume_check(check_id):
    out = _relay("POST", f"/checks/{check_id}/resume")
    record(g.identity.name, "watch.resume", target=out["name"])
    return out


@blp.route("/watch/checks/<check_id>/events")
@require("alerts:read")
def check_events(check_id):
    limit = request.args.get("limit", "50")
    return {"events": _relay("GET", f"/checks/{check_id}/events", params={"limit": limit})}


@blp.route("/integrations/<integration_id>/watch", methods=["POST"])
@require("config:write")
@blp.arguments(BindSchema)
@blp.response(200, IntegrationSchema)
def bind_integration(payload, integration_id):
    """Forward this integration's Watchdog to an EXISTING check. The check's URL was only ever
    shown once, so binding ROTATES it: anything else pinging the old URL stops counting."""
    integ = _prometheus_integration(integration_id)
    other = _bound_to(payload["check_id"])
    if other is not None and other.id != integ.id:
        abort(409, message=f"that check already receives {other.name}'s Watchdog")
    out = _relay("POST", f"/checks/{payload['check_id']}/rotate")
    _bind(integ, out["id"], out["ping_url"])
    db.commit()
    record(g.identity.name, "watch.bind", target=integ.name, detail=out["name"])
    return integ


@blp.route("/integrations/<integration_id>/watch", methods=["DELETE"])
@require("config:write")
@blp.response(200, IntegrationSchema)
def unbind_integration(integration_id):
    """Stop forwarding. The check itself stays on the relay — and will go DOWN once its grace
    runs out unless something else pings it, or it is paused or deleted."""
    integ = _prometheus_integration(integration_id)
    integ.watch_check_id = integ.watch_ping_url = integ.watch_forwarded_at = None
    db.commit()
    record(g.identity.name, "watch.unbind", target=integ.name)
    return integ
