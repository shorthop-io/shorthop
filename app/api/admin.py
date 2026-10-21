"""People and configuration: /me, users, teams, tokens, integrations, audit."""
import datetime as dt
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import g, request
from flask_smorest import Blueprint, abort
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .. import promclient, selfmon
from ..audit import record
from ..auth import (
    INTEGRATION_TOKEN_PREFIX, TOKEN_PREFIX, csrf_token, hash_password, new_token, require,
    role_scopes, validate_scopes,
)
from ..core import contact
from ..db import SessionLocal
from ..models import (ApiToken, AuditEntry, Integration, Occurrence, Route, Team, TeamMember,
                      User, now)
from .schemas import (
    AuditSchema, IntegrationCreateSchema, IntegrationCreatedSchema, IntegrationSchema,
    ContactRulesSchema, IntegrationUpdateSchema, MeSchema, PasswordSchema, ProfileUpdateSchema,
    TeamCreateSchema, TeamMemberAddSchema,
    TeamSchema, TeamUpdateSchema, TokenCreateSchema, TokenCreatedSchema, TokenSchema,
    UserCreateSchema, UserSchema, UserUpdateSchema,
)

blp = Blueprint("admin", __name__, url_prefix="/api/v1",
                description="Users, teams, tokens, integrations and the audit log")

db = SessionLocal


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


# --- me ------------------------------------------------------------------------------

@blp.route("/me")
@require("alerts:read")
@blp.response(200, MeSchema)
def me():
    """Who you are, what you may do, and (for a browser session) the CSRF token every
    write must send back as `X-CSRF-Token`."""
    return _me_obj()


def _me_obj():
    user = g.identity.user
    user.scopes = g.identity.scopes
    user.via = g.identity.via
    user.csrf_token = csrf_token() if g.identity.via == "session" else ""
    return user


_PHONE = re.compile(r"^\+?[0-9 ()\-]{5,30}$")


@blp.route("/me", methods=["PUT"])
@require("alerts:read")
@blp.arguments(ProfileUpdateSchema)
@blp.response(200, MeSchema)
def update_me(payload):
    """Your own name, time zone and phone. Role and email are an admin's to change."""
    user = g.identity.user
    if "name" in payload:
        user.name = payload["name"].strip()
    if "timezone" in payload:
        tz = (payload["timezone"] or "").strip() or None
        if tz:
            try:
                ZoneInfo(tz)
            except (ZoneInfoNotFoundError, ValueError):
                abort(422, message=f"timezone: '{tz}' is not a time zone (e.g. Europe/Stockholm)")
        user.timezone = tz
    if "phone" in payload:
        phone = (payload["phone"] or "").strip() or None
        if phone and not _PHONE.match(phone):
            abort(422, message="phone: digits, spaces and an optional leading + (e.g. +46 70 123 45 67)")
        user.phone = phone
    db.commit()
    record(g.identity.name, "me.update", detail=", ".join(sorted(payload)))
    return _me_obj()


@blp.route("/me/contact-rules", methods=["PUT"])
@require("alerts:read")
@blp.arguments(ContactRulesSchema)
@blp.response(200, MeSchema)
def set_contact_rules(payload):
    """How YOU are paged when an escalation step reaches you: push and/or email, each after a
    delay; a delayed page is dropped if the incident is acknowledged first."""
    rules = payload["rules"]
    user = g.identity.user
    if rules is None:
        user.contact_rules = None
    else:
        try:
            user.contact_rules = contact.validate(rules)
        except contact.RuleError as e:
            abort(422, message=str(e))
    db.commit()
    record(g.identity.name, "me.contact_rules",
           detail="default" if user.contact_rules is None else f"{len(user.contact_rules)} rule(s)")
    return _me_obj()


# --- users (admin) -------------------------------------------------------------------

@blp.route("/users")
@require("oncall:read")
@blp.response(200, UserSchema(many=True))
def list_users():
    return db.scalars(select(User).order_by(User.email)).all()


@blp.route("/users", methods=["POST"])
@require("admin")
@blp.arguments(UserCreateSchema)
@blp.response(201, UserSchema)
def create_user(payload):
    """Invite someone. They sign in with Google using this address; there is no self-signup."""
    user = User(email=payload["email"].strip().lower(), name=payload["name"],
                role=payload["role"])
    db.add(user)
    _commit_or_conflict("a user with that email already exists")
    record(g.identity.name, "user.create", target=user.email, detail=user.role)
    return user


