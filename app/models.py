"""Shorthop's data model (M1: people, credentials, alerts, silences; M2: routing, incidents,
escalation, channels and the notification outbox — at the end of this file).

The central idea is the split between an alert's two identities:

* **fingerprint** — the exact label set (scoped to its integration). Two alerts with the
  same fingerprint are the same alert; Prometheus re-sends it every evaluation and we
  update one row.
* **signature** — the fingerprint minus the labels that name an *instance* (`pod`,
  `instance`, …). The same fault tomorrow on another pod has a new fingerprint and the SAME
  signature, which is what makes "last time this fired, what fixed it" answerable.

An **Occurrence** is one firing episode: it opens when an alert starts firing and closes when
it resolves. Occurrences are never deleted — the history is the product.
"""
import datetime as dt
import uuid

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base, JSONType


def _uuid() -> str:
    return str(uuid.uuid4())


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def aware(value: dt.datetime | None) -> dt.datetime | None:
    """SQLite hands back naive datetimes even for timezone=True columns; everything stored
    here is UTC, so a naive value is UTC. Postgres returns aware ones already."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


TS = DateTime(timezone=True)


# --- people ------------------------------------------------------------------------

class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(254), unique=True)  # stored lower-cased
    name: Mapped[str] = mapped_column(String(200), default="")
    # admin | responder | viewer — see auth.ROLES
    role: Mapped[str] = mapped_column(String(20), default="responder")
    # Break-glass only: SSO is the normal door, a password exists for when SSO is down.
    password_hash: Mapped[str | None] = mapped_column(String(100), nullable=True)
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=now)
    last_login_at: Mapped[dt.datetime | None] = mapped_column(TS, nullable=True)
    # The person's own settings (My account). timezone = IANA name for showing times; NULL =
    # the browser's / phone's. phone = for an SMS/voice fallback later. contact_rules = how they
    # are paged (app/core/contact.py); NULL = the default (email + every phone, at once).
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    contact_rules: Mapped[list | None] = mapped_column(JSONType, nullable=True)
    # The person's Slack user id (U…), found by email the first time they act in Slack or are
    # paged there. Kept so a later Slack email change still maps to the same person.
    slack_user_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)

    memberships: Mapped[list["TeamMember"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=now)

    members: Mapped[list["TeamMember"]] = relationship(
        back_populates="team", cascade="all, delete-orphan"
    )


class TeamMember(Base):
    __tablename__ = "team_members"
    __table_args__ = (UniqueConstraint("team_id", "user_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # owner (manages the team's config) | member
    role: Mapped[str] = mapped_column(String(20), default="member")
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=now)

    team: Mapped[Team] = relationship(back_populates="members")
    user: Mapped[User] = relationship(back_populates="memberships")


# --- credentials -------------------------------------------------------------------

class ApiToken(Base):
    """A personal access token. Acts AS its user, narrowed to its own scopes."""

    __tablename__ = "api_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)  # sha256 hex
    prefix: Mapped[str] = mapped_column(String(16))  # shown in lists, never the secret
    scopes: Mapped[str] = mapped_column(String(500))  # comma-separated
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=now)
    last_used_at: Mapped[dt.datetime | None] = mapped_column(TS, nullable=True)
    expires_at: Mapped[dt.datetime | None] = mapped_column(TS, nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)

    user: Mapped[User] = relationship()

    def scope_list(self) -> list[str]:
        return [s for s in (self.scopes or "").split(",") if s]


class AuditEntry(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[dt.datetime] = mapped_column(TS, default=now, index=True)
    actor: Mapped[str] = mapped_column(String(254), default="")
    action: Mapped[str] = mapped_column(String(100))
    target: Mapped[str] = mapped_column(String(254), default="")
    status: Mapped[str] = mapped_column(String(20), default="ok")
    detail: Mapped[str] = mapped_column(Text, default="")


class Setting(Base):
    """A runtime setting changed from the admin Settings page. A row OVERRIDES the environment
    variable of the same meaning; no row = the environment value applies. See app/settings.py."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict | list | str | int | bool | None] = mapped_column(JSONType, nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(TS, default=now)
    updated_by: Mapped[str] = mapped_column(String(254), default="")


# --- inbound -----------------------------------------------------------------------

