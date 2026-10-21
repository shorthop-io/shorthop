"""M4: pairing phones and registering them for push.

    web (signed in):  POST /api/v1/devices/pair-codes          → {code, expires_at, link}
    phone:            POST /api/v1/devices/pair {code, name}    → {token: "shd_…", user}
    phone:            PUT  /api/v1/devices/me/push {push_token}

A phone never goes through Google: identity was already proven in the browser that minted
the code. The code is short so it can be typed, which is why it is single-use, expires in
PAIR_CODE_MINUTES, is stored hashed, and the pairing door is rate-limited per client address.
"""
import datetime as dt
import secrets as _secrets
from urllib.parse import quote

from flask import g, request
from flask_smorest import Blueprint, abort
from marshmallow import EXCLUDE, Schema, fields, validate
from sqlalchemy import select

from .. import config as cfg
from .. import ratelimit, relay
from ..clientip import client_ip
from ..exposure import public_base_url
from ..audit import record
from ..auth import DEVICE_TOKEN_PREFIX, hash_token, new_token, require
from ..db import SessionLocal
from ..models import Device, PairCode, User, aware, now

blp = Blueprint("devices", __name__, url_prefix="/api/v1",
                description="Pairing phones and registering them for push")
db = SessionLocal

PAIR_CODE_MINUTES = 10
# No 0/O/1/I/L: a code read off one screen and typed into another must survive the reading.
_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
_PAIR_LIMIT = 20          # attempts …
_PAIR_WINDOW_S = 600      # … per address per 10 minutes


class _In(Schema):
    class Meta:
        unknown = EXCLUDE


class DeviceSchema(Schema):
    id = fields.Str()
    name = fields.Str()
    platform = fields.Str()
    prefix = fields.Str()
    has_push = fields.Method("_push")
    created_at = fields.DateTime()
    last_seen_at = fields.DateTime(allow_none=True)
    revoked = fields.Bool()
    # Push relay state (only meaningful when a relay is configured): registered / refused /
    # error, and the relay's own words for a refusal ("add a payment method …").
    relay_status = fields.Str(allow_none=True)
    relay_message = fields.Str(allow_none=True)
    relay_signs_out_at = fields.DateTime(allow_none=True)

    def _push(self, obj):
        return bool(obj.push_token)


def _client_addr() -> str:
    # Not X-Forwarded-For's first entry: the caller writes that, so keying a limit on it hands
    # out a fresh budget per request. app/clientip.py believes only our own proxies.
    return client_ip()


def _rate_limited(addr: str) -> bool:
    # Counted in the database (app/ratelimit.py): an in-process counter would give every
    # replica its own 20 attempts.
    return ratelimit.hit("pair", addr, _PAIR_LIMIT, _PAIR_WINDOW_S)


@blp.route("/devices/pair-codes", methods=["POST"])
@require("alerts:read")
def create_pair_code():
    """A one-time code to pair a phone as yourself. Shown once; expires in 10 minutes."""
    if g.identity.via == "device":
        abort(403, message="a phone cannot pair another phone")
    code = "".join(_secrets.choice(_ALPHABET) for _ in range(8))
    expires = now() + dt.timedelta(minutes=PAIR_CODE_MINUTES)
    db.add(PairCode(user_id=g.identity.user_id, code_hash=hash_token(code), expires_at=expires))
    db.commit()
    record(g.identity.name, "device.pair_code")
    # The phone talks to the PUBLIC listener (PUBLIC_API_URL), not the private web origin.
    base = public_base_url()
    return {"code": f"{code[:4]}-{code[4:]}", "expires_at": expires.isoformat(),
            "link": f"shorthop://pair?url={quote(base, safe='')}&code={code}"}


class PairSchema(_In):
    code = fields.Str(required=True, validate=validate.Length(min=8, max=12))
    name = fields.Str(load_default="phone", validate=validate.Length(max=200))
    platform = fields.Str(load_default="", validate=validate.Length(max=20))