@blp.route("/users/<user_id>", methods=["PATCH"])
@require("admin")
@blp.arguments(UserUpdateSchema)
@blp.response(200, UserSchema)
def update_user(payload, user_id):
    user = _get(User, user_id, "user")
    if user.id == g.identity.user_id and (payload.get("disabled") or
                                          payload.get("role", "admin") != "admin"):
        # The last thing an admin should be able to do by accident is lock themselves out.
        abort(409, message="you cannot disable or demote yourself")
    for key in ("name", "role", "disabled"):
        if key in payload:
            setattr(user, key, payload[key])
    db.commit()
    record(g.identity.name, "user.update", target=user.email,
           detail=", ".join(f"{k}={payload[k]}" for k in payload))
    return user


@blp.route("/users/<user_id>/password", methods=["PUT"])
@require("admin")
@blp.arguments(PasswordSchema)
@blp.response(204)
def set_password(payload, user_id):
    """Set the break-glass password for a user. Normal sign-in is Google; this is for when
    Google is the thing that is broken."""
    user = _get(User, user_id, "user")
    user.password_hash = hash_password(payload["password"])
    db.commit()
    record(g.identity.name, "user.password.set", target=user.email)


@blp.route("/users/<user_id>/password", methods=["DELETE"])
@require("admin")
@blp.response(204)
def clear_password(user_id):
    user = _get(User, user_id, "user")
    user.password_hash = None
    db.commit()
    record(g.identity.name, "user.password.clear", target=user.email)


# --- teams ---------------------------------------------------------------------------

def _may_manage_team(team: Team) -> bool:
    if g.identity.can("config:write"):
        return True
    return any(m.user_id == g.identity.user_id and m.role == "owner" for m in team.members)


@blp.route("/teams")
@require("oncall:read")
@blp.response(200, TeamSchema(many=True))
def list_teams():
    return db.scalars(select(Team).order_by(Team.name)).all()


@blp.route("/teams", methods=["POST"])
@require("config:write")
@blp.arguments(TeamCreateSchema)
@blp.response(201, TeamSchema)
def create_team(payload):
    team = Team(**payload)
    db.add(team)
    _commit_or_conflict("a team with that slug already exists")
    record(g.identity.name, "team.create", target=team.slug)
    return team


@blp.route("/teams/<team_id>")
@require("oncall:read")
@blp.response(200, TeamSchema)
def get_team(team_id):
    return _get(Team, team_id, "team")


@blp.route("/teams/<team_id>", methods=["PATCH"])
@require("oncall:read")
@blp.arguments(TeamUpdateSchema)
@blp.response(200, TeamSchema)
def update_team(payload, team_id):
    team = _get(Team, team_id, "team")
    # OX Agent: Broken Access Control prevented — only config:write or the team's owners
    if not _may_manage_team(team):
        abort(403, message="only an owner of this team (or config:write) may change it")
    for key, value in payload.items():
        setattr(team, key, value)
    db.commit()
    record(g.identity.name, "team.update", target=team.slug)
    return team


@blp.route("/teams/<team_id>", methods=["DELETE"])
@require("config:write")
@blp.response(204)
def delete_team(team_id):
    team = _get(Team, team_id, "team")
    db.delete(team)
    db.commit()
    record(g.identity.name, "team.delete", target=team.slug)


@blp.route("/teams/<team_id>/members", methods=["POST"])
@require("oncall:read")
@blp.arguments(TeamMemberAddSchema)
@blp.response(200, TeamSchema)
def add_member(payload, team_id):
    team = _get(Team, team_id, "team")
    if not _may_manage_team(team):
        abort(403, message="only an owner of this team (or config:write) may change it")
    user = _get(User, payload["user_id"], "user")
    existing = next((m for m in team.members if m.user_id == user.id), None)
    if existing:
        existing.role = payload["role"]
    else:
        team.members.append(TeamMember(user_id=user.id, role=payload["role"]))
    db.commit()
    record(g.identity.name, "team.member.set", target=team.slug,
           detail=f"{user.email}={payload['role']}")
    return team