class Integration(Base):
    """One inbound door with its own token: a Prometheus, an Alertmanager webhook, or a
    generic JSON sender. Alerts are scoped to the integration they arrived through."""

    __tablename__ = "integrations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    # prometheus | alertmanager | webhook
    kind: Mapped[str] = mapped_column(String(20))
    team_id: Mapped[str | None] = mapped_column(
        ForeignKey("teams.id", ondelete="SET NULL"), nullable=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    prefix: Mapped[str] = mapped_column(String(16))
    # Labels added to every alert from this door, e.g. {"cluster": "big"}. Lets two
    # Prometheus servers that do not set external_labels stay distinguishable.
    extra_labels: Mapped[dict] = mapped_column(JSONType, default=dict)
    # Overrides SIGNATURE_IGNORE_LABELS for this integration when set.
    signature_ignore: Mapped[list | None] = mapped_column(JSONType, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=now)
    last_received_at: Mapped[dt.datetime | None] = mapped_column(TS, nullable=True)
    last_watchdog_at: Mapped[dt.datetime | None] = mapped_column(TS, nullable=True)
    # Shadow comparison (kind=prometheus only): the Prometheus this integration receives
    # from, and the last result of asking it what is firing. See app/selfmon.py.
    prometheus_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    shadow_status: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    # Set when an integration that has alerts is deleted: it is archived instead, because its
    # occurrences are history and are never deleted. Its token no longer works and it is
    # hidden from the list; the alerts it delivered still name it.
    archived_at: Mapped[dt.datetime | None] = mapped_column(TS, nullable=True)
    # Shorthop Watch (relay SHOR-31): this integration's Watchdog is forwarded to a Watch check
    # on the push relay, so the chain Prometheus → Shorthop → network is watched from OUTSIDE
    # this installation. The ping URL is a credential (anyone holding it can keep a dead
    # check looking alive): write-only, never returned by the API, never logged.
    watch_check_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    watch_ping_url: Mapped[str | None] = mapped_column(String(300), nullable=True)
    watch_forwarded_at: Mapped[dt.datetime | None] = mapped_column(TS, nullable=True)


# --- alerts ------------------------------------------------------------------------

class Occurrence(Base):
    """One firing episode of one alert (fingerprint).

    status: firing → acknowledged → resolved. `resolved_at IS NULL` means open, and a
    partial unique index guarantees at most one OPEN occurrence per fingerprint — so an
    alert that resolves and fires again is a second row, and the history keeps both.
    """

    __tablename__ = "occurrences"
    __table_args__ = (
        Index(
            "uq_occurrence_open_fingerprint",
            "fingerprint",
            unique=True,
            postgresql_where=text("resolved_at IS NULL"),
            sqlite_where=text("resolved_at IS NULL"),
        ),
        Index("ix_occurrence_signature_started", "signature", "started_at"),
        Index("ix_occurrence_status", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    integration_id: Mapped[str] = mapped_column(
        ForeignKey("integrations.id", ondelete="RESTRICT")
    )
    team_id: Mapped[str | None] = mapped_column(
        ForeignKey("teams.id", ondelete="SET NULL"), nullable=True
    )
    fingerprint: Mapped[str] = mapped_column(String(64))
    signature: Mapped[str] = mapped_column(String(64))
    alertname: Mapped[str] = mapped_column(String(200), default="")
    severity: Mapped[str] = mapped_column(String(40), default="")
    labels: Mapped[dict] = mapped_column(JSONType, default=dict)
    # Labels a label rule set or changed: {label: rule name}. NULL/empty = as sent.
    rule_labels: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    signature_labels: Mapped[dict] = mapped_column(JSONType, default=dict)
    annotations: Mapped[dict] = mapped_column(JSONType, default=dict)
    generator_url: Mapped[str] = mapped_column(Text, default="")

    status: Mapped[str] = mapped_column(String(20), default="firing")
    started_at: Mapped[dt.datetime] = mapped_column(TS)
    last_seen_at: Mapped[dt.datetime] = mapped_column(TS)
    # When the SENDER says this stops being true unless re-sent. Prometheus stamps it a few
    # resend intervals into the future; the scheduler resolves anything past it.
    expires_at: Mapped[dt.datetime] = mapped_column(TS)
    acked_at: Mapped[dt.datetime | None] = mapped_column(TS, nullable=True)
    acked_by: Mapped[str | None] = mapped_column(String(254), nullable=True)
    resolved_at: Mapped[dt.datetime | None] = mapped_column(TS, nullable=True)
    # source (the sender said resolved) | timeout (stopped being sent) | manual
    resolved_how: Mapped[str | None] = mapped_column(String(20), nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(254), nullable=True)
    # "What fixed it?" — asked at resolve time, the input to the knowledge base.
    resolution_category: Mapped[str | None] = mapped_column(String(40), nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    send_count: Mapped[int] = mapped_column(Integer, default=1)
    # The incident (page) this alert belongs to — set by routing when it first fires.
    # NULL = matched no route, which is a legitimate state: recorded, never paged.
    incident_id: Mapped[str | None] = mapped_column(
        ForeignKey("incidents.id", ondelete="SET NULL"), nullable=True, index=True
    )

    events: Mapped[list["TimelineEvent"]] = relationship(
        back_populates="occurrence",
        cascade="all, delete-orphan",
        order_by="TimelineEvent.at",
    )


class TimelineEvent(Base):
    """Everything that happened to an occurrence, in order: fired, acked, note, resolved…"""

    __tablename__ = "timeline_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    occurrence_id: Mapped[str] = mapped_column(
        ForeignKey("occurrences.id", ondelete="CASCADE"), index=True
    )
    at: Mapped[dt.datetime] = mapped_column(TS, default=now)
    kind: Mapped[str] = mapped_column(String(30))
    actor: Mapped[str] = mapped_column(String(254), default="")
    detail: Mapped[str] = mapped_column(Text, default="")

    occurrence: Mapped[Occurrence] = relationship(back_populates="events")


class Silence(Base):
    """Matchers in Alertmanager's shape, so a silence written against Alertmanager reads
    the same here: [{"name": "alertname", "value": "X", "isRegex": false, "isEqual": true}].

    Expiring a silence sets `ends_at` to now; silences are never deleted, because "who muted
    this and why" is part of the record of what happened.
    """

    __tablename__ = "silences"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    matchers: Mapped[list] = mapped_column(JSONType)
    starts_at: Mapped[dt.datetime] = mapped_column(TS)
    ends_at: Mapped[dt.datetime] = mapped_column(TS, index=True)
    comment: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(254))
    team_id: Mapped[str | None] = mapped_column(
        ForeignKey("teams.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=now)
    expired_by: Mapped[str | None] = mapped_column(String(254), nullable=True)


# --- M2: routing, incidents, escalation, delivery ------------------------------------------

class EscalationPolicy(Base):
    """Who is told, in what order, while an incident stays unacknowledged.

    steps = [{"delay_minutes": 0, "targets": [{"type": "user"|"team"|"channel", "id": "…"}]}, …]
    Step 0 runs when the incident opens; each later step runs `delay_minutes` after the
    previous one, unless the incident was acknowledged or resolved in between. `repeat` runs
    the whole list again that many times.
    """

    __tablename__ = "escalation_policies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    team_id: Mapped[str | None] = mapped_column(
        ForeignKey("teams.id", ondelete="SET NULL"), nullable=True
    )
    steps: Mapped[list] = mapped_column(JSONType, default=list)
    repeat: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=now)


class LabelRule(Base):
    """Adds or changes labels on incoming alerts, before routing. Ordered (`position`), every
    matching rule applies top to bottom; `stop` ends evaluation. See app/core/enrich.py."""

    __tablename__ = "label_rules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    position: Mapped[int] = mapped_column(Integer, default=0, index=True)
    name: Mapped[str] = mapped_column(String(200))
    # Alertmanager-shape matchers, all must match. Empty = every alert (then a text pattern
    # usually narrows it).
    matchers: Mapped[list] = mapped_column(JSONType, default=list)
    # Case-insensitive regex SEARCHED in the alert text: one annotation (text_field) or all.
    text_pattern: Mapped[str | None] = mapped_column(String(300), nullable=True)
    text_field: Mapped[str | None] = mapped_column(String(100), nullable=True)
    set_labels: Mapped[dict] = mapped_column(JSONType, default=dict)
    integration_ids: Mapped[list | None] = mapped_column(JSONType, nullable=True)
    stop: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=now)


class Route(Base):
    """One rule in a FLAT ordered list: first enabled match wins. Deliberately not a tree —
    a deep tree is what made Alertmanager routing unreadable."""

    __tablename__ = "routes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    position: Mapped[int] = mapped_column(Integer, default=0, index=True)
    name: Mapped[str] = mapped_column(String(200))
    # Alertmanager-shape matchers, all must match. An empty list matches everything (the
    # catch-all at the bottom of the list).
    matchers: Mapped[list] = mapped_column(JSONType, default=list)
    # Restrict to these integrations; NULL = any.
    integration_ids: Mapped[list | None] = mapped_column(JSONType, nullable=True)
    team_id: Mapped[str | None] = mapped_column(
        ForeignKey("teams.id", ondelete="SET NULL"), nullable=True
    )
    escalation_policy_id: Mapped[str | None] = mapped_column(
        ForeignKey("escalation_policies.id", ondelete="SET NULL"), nullable=True
    )
    # Alerts with equal values of these labels are ONE incident (one page, not ten).
    group_by: Mapped[list] = mapped_column(JSONType, default=lambda: ["alertname"])
    # Tell the targets again when the incident resolves.
    notify_resolved: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=now)