@blp.route("/devices/pair", methods=["POST"])
@blp.arguments(PairSchema)
def pair(payload):
    """Exchange a pairing code for a device token (shown once). No credential needed — the
    code IS the credential, which is why it is short-lived, single-use and rate-limited."""
    addr = _client_addr()
    # OX Agent: Brute Force prevented — pairing attempts rate-limited per client address
    if _rate_limited(addr):
        abort(429, message="too many pairing attempts — wait a few minutes")
    code = payload["code"].replace("-", "").replace(" ", "").upper()
    row = db.scalar(select(PairCode).where(PairCode.code_hash == hash_token(code)))
    if row is None or row.used_at is not None or aware(row.expires_at) <= now():
        abort(400, message="that code is wrong, used or expired — make a new one on the web")
    user = db.get(User, row.user_id)
    if user is None or user.disabled:
        abort(400, message="that code is wrong, used or expired — make a new one on the web")
    row.used_at = now()
    plaintext, digest, prefix = new_token(DEVICE_TOKEN_PREFIX)
    dev = Device(user_id=user.id, name=payload["name"] or "phone", platform=payload["platform"],
                 token_hash=digest, prefix=prefix)
    db.add(dev)
    db.commit()
    record(user.email, "device.pair", target=dev.name, detail=payload["platform"])
    return {"token": plaintext, "device": DeviceSchema().dump(dev),
            "user": {"id": user.id, "email": user.email, "name": user.name}}


@blp.route("/devices")
@require("alerts:read")
@blp.response(200, DeviceSchema(many=True))
def list_devices():
    """Your paired phones."""
    return db.scalars(select(Device).where(Device.user_id == g.identity.user_id)
                      .order_by(Device.created_at.desc())).all()


@blp.route("/devices/<device_id>", methods=["DELETE"])
@require("alerts:read")
@blp.response(200, DeviceSchema)
def revoke_device(device_id):
    dev = db.get(Device, device_id)
    if dev is None or (dev.user_id != g.identity.user_id and not g.identity.can("admin")):
        abort(404, message="device not found")
    relay.sign_out_device(dev)   # frees the phone's paid seat at the relay
    dev.revoked, dev.push_token = True, None
    db.commit()
    record(g.identity.name, "device.revoke", target=dev.name)
    return dev


class PushSchema(_In):
    # Expo tokens look like ExponentPushToken[xxxxxxxx]; refuse anything else so a typo or a
    # raw FCM token is a clear 422, not a delivery failure at 03:00.
    push_token = fields.Str(required=True, validate=validate.Regexp(
        r"^Expo(nent)?PushToken\[[A-Za-z0-9_\-]{10,200}\]$", error="not an Expo push token"))


def _this_device() -> Device:
    if g.identity.via != "device":
        abort(403, message="only a paired phone can do this")
    return db.get(Device, g.identity.device_id)


@blp.route("/devices/me/push", methods=["PUT"])
@require("alerts:read")
@blp.arguments(PushSchema)
@blp.response(200, DeviceSchema)
def register_push(payload):
    """The phone's Expo push token. Re-sent on every launch; a token moves between devices
    only by re-pairing, so it is unique to this row."""
    dev = _this_device()
    for other in db.scalars(select(Device).where(Device.push_token == payload["push_token"],
                                                 Device.id != dev.id)).all():
        other.push_token = None  # the same phone paired again: the old row stops receiving
        relay.sign_out_device(other)
    if dev.push_token != payload["push_token"]:
        relay.sign_out_device(dev)   # a new token is a new registration at the relay
    dev.push_token = payload["push_token"]
    # Register with the relay now, so a refusal reaches the app and the Phones page straight
    # away. Pairing itself never fails over it: the phone still acknowledges, and email still
    # pages — only push to THIS phone waits for a seat.
    relay.sync_device(dev, force=True)   # the phone itself: may sign back in after going unused
    db.commit()
    return dev


@blp.route("/devices/me", methods=["DELETE"])
@require("alerts:read")
@blp.response(204)
def unpair_self():
    """A phone signing itself out: its token stops working and it stops receiving pages."""
    dev = _this_device()
    relay.sign_out_device(dev)
    dev.revoked, dev.push_token = True, None
    db.commit()
    record(g.identity.name, "device.unpair", target=dev.name)