@blp.route("/teams/<team_id>/members/<user_id>", methods=["DELETE"])
@require("oncall:read")
@blp.response(200, TeamSchema)
def remove_member(team_id, user_id):
    team = _get(Team, team_id, "team")
    if not _may_manage_team(team):
        abort(403, message="only an owner of this team (or config:write) may change it")
    member = next((m for m in team.members if m.user_id == user_id), None)
    if member is None:
        abort(404, message="not a member of this team")
    team.members.remove(member)
    db.commit()
    record(g.identity.name, "team.member.remove", target=team.slug, detail=user_id)
    return team


# --- personal access tokens ------------------------------------------------------------

@blp.route("/tokens")
@require("alerts:read")
@blp.response(200, TokenSchema(many=True))
def list_tokens():
    """Your own tokens. An admin adds `?all=1` to see everybody's."""
    stmt = select(ApiToken).order_by(ApiToken.created_at.desc())
    if not (request.args.get("all") and g.identity.can("admin")):
        stmt = stmt.where(ApiToken.user_id == g.identity.user_id)
    return db.scalars(stmt).all()


@blp.route("/tokens", methods=["POST"])
@require("alerts:read")
@blp.arguments(TokenCreateSchema)
@blp.response(201, TokenCreatedSchema)
def create_token(payload):
    """Mint a personal token. Shown ONCE. It can never hold more than your own role — and
    if your role is later narrowed, so is the token."""
    scopes, error = validate_scopes(payload["scopes"])
    if error:
        abort(422, message=error)
    mine = role_scopes(g.identity.user.role)
    if "admin" not in mine:
        beyond = [s for s in scopes if s not in mine]
        # OX Agent: Privilege Escalation prevented — a token cannot carry scopes its owner lacks
        if beyond:
            abort(403, message=f"your role does not include: {', '.join(beyond)}")
    plaintext, digest, prefix = new_token(TOKEN_PREFIX)
    expires = (now() + dt.timedelta(days=payload["expires_in_days"])
               if payload.get("expires_in_days") else None)
    row = ApiToken(user_id=g.identity.user_id, name=payload["name"], token_hash=digest,
                   prefix=prefix, scopes=",".join(scopes), expires_at=expires)
    db.add(row)
    db.commit()
    record(g.identity.name, "token.create", target=prefix, detail=row.scopes)
    row.token = plaintext
    return row


@blp.route("/tokens/<token_id>", methods=["DELETE"])
@require("alerts:read")
@blp.response(200, TokenSchema)
def revoke_token(token_id):
    row = _get(ApiToken, token_id, "token")
    if row.user_id != g.identity.user_id and not g.identity.can("admin"):
        abort(404, message="token not found")  # not 403: ids of others' tokens are not probeable
    row.revoked = True
    db.commit()
    record(g.identity.name, "token.revoke", target=row.prefix)
    return row


# --- integrations ----------------------------------------------------------------------

@blp.route("/integrations")
@require("alerts:read")
@blp.response(200, IntegrationSchema(many=True))
def list_integrations():
    """Archived (deleted) integrations are left out unless ?archived=1."""
    q = select(Integration).order_by(Integration.name)
    if request.args.get("archived") not in ("1", "true"):
        q = q.where(Integration.archived_at.is_(None))
    return db.scalars(q).all()


@blp.route("/integrations", methods=["POST"])
@require("config:write")
@blp.arguments(IntegrationCreateSchema)
@blp.response(201, IntegrationCreatedSchema)
def create_integration(payload):
    """Create an inbound door. Its token is shown ONCE — paste it into the sender."""
    payload["prometheus_url"] = _prometheus_url(payload.get("prometheus_url"), payload["kind"])
    plaintext, digest, prefix = new_token(INTEGRATION_TOKEN_PREFIX)
    row = Integration(token_hash=digest, prefix=prefix, **payload)
    db.add(row)
    _commit_or_conflict("an integration with that name already exists")
    record(g.identity.name, "integration.create", target=row.name, detail=row.kind)
    row.token = plaintext
    return row


