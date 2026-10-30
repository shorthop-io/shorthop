from marshmallow import EXCLUDE, Schema, fields, validate

from ..alerts import RESOLUTION_CATEGORIES
from ..auth import ROLES, SCOPES


class _In(Schema):
    class Meta:
        unknown = EXCLUDE  # a client newer than this server must not 422 on a new field


# --- alerts --------------------------------------------------------------------------

class TimelineEventSchema(Schema):
    at = fields.DateTime()
    kind = fields.Str()
    actor = fields.Str()
    detail = fields.Str()


class OccurrenceSchema(Schema):
    id = fields.Str()
    integration_id = fields.Str()
    team_id = fields.Str(allow_none=True)
    fingerprint = fields.Str()
    signature = fields.Str()
    alertname = fields.Str()
    severity = fields.Str()
    status = fields.Str()
    labels = fields.Dict()
    rule_labels = fields.Dict(allow_none=True)
    annotations = fields.Dict()
    generator_url = fields.Str()
    started_at = fields.DateTime()
    last_seen_at = fields.DateTime()
    expires_at = fields.DateTime()
    acked_at = fields.DateTime(allow_none=True)
    acked_by = fields.Str(allow_none=True)
    resolved_at = fields.DateTime(allow_none=True)
    resolved_how = fields.Str(allow_none=True)
    resolved_by = fields.Str(allow_none=True)
    resolution_category = fields.Str(allow_none=True)
    resolution_note = fields.Str(allow_none=True)
    send_count = fields.Int()
    incident_id = fields.Str(allow_none=True)
    silenced_by = fields.List(fields.Str(), dump_default=list)


class HistoryItemSchema(Schema):
    id = fields.Str()
    status = fields.Str()
    started_at = fields.DateTime()
    resolved_at = fields.DateTime(allow_none=True)
    resolved_how = fields.Str(allow_none=True)
    resolved_by = fields.Str(allow_none=True)
    resolution_category = fields.Str(allow_none=True)
    resolution_note = fields.Str(allow_none=True)
    labels = fields.Dict()


class HistorySchema(Schema):
    signature = fields.Str()
    total = fields.Int()
    last_90_days = fields.Int()
    last_started_at = fields.DateTime(allow_none=True)
    recent = fields.List(fields.Nested(HistoryItemSchema))
    recent_fixes = fields.List(fields.Nested(HistoryItemSchema))


class OccurrenceDetailSchema(OccurrenceSchema):
    signature_labels = fields.Dict()
    events = fields.List(fields.Nested(TimelineEventSchema))
    history = fields.Nested(HistorySchema)


class AlertListArgs(_In):
    status = fields.Str(load_default="open", validate=validate.OneOf(
        ["open", "firing", "acknowledged", "resolved", "all"]))
    q = fields.Str(load_default="")
    team_id = fields.Str(load_default=None)
    # label=key=value, repeatable
    label = fields.List(fields.Str(), load_default=list)
    limit = fields.Int(load_default=100, validate=validate.Range(min=1, max=1000))


class ResolveSchema(_In):
    category = fields.Str(load_default=None, validate=validate.OneOf(RESOLUTION_CATEGORIES))
    note = fields.Str(load_default=None, validate=validate.Length(max=4000))


class NoteSchema(_In):
    text = fields.Str(required=True, validate=validate.Length(min=1, max=4000))
    # "suggestion" = written by an automation (an AI triage agent), shown apart from people's notes.
    kind = fields.Str(load_default="note", validate=validate.OneOf(["note", "suggestion"]))


# --- silences ------------------------------------------------------------------------

class MatcherSchema(Schema):
    name = fields.Str(required=True)
    value = fields.Str(required=True)
    isRegex = fields.Bool(load_default=False)
    isEqual = fields.Bool(load_default=True)