class Incident(Base):
    """A group of alerts that pages as one. triggered → acknowledged → resolved.

    `next_step_at` is the durable timer: the scheduler runs the escalation step that is due
    and sets the next one. Kept in the row, so a restart in the middle of an incident
    continues where it was instead of forgetting to page the second person.
    """

    __tablename__ = "incidents"
    __table_args__ = (
        Index("uq_incident_open_group", "group_key", unique=True,
              postgresql_where=text("resolved_at IS NULL"),
              sqlite_where=text("resolved_at IS NULL")),
        Index("ix_incident_next_step", "next_step_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    number: Mapped[int] = mapped_column(Integer, default=0, index=True)  # human-friendly #
    route_id: Mapped[str | None] = mapped_column(
        ForeignKey("routes.id", ondelete="SET NULL"), nullable=True
    )
    team_id: Mapped[str | None] = mapped_column(
        ForeignKey("teams.id", ondelete="SET NULL"), nullable=True
    )
    escalation_policy_id: Mapped[str | None] = mapped_column(
        ForeignKey("escalation_policies.id", ondelete="SET NULL"), nullable=True
    )
    group_key: Mapped[str] = mapped_column(String(64))
    group_labels: Mapped[dict] = mapped_column(JSONType, default=dict)
    title: Mapped[str] = mapped_column(String(300))
    severity: Mapped[str] = mapped_column(String(40), default="")
    status: Mapped[str] = mapped_column(String(20), default="triggered")
    opened_at: Mapped[dt.datetime] = mapped_column(TS, default=now)
    acked_at: Mapped[dt.datetime | None] = mapped_column(TS, nullable=True)
    acked_by: Mapped[str | None] = mapped_column(String(254), nullable=True)
    resolved_at: Mapped[dt.datetime | None] = mapped_column(TS, nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(254), nullable=True)
    # Escalation state: which step runs next, which pass through the list, and when.
    next_step: Mapped[int] = mapped_column(Integer, default=0)
    pass_no: Mapped[int] = mapped_column(Integer, default=0)
    next_step_at: Mapped[dt.datetime | None] = mapped_column(TS, nullable=True)
    notify_resolved: Mapped[bool] = mapped_column(Boolean, default=True)
    # Set when a person moved it to another team (the latest move; the timeline has them all).
    # Carried in the page the new team gets, so "why is this mine?" is answered in the page.
    reassigned_at: Mapped[dt.datetime | None] = mapped_column(TS, nullable=True)
    reassigned_by: Mapped[str | None] = mapped_column(String(254), nullable=True)
    reassign_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Level (P1, P2 …; core/levels.py). Derived from the alerts until a person sets it; then
    # `level_manual` and it — and the severity it maps to — stops following the alerts.
    level: Mapped[str | None] = mapped_column(String(20), nullable=True)
    level_manual: Mapped[bool] = mapped_column(Boolean, default=False)
    level_set_by: Mapped[str | None] = mapped_column(String(254), nullable=True)
    level_set_at: Mapped[dt.datetime | None] = mapped_column(TS, nullable=True)

    alerts: Mapped[list["Occurrence"]] = relationship(foreign_keys="Occurrence.incident_id")


class Channel(Base):
    """A destination that is not a person: a shared mailbox or a webhook endpoint.

    kind=email:   config = {"to": ["ops@example.com", …]}
    kind=webhook: config = {"url": "https://…"}; `secret` signs every body (HMAC-SHA256).
    kind=slack:   config = {"channel_id": "C…", "channel_name": "ops-alerts"} — posted by the
                  Slack app (Settings → Slack), one message per incident, kept up to date.
    """

    __tablename__ = "channels"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    kind: Mapped[str] = mapped_column(String(20))
    team_id: Mapped[str | None] = mapped_column(
        ForeignKey("teams.id", ondelete="SET NULL"), nullable=True
    )
    config: Mapped[dict] = mapped_column(JSONType, default=dict)
    secret: Mapped[str | None] = mapped_column(String(100), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=now)


class Inhibition(Base):
    """While an alert matching `source` fires, alerts matching `target` that share the
    `equal` labels are not delivered (still recorded). The classic: a cluster-down alert
    suppressing every per-pod alert on that cluster."""

    __tablename__ = "inhibitions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200))
    source_matchers: Mapped[list] = mapped_column(JSONType)
    target_matchers: Mapped[list] = mapped_column(JSONType)
    equal: Mapped[list] = mapped_column(JSONType, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=now)


class Notification(Base):
    """The outbox AND the delivery log. A step writes `pending` rows in the same transaction
    that advances the incident; the delivery loop sends them and records the outcome. A
    crash between the two re-sends at most, never silently drops."""

    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notification_pending", "status", "next_attempt_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    incident_id: Mapped[str] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), index=True
    )
    event: Mapped[str] = mapped_column(String(20))  # triggered | escalated | resolved | test
    step: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kind: Mapped[str] = mapped_column(String(20))   # email | webhook | push | slack
    # Who/what it is for, for the log: "user ann@example.com", "channel ops-slack".
    target: Mapped[str] = mapped_column(String(300))
    destination: Mapped[str] = mapped_column(String(500))  # an address or a URL
    channel_id: Mapped[str | None] = mapped_column(
        ForeignKey("channels.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|sent|failed|suppressed
    # A page from a DELAYED contact rule: dropped at send time if the incident is no longer
    # triggered ("email me if I have not reacted in 5 minutes").
    until_ack: Mapped[bool] = mapped_column(Boolean, default=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[dt.datetime] = mapped_column(TS, default=now)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=now)
    sent_at: Mapped[dt.datetime | None] = mapped_column(TS, nullable=True)


class SlackMessage(Base):
    """The Slack message that stands for an incident in one conversation (a channel or a
    person's DM). Posted once, then UPDATED in place as the incident changes; what happens
    to it (acknowledged by, escalated, moved, notes) is a reply in its thread.

    `dirty` = the message no longer shows the incident as it is; the dispatch loop updates it
    (and posts `pending_replies` into the thread) — claimed with SKIP LOCKED like the outbox,
    so two replicas never post the same reply twice."""

    __tablename__ = "slack_messages"
    __table_args__ = (
        UniqueConstraint("incident_id", "conversation", name="uq_slack_message_conv"),
        Index("ix_slack_message_dirty", "dirty", "dirty_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    incident_id: Mapped[str] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), index=True
    )
    conversation: Mapped[str] = mapped_column(String(40))   # Slack channel / DM id (C…, D…)
    ts: Mapped[str] = mapped_column(String(40))             # the message's Slack timestamp
    channel_id: Mapped[str | None] = mapped_column(
        ForeignKey("channels.id", ondelete="SET NULL"), nullable=True
    )
    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    dirty: Mapped[bool] = mapped_column(Boolean, default=False)
    dirty_at: Mapped[dt.datetime] = mapped_column(TS, default=now)
    pending_replies: Mapped[list] = mapped_column(JSONType, default=list)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=now)
    updated_at: Mapped[dt.datetime | None] = mapped_column(TS, nullable=True)


# --- M3: on-call schedules ---------------------------------------------------------------

class Schedule(Base):
    """Layered rotations in one timezone; see core/oncall.py for the rules. Layers live as one
    JSON document because they are always read and written WHOLE with the schedule."""

    __tablename__ = "schedules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    team_id: Mapped[str | None] = mapped_column(
        ForeignKey("teams.id", ondelete="SET NULL"), nullable=True
    )
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    layers: Mapped[list] = mapped_column(JSONType, default=list)
    # Email the person coming ON call when the answer changes.
    handover_notify: Mapped[bool] = mapped_column(Boolean, default=True)
    # Who was on call at the last check — what a handover is detected against.
    last_on_call: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=now)

    overrides: Mapped[list["ScheduleOverride"]] = relationship(
        back_populates="schedule", cascade="all, delete-orphan"
    )