@blp.route("/integrations/<integration_id>", methods=["PATCH"])
@require("config:write")
@blp.arguments(IntegrationUpdateSchema)
@blp.response(200, IntegrationSchema)
def update_integration(payload, integration_id):
    row = _get(Integration, integration_id, "integration")
    if row.archived_at is not None:
        abort(404, message="integration not found")
    if row.kind == "internal":
        abort(409, message="the built-in integration cannot be changed")
    if "prometheus_url" in payload:
        payload["prometheus_url"] = _prometheus_url(payload["prometheus_url"], row.kind)
        row.shadow_status = None  # a different Prometheus: the old comparison means nothing
    for key, value in payload.items():
        setattr(row, key, value)
    _commit_or_conflict("an integration with that name already exists")
    record(g.identity.name, "integration.update", target=row.name)
    return row


@blp.route("/integrations/<integration_id>", methods=["DELETE"])
@require("config:write")
def delete_integration(integration_id):
    """Delete an inbound door. One that never delivered an alert is removed outright. One that
    did is ARCHIVED: its alerts are history and are never deleted, so the row stays for them
    to point at — but its token stops working at once, it is hidden from the list and its name
    is freed for reuse. Either way it is dropped from any route that filtered on it."""
    row = _get(Integration, integration_id, "integration")
    if row.kind == "internal":
        abort(409, message="the built-in integration cannot be deleted")
    if row.archived_at is not None:
        abort(404, message="integration not found")
    name = row.name
    for route in db.scalars(select(Route)).all():
        if route.integration_ids and row.id in route.integration_ids:
            route.integration_ids = [i for i in route.integration_ids if i != row.id]

    has_alerts = db.scalar(select(Occurrence.id).where(
        Occurrence.integration_id == row.id).limit(1)) is not None
    if not has_alerts:
        db.delete(row)
        db.commit()
        record(g.identity.name, "integration.delete", target=name)
        return {"deleted": True, "archived": False}

    # Close Shorthop's own alerts about it first, while they still carry its name.
    if row.kind == "prometheus":
        selfmon._raise("ShorthopIngestSilent", row, False, "critical", "")
        selfmon._raise("ShorthopShadowMismatch", row, False, "warning", "")
    t = now()
    _, digest, _prefix = new_token(INTEGRATION_TOKEN_PREFIX)  # the old token is dead now
    row.token_hash = digest
    row.enabled = False
    row.prometheus_url = None
    row.shadow_status = None
    row.archived_at = t
    row.name = f"{name} (deleted {t:%Y-%m-%d %H:%M})"[:200]
    db.commit()
    record(g.identity.name, "integration.delete", target=name, detail="archived: has alert history")
    return {"deleted": True, "archived": True}


def _prometheus_url(url, kind):
    """None/blank clears it. Validated against PROMETHEUS_ALLOWED_HOSTS at write time too, so
    a URL that would never be fetched is refused with a reason instead of stored."""
    if not url or not url.strip():
        return None
    if kind != "prometheus":
        abort(422, message="only a prometheus integration can be compared with a Prometheus")
    try:
        return promclient.check_url(url)
    except promclient.PrometheusError as e:
        abort(422, message=str(e))


@blp.route("/integrations/<integration_id>/check", methods=["POST"])
@require("alerts:read")
@blp.response(200, IntegrationSchema)
def check_integration(integration_id):
    """Compare with the integration's Prometheus now (normally done every minute)."""
    row = _get(Integration, integration_id, "integration")
    if not row.prometheus_url:
        abort(409, message="set a Prometheus URL on this integration first")
    selfmon.check_shadow(row)
    return row


@blp.route("/integrations/<integration_id>/rotate", methods=["POST"])
@require("config:write")
@blp.response(200, IntegrationCreatedSchema)
def rotate_integration(integration_id):
    """Issue a new token; the old one stops working immediately."""
    row = _get(Integration, integration_id, "integration")
    if row.archived_at is not None:
        abort(404, message="integration not found")
    if row.kind == "internal":
        abort(409, message="the built-in integration has no token to rotate")
    plaintext, digest, prefix = new_token(INTEGRATION_TOKEN_PREFIX)
    row.token_hash, row.prefix = digest, prefix
    db.commit()
    record(g.identity.name, "integration.rotate", target=row.name)
    row.token = plaintext
    return row


# --- audit -----------------------------------------------------------------------------

@blp.route("/audit")
@require("admin")
@blp.response(200, AuditSchema(many=True))
def list_audit():
    return db.scalars(select(AuditEntry).order_by(AuditEntry.at.desc()).limit(500)).all()