class SilenceSchema(Schema):
    id = fields.Str()
    matchers = fields.List(fields.Nested(MatcherSchema))
    starts_at = fields.DateTime()
    ends_at = fields.DateTime()
    comment = fields.Str()
    created_by = fields.Str()
    team_id = fields.Str(allow_none=True)
    created_at = fields.DateTime()
    expired_by = fields.Str(allow_none=True)
    state = fields.Str()  # pending | active | expired, computed


class SilenceCreateSchema(_In):
    matchers = fields.List(fields.Dict(), required=True)
    starts_at = fields.DateTime(load_default=None)
    ends_at = fields.DateTime(required=True)
    comment = fields.Str(load_default="", validate=validate.Length(max=2000))
    team_id = fields.Str(load_default=None)


class SilencePreviewSchema(_In):
    matchers = fields.List(fields.Dict(), required=True)


class SilenceListArgs(_In):
    state = fields.Str(load_default="current", validate=validate.OneOf(
        ["current", "active", "pending", "expired", "all"]))


# --- people & config ---------------------------------------------------------------

class TeamRefSchema(Schema):
    team_id = fields.Str()
    slug = fields.Str(attribute="team.slug")
    name = fields.Str(attribute="team.name")
    role = fields.Str()


class UserSchema(Schema):
    id = fields.Str()
    email = fields.Str()
    name = fields.Str()
    role = fields.Str()
    disabled = fields.Bool()
    has_password = fields.Method("_has_password")
    created_at = fields.DateTime()
    last_login_at = fields.DateTime(allow_none=True)
    teams = fields.List(fields.Nested(TeamRefSchema), attribute="memberships")

    def _has_password(self, obj):
        return bool(obj.password_hash)


class MeSchema(UserSchema):
    scopes = fields.List(fields.Str())
    csrf_token = fields.Str()
    via = fields.Str()
    timezone = fields.Str(allow_none=True)
    phone = fields.Str(allow_none=True)
    # The rules in force (the default when none are set) and whether they ARE the default.
    contact_rules = fields.Method("_rules")
    contact_rules_default = fields.Method("_rules_default")
    contact_gap = fields.Method("_gap")

    def _rules(self, obj):
        from ..core import contact
        return obj.contact_rules or contact.DEFAULT_RULES

    def _rules_default(self, obj):
        return not obj.contact_rules

    def _gap(self, obj):
        from ..core import contact
        return contact.immediate_gap(obj.contact_rules or contact.DEFAULT_RULES)


class ProfileUpdateSchema(_In):
    """Your own profile. Omitted keys are left alone; null/"" clears timezone and phone."""
    name = fields.Str(validate=validate.Length(max=200))
    timezone = fields.Str(allow_none=True, validate=validate.Length(max=64))
    phone = fields.Str(allow_none=True, validate=validate.Length(max=40))


class ContactRulesSchema(_In):
    """null = back to the default (email + every phone, at once)."""
    rules = fields.List(fields.Dict(), required=True, allow_none=True)


class UserCreateSchema(_In):
    email = fields.Email(required=True)
    name = fields.Str(load_default="", validate=validate.Length(max=200))
    role = fields.Str(load_default="responder", validate=validate.OneOf(list(ROLES)))


class UserUpdateSchema(_In):
    name = fields.Str(validate=validate.Length(max=200))
    role = fields.Str(validate=validate.OneOf(list(ROLES)))
    disabled = fields.Bool()


class PasswordSchema(_In):
    password = fields.Str(required=True, validate=validate.Length(min=12, max=200))


class TeamMemberSchema(Schema):
    user_id = fields.Str()
    email = fields.Str(attribute="user.email")
    name = fields.Str(attribute="user.name")
    role = fields.Str()


class TeamSchema(Schema):
    id = fields.Str()
    slug = fields.Str()
    name = fields.Str()
    description = fields.Str()
    created_at = fields.DateTime()
    members = fields.List(fields.Nested(TeamMemberSchema))


class TeamCreateSchema(_In):
    slug = fields.Str(required=True, validate=validate.Regexp(
        r"^[a-z][a-z0-9-]{1,62}$", error="lower-case letters, digits and dashes"))
    name = fields.Str(required=True, validate=validate.Length(min=1, max=200))
    description = fields.Str(load_default="", validate=validate.Length(max=2000))