class ScheduleOverride(Base):
    """Somebody covers [starts_at, ends_at) — beats every layer; the newest override wins."""

    __tablename__ = "schedule_overrides"
    __table_args__ = (Index("ix_override_window", "schedule_id", "starts_at", "ends_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    schedule_id: Mapped[str] = mapped_column(ForeignKey("schedules.id", ondelete="CASCADE"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    starts_at: Mapped[dt.datetime] = mapped_column(TS)
    ends_at: Mapped[dt.datetime] = mapped_column(TS)
    reason: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(254))
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=now)

    schedule: Mapped[Schedule] = relationship(back_populates="overrides")
    user: Mapped[User] = relationship()


class CalendarFeed(Base):
    """An iCal subscription URL: "my on-call shifts" (kind=mine) or everyone on one schedule
    (kind=schedule). Calendar apps cannot send a bearer header, so the URL itself carries a
    secret (`shc_…`, stored hashed, shown once). Read-only; dies with its owner's access."""

    __tablename__ = "calendar_feeds"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # mine | schedule
    schedule_id: Mapped[str | None] = mapped_column(
        ForeignKey("schedules.id", ondelete="CASCADE"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(200))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    prefix: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=now)
    last_fetched_at: Mapped[dt.datetime | None] = mapped_column(TS, nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


# --- M4: phones --------------------------------------------------------------------------

class PairCode(Base):
    """A one-time code shown on the web ("Add a phone") and typed into the app. Stored hashed,
    expires in minutes, usable once."""

    __tablename__ = "pair_codes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    code_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[dt.datetime] = mapped_column(TS)
    used_at: Mapped[dt.datetime | None] = mapped_column(TS, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=now)


class Device(Base):
    """A paired phone. Its token `shd_…` acts as the user (never with config:write or admin),
    is revocable on its own, and carries the Expo push token the phone registered."""

    __tablename__ = "devices"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    platform: Mapped[str] = mapped_column(String(20), default="")
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    prefix: Mapped[str] = mapped_column(String(16))
    push_token: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=now)
    last_seen_at: Mapped[dt.datetime | None] = mapped_column(TS, nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    # Push relay (app/relay.py), only used when one is configured. NULL status = never tried.
    # registered: relay_device_id is this phone at the relay; refused: the relay would not
    # sign it in (relay_message says why — usually "add a payment method"); error: could not
    # ask (retried on the next page).
    relay_device_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    relay_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    relay_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # SHOR-10: the relay warned that this phone is unused and will be signed out at this time
    # unless the app is opened; NULL = no warning pending. relay_stale_notified_at = when we
    # emailed the owner + admins about it (once per warning).
    relay_signs_out_at: Mapped[dt.datetime | None] = mapped_column(TS, nullable=True)
    relay_stale_notified_at: Mapped[dt.datetime | None] = mapped_column(TS, nullable=True)

    user: Mapped[User] = relationship()


# --- shared rate limits ------------------------------------------------------------------

class RateWindow(Base):
    """A fixed-window counter shared by every replica (app/ratelimit.py). An in-process
    counter gives each replica its own budget, so two replicas double every limit.

    `key_hash` is a sha256 of the caller key (a client address), never the address itself.
    Rows older than the longest window are pruned by the leader's tick."""

    __tablename__ = "rate_windows"

    bucket: Mapped[str] = mapped_column(String(40), primary_key=True)
    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_start: Mapped[dt.datetime] = mapped_column(TS, primary_key=True)
    count: Mapped[int] = mapped_column(Integer, default=0)