class TeamUpdateSchema(_In):
    name = fields.Str(validate=validate.Length(min=1, max=200))
    description = fields.Str(validate=validate.Length(max=2000))


class TeamMemberAddSchema(_In):
    user_id = fields.Str(required=True)
    role = fields.Str(load_default="member", validate=validate.OneOf(["owner", "member"]))


class TokenSchema(Schema):
    id = fields.Str()
    name = fields.Str()
    prefix = fields.Str()
    scopes = fields.Method("_scopes")
    user_id = fields.Str()
    created_at = fields.DateTime()
    last_used_at = fields.DateTime(allow_none=True)
    expires_at = fields.DateTime(allow_none=True)
    revoked = fields.Bool()

    def _scopes(self, obj):
        return obj.scope_list()


class TokenCreatedSchema(TokenSchema):
    token = fields.Str()


class TokenCreateSchema(_In):
    name = fields.Str(required=True, validate=validate.Length(min=1, max=200))
    scopes = fields.List(fields.Str(validate=validate.OneOf(list(SCOPES))), required=True)
    expires_in_days = fields.Int(load_default=None, validate=validate.Range(min=1, max=730))


class IntegrationSchema(Schema):
    id = fields.Str()
    name = fields.Str()
    kind = fields.Str()
    team_id = fields.Str(allow_none=True)
    prefix = fields.Str()
    extra_labels = fields.Dict()
    signature_ignore = fields.List(fields.Str(), allow_none=True)
    enabled = fields.Bool()
    created_at = fields.DateTime()
    last_received_at = fields.DateTime(allow_none=True)
    last_watchdog_at = fields.DateTime(allow_none=True)
    prometheus_url = fields.Str(allow_none=True)
    archived_at = fields.DateTime(allow_none=True)
    # Shorthop Watch forwarding. The ping URL itself is a credential and is never returned.
    watch_check_id = fields.Str(allow_none=True)
    watch_forwarded_at = fields.DateTime(allow_none=True)
    shadow_status = fields.Method("_shadow")
    ingest_path = fields.Method("_path")

    def _path(self, obj):
        return {"prometheus": "/ingest/prometheus/api/v2/alerts",
                "alertmanager": "/ingest/alertmanager",
                "webhook": "/ingest/webhook"}.get(obj.kind, "")

    def _shadow(self, obj):
        # The raw keys are bookkeeping for the next comparison, not something to show.
        s = obj.shadow_status
        return {k: v for k, v in s.items() if not k.endswith("_keys")} if s else None


class IntegrationCreatedSchema(IntegrationSchema):
    token = fields.Str()


class IntegrationCreateSchema(_In):
    name = fields.Str(required=True, validate=validate.Length(min=1, max=200))
    kind = fields.Str(required=True, validate=validate.OneOf(
        ["prometheus", "alertmanager", "webhook"]))
    team_id = fields.Str(load_default=None)
    extra_labels = fields.Dict(keys=fields.Str(), values=fields.Str(), load_default=dict)
    signature_ignore = fields.List(fields.Str(), load_default=None)
    prometheus_url = fields.Str(load_default=None, allow_none=True, validate=validate.Length(max=500))


class IntegrationUpdateSchema(_In):
    name = fields.Str(validate=validate.Length(min=1, max=200))
    team_id = fields.Str(allow_none=True)
    extra_labels = fields.Dict(keys=fields.Str(), values=fields.Str())
    signature_ignore = fields.List(fields.Str(), allow_none=True)
    enabled = fields.Bool()
    prometheus_url = fields.Str(allow_none=True, validate=validate.Length(max=500))


class AuditSchema(Schema):
    id = fields.Int()
    at = fields.DateTime()
    actor = fields.Str()
    action = fields.Str()
    target = fields.Str()
    status = fields.Str()
    detail = fields.Str()
