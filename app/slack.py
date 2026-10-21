"""The Slack app: incidents as live Slack messages you can act on.

What it does
  * A `slack` channel (or a person's "Slack" contact rule) posts ONE message per incident per
    conversation, and keeps it current: status, level, team, open alerts, the history line.
    Every change — from Slack, the web, the phone or MCP — redraws it, and says what happened
    in the message's thread ("Acknowledged by ann@…", "Escalated — step 2: …").
  * Buttons on it: Acknowledge · Resolve… · Level ▾ · Move team… · More ▾ (Escalate now,
    Add responder…, Snooze…, Add note…, Open in Shorthop). A resolved one keeps "What fixed
    it?" and "Add note".
  * A slash command, `/shorthop`: oncall, incidents, ack/resolve/level/escalate/snooze <#>.

How it connects — SOCKET MODE
  Shorthop opens an OUTBOUND WebSocket to Slack with the app-level token (xapp-…); clicks and
  commands arrive on it. No public URL, which is the point: this Shorthop is LAN/NetBird
  only. Every replica connects (Slack allows ten connections per app and delivers each event
  to ONE of them), so a pod going away loses nothing. The Web API (posting, updating) uses
  the bot token (xoxb-…). Both are admin settings (Settings → Slack), write-only.

Who is acting
  ⚠️ A click is done AS the Shorthop user whose email is the clicker's Slack email (looked up
  once, then remembered in users.slack_user_id). Their role decides what they may do —
  exactly as in the web UI — and a Slack user from ANOTHER workspace (a Slack Connect channel)
  is refused even when the email matches, because that workspace vouches for its own emails,
  not ours. Every action is audited `incident.<verb>` with "via Slack".

Messages are changed by the DISPATCH loop, never by the request that changed the incident:
`touch()` marks the incident's messages dirty in the caller's transaction (so a rolled-back
action leaves Slack alone), and `refresh_dirty()` claims them with SKIP LOCKED — two
replicas never post the same thread reply twice.
"""
import datetime as dt
import logging
import re
import threading
import time

from prometheus_client import Counter, Gauge
from sqlalchemy import select

from . import alerts as alert_ops
from . import config as cfg
from . import incidents
from . import settings as st
from .audit import record
from .auth import find_user_by_email, role_scopes, scopes_allow
from .core import levels as lv
from .db import SessionLocal
from .models import (
    Channel, EscalationPolicy, Incident, Notification, Schedule, SlackMessage, Team, User,
    aware, now,
)

logger = logging.getLogger(__name__)

ACTIONS = Counter("shorthop_slack_actions_total", "Slack button/modal/command actions, by outcome.",
                  ["action", "outcome"])
CONNECTED = Gauge("shorthop_slack_connected", "1 when this replica's Slack Socket Mode connection is up.")
CONNECTED.set(0)

TIMEOUT = 10
MAX_REFRESH_ATTEMPTS = 8
MAX_TEXT = 2900   # Slack's limit for a section's text is 3000


class SlackError(Exception):
    """Caller-safe: what Slack (or the lack of it) said."""


class NotAllowed(Exception):
    """Shown to the person who clicked, privately."""


# --- the Web API --------------------------------------------------------------------------

# Read methods take form/query parameters; the rest take a JSON body (blocks are nested).
_FORM_METHODS = {"auth.test", "users.info", "users.lookupByEmail", "conversations.list",
                 "conversations.info"}

_ERRORS = {
    "not_in_channel": "the app is not in that channel — invite it (/invite @Shorthop), or for a "
                      "public channel give the app the chat:write.public scope",
    "channel_not_found": "that channel does not exist, or it is private and the app is not in it",
    "invalid_auth": "the bot token was refused — check Settings → Slack",
    "not_authed": "no bot token — check Settings → Slack",
    "account_inactive": "the bot token belongs to an app that was uninstalled",
    "missing_scope": "the Slack app is missing a scope — reinstall it from the manifest in Settings → Slack",
    "users_not_found": "no Slack user has that email",
    "ratelimited": "Slack rate-limited the app; it is retried",
}


def api(method: str, **params) -> dict:
    """Call a Slack Web API method with the bot token. A seam: the tests replace it.
    Raises SlackError with a readable reason; the token never appears in it."""
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError, SlackClientError

    token = st.slack().bot_token
    if not token:
        raise SlackError("Slack is not set up (Settings → Slack: bot token)")
    client = WebClient(token=token, timeout=TIMEOUT)
    try:
        if method in _FORM_METHODS:
            resp = client.api_call(method, http_verb="GET", params=params)
        else:
            resp = client.api_call(method, json=params)
    except SlackApiError as e:
        code = (e.response.get("error") if e.response is not None else "") or "error"
        needed = e.response.get("needed") if e.response is not None else None
        msg = _ERRORS.get(code, code)
        if code == "missing_scope" and needed:
            msg += f" (needs {needed})"
        raise SlackError(msg) from None
    except SlackClientError as e:
        raise SlackError(f"could not reach Slack ({type(e).__name__})") from None
    except OSError as e:
        raise SlackError(f"could not reach Slack ({type(e).__name__})") from None
    return dict(resp.data) if hasattr(resp, "data") else dict(resp)


# --- rendering ----------------------------------------------------------------------------

def _url(inc: Incident) -> str:
    return f"{cfg.PUBLIC_URL}/incidents/{inc.id}" if cfg.PUBLIC_URL else ""


def _esc(text: str) -> str:
    """Slack's mrkdwn needs only these three escaped; anything from an alert goes through it,
    so a label cannot become a link or an @channel."""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _when(t: dt.datetime | None) -> str:
    if t is None:
        return ""
    ts = int(aware(t).timestamp())
    # Rendered in each reader's own time zone by Slack; the fallback is UTC.
    return f"<!date^{ts}^{{date_short_pretty}} {{time}}|{aware(t):%Y-%m-%d %H:%M} UTC>"


_STATUS = {"triggered": (":red_circle:", "Triggered"),
           "acknowledged": (":large_yellow_circle:", "Acknowledged"),
           "resolved": (":large_green_circle:", "Resolved")}


def _silenced_until(inc: Incident) -> dt.datetime | None:
    open_alerts = [o for o in inc.alerts if o.resolved_at is None]
    if not open_alerts:
        return None
    silences = alert_ops.active_silences()
    ends = []
    for o in open_alerts:
        hit = [aware(s.ends_at) for s in silences if s.id in alert_ops.silenced_by(o, silences)]
        if not hit:
            return None     # one live alert = not silenced
        ends.append(max(hit))
    return min(ends)


def render(inc: Incident) -> tuple[str, list[dict]]:
    """(fallback text, blocks) for the incident message."""
    db = SessionLocal
    emoji, status = _STATUS.get(inc.status, (":white_circle:", inc.status))
    title = f"#{inc.number} {_esc(inc.title)}"
    url = _url(inc)
    head = f"{emoji} *<{url}|{title}>*" if url else f"{emoji} *{title}*"
    team = db.get(Team, inc.team_id) if inc.team_id else None
    open_alerts = [o for o in inc.alerts if o.resolved_at is None]

    facts = []
    if inc.level:
        facts.append(f"*{_esc(inc.level)}*" + (" (set by hand)" if inc.level_manual else ""))
    if inc.severity:
        facts.append(_esc(inc.severity))
    facts.append(status + (f" by {_esc(inc.acked_by)}" if inc.status == "acknowledged" and inc.acked_by else "")
                 + (f" by {_esc(inc.resolved_by)}" if inc.status == "resolved" and inc.resolved_by else ""))
    facts.append(f"team {_esc(team.name)}" if team else "no team")
    facts.append(f"{len(open_alerts)} of {len(inc.alerts)} alert(s) open")
    facts.append(f"opened {_when(inc.opened_at)}")

    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": head[:MAX_TEXT]}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": " · ".join(facts)[:MAX_TEXT]}]},
    ]
    if inc.status == "triggered" and inc.next_step_at:
        blocks[1]["elements"].append({"type": "mrkdwn",
                                      "text": f"next escalation {_when(inc.next_step_at)}"})
    silenced = _silenced_until(inc) if inc.resolved_at is None else None
    if silenced:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
                       "text": f":zzz: Silenced until {_when(silenced)} — escalation is on hold"}]})
    if inc.reassigned_at:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": _esc(
            f"Moved by {inc.reassigned_by} {inc.reassign_note or ''}".strip())[:MAX_TEXT]}]})

    lines = []
    for o in (open_alerts or inc.alerts)[:5]:
        summ = (o.annotations or {}).get("summary") or (o.annotations or {}).get("description") or ""
        state = "" if o.resolved_at is None else " _(resolved)_"
        line = f"• *{_esc(o.alertname)}*{state}" + (f" — {_esc(summ)[:300]}" if summ else "")
        runbook = (o.annotations or {}).get("runbook_url")
        if runbook and re.match(r"^https?://", runbook):
            line += f" <{runbook}|runbook>"
        lines.append(line)
    more = len(open_alerts or inc.alerts) - 5
    if more > 0:
        lines.append(f"…and {more} more")
    if lines:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)[:MAX_TEXT]}})

    from .delivery import _history_line  # the differentiator, on the message itself
    hist = _history_line(inc)
    if hist:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
                       "text": ":books: " + _esc(hist)[:MAX_TEXT]}]})

    blocks.append(_actions(inc))
    text = f"[{status}] #{inc.number} {inc.title}" + (f" ({inc.level})" if inc.level else "")
    return text[:300], blocks


def _option(text: str, value: str) -> dict:
    return {"text": {"type": "plain_text", "text": text[:75]}, "value": value[:150]}


def _actions(inc: Incident) -> dict:
    iid = inc.id
    if inc.resolved_at is not None:
        elements = [
            {"type": "button", "action_id": "sh_resolve", "value": iid,
             "text": {"type": "plain_text", "text": "What fixed it?"}},
            {"type": "button", "action_id": "sh_note", "value": iid,
             "text": {"type": "plain_text", "text": "Add note"}},
        ]
        if _url(inc):
            elements.append({"type": "button", "action_id": "sh_open", "url": _url(inc),
                             "text": {"type": "plain_text", "text": "Open in Shorthop"}})
        return {"type": "actions", "block_id": f"sh:{iid}", "elements": elements}

    elements = []
    if inc.status == "triggered":
        elements.append({"type": "button", "action_id": "sh_ack", "value": iid, "style": "primary",
                         "text": {"type": "plain_text", "text": "Acknowledge"}})
    elements.append({"type": "button", "action_id": "sh_resolve", "value": iid,
                     "text": {"type": "plain_text", "text": "Resolve…"}})
    levels = st.incident_levels()
    opts = [_option(f"{x['name']} · {x['severity']}", f"{x['name']}|{iid}") for x in levels]
    sel = {"type": "static_select", "action_id": "sh_level", "options": opts,
           "placeholder": {"type": "plain_text", "text": "Level"}}
    current = next((o for o in opts if o["value"].split("|")[0] == inc.level), None)
    if current:
        sel["initial_option"] = current
    elements.append(sel)
    elements.append({"type": "button", "action_id": "sh_move", "value": iid,
                     "text": {"type": "plain_text", "text": "Move team…"}})
    more = []
    if inc.status == "triggered" and inc.next_step_at:
        more.append(_option("Escalate now", f"escalate|{iid}"))
    more += [_option("Add responder…", f"responder|{iid}"),
             _option("Snooze…", f"snooze|{iid}"),
             _option("Add note…", f"note|{iid}")]
    if _url(inc):
        o = _option("Open in Shorthop", f"open|{iid}")
        o["url"] = _url(inc)
        more.append(o)
    elements.append({"type": "overflow", "action_id": "sh_more", "options": more[:5]})
    return {"type": "actions", "block_id": f"sh:{iid}", "elements": elements}


# --- marking messages for a redraw (called from incidents.py) -------------------------------

def touch(db, incident_id: str, reply: str | None = None) -> None:
    """Mark the incident's Slack messages for a redraw, with `reply` for their threads.

    ⚠️ Locks the rows (FOR UPDATE): a refresh holding one is posting its pending replies, and
    without the lock this would append to the list it read before the refresh cleared it —
    re-posting replies already in the thread."""
    rows = db.scalars(select(SlackMessage).where(SlackMessage.incident_id == incident_id)
                      .with_for_update()).all()
    for r in rows:
        r.dirty, r.dirty_at = True, now()
        if reply:
            r.pending_replies = list(r.pending_replies or []) + [reply[:MAX_TEXT]]


# --- sending (the outbox calls this for kind=slack) ---------------------------------------

def _slack_user_id(db, user: User) -> str:
    if user.slack_user_id:
        return user.slack_user_id
    found = api("users.lookupByEmail", email=user.email)
    sid = (found.get("user") or {}).get("id")
    if not sid:
        raise SlackError(f"no Slack user has the email {user.email}")
    user.slack_user_id = sid
    return sid


def _conversation(db, n: Notification) -> tuple[str, str | None]:
    """(Slack conversation id, Shorthop user id for a DM)."""
    if n.destination.startswith("user:"):
        user = db.get(User, n.destination[5:])
        if user is None or user.disabled:
            raise SlackError("that person no longer exists or is disabled")
        opened = api("conversations.open", users=_slack_user_id(db, user))
        conv = (opened.get("channel") or {}).get("id")
        if not conv:
            raise SlackError("Slack did not open a DM")
        return conv, user.id
    ch = db.get(Channel, n.channel_id) if n.channel_id else None
    if ch is None:
        raise SlackError("the channel no longer exists")
    conv = (ch.config or {}).get("channel_id")
    if not conv:
        raise SlackError("the channel has no Slack channel set")
    return conv, None


_PAGED_AGAIN = {"triggered": ":bell: You are paged about this incident",
                "escalated": ":rotating_light: Still not acknowledged — you are paged again"}


def deliver(db, n: Notification, inc: Incident) -> None:
    conv, user_id = _conversation(db, n)
    if n.event == "test":
        api("chat.postMessage", channel=conv, text=f":wave: A test from Shorthop — this is where "
            f"its incidents will appear.{' Sent by ' + n.target if n.target else ''}")
        return
    msg = db.scalar(select(SlackMessage).where(SlackMessage.incident_id == inc.id,
                                               SlackMessage.conversation == conv).with_for_update())
    text, blocks = render(inc)
    if msg is None:
        posted = api("chat.postMessage", channel=conv, text=text, blocks=blocks,
                     unfurl_links=False, unfurl_media=False)
        db.add(SlackMessage(incident_id=inc.id, conversation=posted.get("channel") or conv,
                            ts=posted["ts"], channel_id=n.channel_id, user_id=user_id,
                            pending_replies=[], updated_at=now()))
        return
    api("chat.update", channel=conv, ts=msg.ts, text=text, blocks=blocks)
    msg.updated_at = now()
    if not msg.pending_replies:
        msg.dirty = False  # replies still queued: the redraw loop posts them
    if user_id and n.event in _PAGED_AGAIN:
        # A DM thread reply does not notify; broadcast it so the person is actually paged.
        api("chat.postMessage", channel=conv, thread_ts=msg.ts, reply_broadcast=True,
            text=_PAGED_AGAIN[n.event])


def send_test(channel: Channel, actor: str) -> None:
    conv = (channel.config or {}).get("channel_id")
    if not conv:
        raise SlackError("the channel has no Slack channel set")
    api("chat.postMessage", channel=conv,
        text=f":wave: A test from Shorthop channel *{_esc(channel.name)}*, sent by {_esc(actor)}. "
             "Incidents routed here will appear like this, with buttons to act on them.")


# --- the redraw loop (dispatch loop, every replica) -------------------------------------------

def _claim_dirty(db, t: dt.datetime, skip: set[str]) -> SlackMessage | None:
    return db.scalar(select(SlackMessage).where(
        SlackMessage.dirty.is_(True), SlackMessage.dirty_at <= t,
        SlackMessage.id.not_in(skip or {""}))
        .order_by(SlackMessage.dirty_at).limit(1).with_for_update(skip_locked=True))


def refresh_dirty(limit: int = 30) -> int:
    """Post pending thread replies and redraw every message that is out of date. One claimed
    row per transaction. A failure backs off (30 s × 2ⁿ, ≤ 10 min) and keeps the replies not
    yet posted; after MAX_REFRESH_ATTEMPTS it stops with the error recorded."""
    if not st.slack().bot_token:
        return 0
    db = SessionLocal
    t = now()
    handled: set[str] = set()
    while len(handled) < limit:
        m = _claim_dirty(db, t, handled)
        if m is None:
            db.rollback()
            break
        handled.add(m.id)
        inc = db.get(Incident, m.incident_id)
        pending = list(m.pending_replies or [])
        try:
            while pending:
                api("chat.postMessage", channel=m.conversation, thread_ts=m.ts, text=pending[0])
                pending.pop(0)
            text, blocks = render(inc)
            api("chat.update", channel=m.conversation, ts=m.ts, text=text, blocks=blocks)
            m.dirty, m.attempts, m.error, m.updated_at = False, 0, None, now()
        except SlackError as e:
            m.attempts += 1
            m.error = str(e)
            if m.attempts >= MAX_REFRESH_ATTEMPTS:
                m.dirty = False
                logger.warning("slack message %s: giving up: %s", m.id, e)
            else:
                m.dirty_at = now() + dt.timedelta(seconds=min(600, 30 * 2 ** (m.attempts - 1)))
        except Exception:  # noqa: BLE001 — one broken message must not stop the others
            logger.exception("slack message %s: unexpected error", m.id)
            m.attempts += 1
            m.dirty_at = now() + dt.timedelta(seconds=60)
        m.pending_replies = pending
        try:
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception("slack message %s: could not record the outcome", m.id)
    return len(handled)


# --- who clicked ------------------------------------------------------------------------------

_state_lock = threading.Lock()
_state: dict = {"client": None, "tokens": None, "team_id": None, "team": None, "bot_user": None,
                "error": None, "since": None}


def _installed_team() -> str | None:
    with _state_lock:
        if _state["team_id"]:
            return _state["team_id"]
    try:
        info = api("auth.test")
    except SlackError:
        return None
    with _state_lock:
        _state["team_id"], _state["team"] = info.get("team_id"), info.get("team")
        _state["bot_user"] = info.get("user")
    return info.get("team_id")


def identify(slack_user_id: str, slack_team_id: str | None = None) -> User:
    """The Shorthop user behind a Slack user, or NotAllowed with what to do about it."""
    db = SessionLocal
    if not slack_user_id or not re.fullmatch(r"[UW][A-Z0-9]{2,30}", slack_user_id):
        raise NotAllowed("Shorthop could not tell who you are in Slack")
    team = _installed_team()
    if slack_team_id and team and slack_team_id != team:
        raise NotAllowed("you are in another Slack workspace — act on this in Shorthop instead")
    u = db.scalar(select(User).where(User.slack_user_id == slack_user_id))
    if u is None:
        try:
            info = (api("users.info", user=slack_user_id).get("user") or {})
        except SlackError as e:
            raise NotAllowed(f"Shorthop could not look you up in Slack: {e}") from None
        if team and info.get("team_id") and info["team_id"] != team:
            raise NotAllowed("you are in another Slack workspace — act on this in Shorthop instead")
        if info.get("is_bot") or info.get("deleted"):
            raise NotAllowed("bots and deactivated users cannot act on incidents")
        email = ((info.get("profile") or {}).get("email") or "").strip().lower()
        if not email:
            raise NotAllowed("Shorthop could not read your Slack email (the app needs the "
                             "users:read.email scope — see Settings → Slack)")
        u = find_user_by_email(email)
        if u is None:
            raise NotAllowed(f"{email} is not a Shorthop user — ask an admin to invite you "
                             "(People → Invite), then try again")
        u.slack_user_id = slack_user_id
        db.commit()
    if u.disabled:
        raise NotAllowed("your Shorthop account is disabled")
    return u


def _need(user: User, scope: str) -> None:
    if not scopes_allow(role_scopes(user.role), scope):
        raise NotAllowed(f"your Shorthop role ({user.role}) may not do that — it needs {scope}")


# --- interactivity -------------------------------------------------------------------------

def _ephemeral(payload: dict, text: str) -> None:
    """Tell only the person who clicked. Best effort."""
    channel = ((payload.get("channel") or {}).get("id")
               or (payload.get("container") or {}).get("channel_id"))
    user = (payload.get("user") or {}).get("id")
    if not channel or not user:
        return
    try:
        api("chat.postEphemeral", channel=channel, user=user, text=text[:MAX_TEXT])
    except SlackError as e:
        logger.info("slack: could not tell %s privately: %s", user, e)


def _input(block_id: str, label: str, element: dict, optional: bool = False, hint: str = "") -> dict:
    b = {"type": "input", "block_id": block_id, "optional": optional,
         "label": {"type": "plain_text", "text": label}, "element": element}
    if hint:
        b["hint"] = {"type": "plain_text", "text": hint[:150]}
    return b


def _text(action_id: str, multiline: bool = False, initial: str = "", max_length: int = 2000) -> dict:
    e = {"type": "plain_text_input", "action_id": action_id, "multiline": multiline,
         "max_length": max_length}
    if initial:
        e["initial_value"] = initial
    return e


def _select(action_id: str, options: list[dict], placeholder: str, initial: str | None = None) -> dict:
    e = {"type": "static_select", "action_id": action_id, "options": options[:100],
         "placeholder": {"type": "plain_text", "text": placeholder}}
    init = next((o for o in options if o["value"] == initial), None) if initial else None
    if init:
        e["initial_option"] = init
    return e


def _modal(callback_id: str, inc: Incident, title: str, submit: str, blocks: list[dict]) -> dict:
    return {"type": "modal", "callback_id": callback_id, "private_metadata": inc.id,
            "title": {"type": "plain_text", "text": title[:24]},
            "submit": {"type": "plain_text", "text": submit[:24]},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [{"type": "context", "elements": [{"type": "mrkdwn",
                        "text": f"*#{inc.number}* {_esc(inc.title)}"[:MAX_TEXT]}]}] + blocks}


def _resolve_modal(inc: Incident) -> dict:
    cats = [_option(c.replace("_", " "), c) for c in alert_ops.RESOLUTION_CATEGORIES]
    done = inc.resolved_at is not None
    return _modal("sh_resolve", inc, "What fixed it?" if done else "Resolve incident",
                  "Save" if done else "Resolve", [
        _input("category", "What kind of fix", _select("v", cats, "Choose…"), optional=True),
        _input("note", "What fixed it", _text("v", multiline=True, max_length=4000), optional=True,
               hint="The next person paged for this alert sees it. Worth two sentences."),
    ])


def _move_modal(inc: Incident) -> dict:
    db = SessionLocal
    teams = db.scalars(select(Team).order_by(Team.name)).all()
    policies = db.scalars(select(EscalationPolicy).order_by(EscalationPolicy.name)).all()
    team_names = {t.id: t.name for t in teams}
    return _modal("sh_move", inc, "Move to another team", "Move", [
        _input("team", "Team", _select("v", [_option(t.name, t.id) for t in teams], "Choose a team")),
        _input("policy", "Escalation policy",
               _select("v", [_option(p.name + (f" ({team_names[p.team_id]})" if p.team_id in team_names else ""),
                                     p.id) for p in policies], "The team's own"), optional=True,
               hint="Empty = the team's own policy, when it has exactly one."),
        _input("note", "Why", _text("v", multiline=True), optional=True),
        _input("page", "Paging", {"type": "checkboxes", "action_id": "v", "options": [
            _option("Page the new team now (even if acknowledged)", "page")],
            "initial_options": [_option("Page the new team now (even if acknowledged)", "page")]},
            optional=True),
    ])


def _responder_modal(inc: Incident) -> dict:
    db = SessionLocal
    people = db.scalars(select(User).where(User.disabled.is_(False)).order_by(User.name, User.email)).all()
    schedules = db.scalars(select(Schedule).order_by(Schedule.name)).all()
    return _modal("sh_responder", inc, "Add a responder", "Page them", [
        _input("user", "A person", _select("v", [_option(u.name or u.email, u.id) for u in people],
                                            "Choose someone"), optional=True),
        _input("schedule", "…or whoever is on call",
               _select("v", [_option(s.name, s.id) for s in schedules], "Choose a schedule"),
               optional=True),
        _input("note", "Why (in the page)", _text("v", multiline=True), optional=True),
    ])


_SNOOZES = [("30 minutes", 30), ("1 hour", 60), ("2 hours", 120), ("4 hours", 240),
            ("8 hours", 480), ("24 hours", 1440)]


def _snooze_modal(inc: Incident) -> dict:
    m = incidents.snooze_matchers(inc)
    shown = ", ".join(f"{x['name']}={x['value']}" for x in m) or "(nothing to silence on)"
    return _modal("sh_snooze", inc, "Snooze incident", "Snooze", [
        _input("minutes", "For", _select("v", [_option(a, str(b)) for a, b in _SNOOZES], "How long",
                                         initial="60")),
        _input("comment", "Why", _text("v", initial=f"snoozed incident #{inc.number} from Slack"),
               hint="The next person needs to know why this is quiet."),
        {"type": "context", "elements": [{"type": "mrkdwn",
         "text": f"Silences alerts matching `{_esc(shown)[:500]}` — escalation holds and resumes "
                 "when the silence ends."}]},
    ])


def _note_modal(inc: Incident) -> dict:
    return _modal("sh_note", inc, "Add a note", "Add", [
        _input("note", "Note", _text("v", multiline=True, max_length=2900),
               hint="Goes on the incident's timeline in Shorthop and into this thread."),
    ])


_MODALS = {"resolve": _resolve_modal, "move": _move_modal, "responder": _responder_modal,
           "snooze": _snooze_modal, "note": _note_modal}


def _open_modal(payload: dict, kind: str, inc: Incident) -> None:
    api("views.open", trigger_id=payload.get("trigger_id"), view=_MODALS[kind](inc))


def _who(payload: dict) -> User:
    user = payload.get("user") or {}
    return identify(user.get("id"), user.get("team_id") or (payload.get("team") or {}).get("id"))


def handle_block_action(payload: dict) -> None:
    """A click on an incident message. The socket ack has already been sent."""
    action = (payload.get("actions") or [{}])[0]
    aid = action.get("action_id", "")
    if aid == "sh_open":
        return  # a link button; Slack opens the URL
    value = action.get("value") or (action.get("selected_option") or {}).get("value") or ""
    try:
        if aid == "sh_level":
            level, _, iid = value.partition("|")
            verb = "level"
        elif aid == "sh_more":
            verb, _, iid = value.partition("|")
            if verb == "open":
                return
        else:
            verb, iid = aid.removeprefix("sh_"), value
        user = _who(payload)
        _need(user, "alerts:write")
        inc = incidents.get(iid)
        if verb in ("resolve", "move", "responder", "snooze", "note"):
            _open_modal(payload, verb, inc)          # trigger_id is good for 3 s: open first
            ACTIONS.labels(verb, "modal").inc()
            return
        if verb == "ack":
            incidents.acknowledge(iid, user.email)
        elif verb == "level":
            incidents.set_level(iid, user.email, level)
        elif verb == "escalate":
            incidents.escalate_now(iid, user.email)
        else:
            return
        record(user.email, f"incident.{verb}", target=iid, detail="via Slack")
        ACTIONS.labels(verb, "ok").inc()
    except NotAllowed as e:
        ACTIONS.labels(aid, "refused").inc()
        _ephemeral(payload, f":no_entry: {e}")
    except (alert_ops.AlertError, LookupError) as e:
        ACTIONS.labels(aid, "error").inc()
        _ephemeral(payload, f":warning: {e}")
        if isinstance(e, alert_ops.AlertError):
            # The message may be stale (someone else acted first) — redraw it.
            try:
                touch(SessionLocal, iid)
                SessionLocal.commit()
            except Exception:  # noqa: BLE001
                SessionLocal.rollback()
    except SlackError as e:
        ACTIONS.labels(aid, "error").inc()
        _ephemeral(payload, f":warning: Slack: {e}")


def _value(state: dict, block: str):
    v = ((state.get(block) or {}).get("v") or {})
    if v.get("type") == "static_select":
        return (v.get("selected_option") or {}).get("value")
    if v.get("type") == "checkboxes":
        return [o.get("value") for o in v.get("selected_options") or []]
    return (v.get("value") or "").strip() or None


def handle_view_submission(payload: dict) -> dict | None:
    """A modal's Submit. Runs BEFORE the ack, because a refusal has to be the ack itself
    ({"response_action": "errors"}) for the modal to stay open and show it."""
    view = payload.get("view") or {}
    cb = view.get("callback_id", "")
    iid = view.get("private_metadata", "")
    state = (view.get("state") or {}).get("values") or {}
    verb = cb.removeprefix("sh_")
    first_block = {"resolve": "note", "move": "team", "responder": "user", "snooze": "comment",
                   "note": "note"}.get(verb, "note")

    def err(text: str, block: str = first_block) -> dict:
        ACTIONS.labels(verb, "error").inc()
        return {"response_action": "errors", "errors": {block: text[:150]}}
    try:
        user = _who(payload)
        _need(user, "alerts:write")
        if verb == "resolve":
            incidents.resolve(iid, user.email, category=_value(state, "category"),
                              note=_value(state, "note"))
        elif verb == "move":
            incidents.reassign(iid, user.email, _value(state, "team"), _value(state, "policy"),
                               note=_value(state, "note"), page="page" in (_value(state, "page") or []))
        elif verb == "responder":
            uid, sid = _value(state, "user"), _value(state, "schedule")
            if bool(uid) == bool(sid):
                return err("Choose one person or one schedule", "user")
            incidents.add_responder(iid, user.email, user_id=uid, schedule_id=sid,
                                    note=_value(state, "note"))
        elif verb == "snooze":
            incidents.snooze(iid, user.email, int(_value(state, "minutes") or 60),
                             comment=_value(state, "comment"))
        elif verb == "note":
            incidents.add_note(iid, user.email, _value(state, "note") or "")
        else:
            return None
        record(user.email, f"incident.{verb}", target=iid, detail="via Slack")
        ACTIONS.labels(verb, "ok").inc()
        return None
    except NotAllowed as e:
        return err(str(e))
    except (alert_ops.AlertError, LookupError, ValueError) as e:
        return err(str(e))
    except SlackError as e:
        return err(f"Slack: {e}")


# --- the slash command -----------------------------------------------------------------------

HELP = (f"*{cfg.SLACK_COMMAND}* — Shorthop in Slack\n"
        f"• `{cfg.SLACK_COMMAND} oncall [schedule]` — who is on call now\n"
        f"• `{cfg.SLACK_COMMAND} ping techops [P2] what you need` — open an incident for a team "
        f"and page them now\n"
        f"• `{cfg.SLACK_COMMAND} incidents` — open incidents\n"
        f"• `{cfg.SLACK_COMMAND} show 42` — post incident #42 here, with its buttons\n"
        f"• `{cfg.SLACK_COMMAND} ack 42` · `resolve 42 [what fixed it]` · `level 42 P1` · "
        f"`escalate 42` · `snooze 42 2h` · `note 42 text`")

_DURATION = re.compile(r"^(\d{1,4})\s*(m|min|h|hr|hours?)?$", re.I)


def _by_number(text: str) -> Incident:
    n = text.lstrip("#")
    if not n.isdigit():
        raise alert_ops.AlertError("give the incident number, e.g. 42")
    inc = SessionLocal.scalar(select(Incident).where(Incident.number == int(n)))
    if inc is None:
        raise LookupError(f"no incident #{n}")
    return inc


def _oncall(arg: str) -> str:
    from . import schedules
    rows = SessionLocal.scalars(select(Schedule).order_by(Schedule.name)).all()
    if arg:
        rows = [s for s in rows if arg.lower() in s.name.lower()]
    if not rows:
        return "No schedule matches." if arg else "No schedules yet."
    out = []
    t = now()
    for s in rows[:20]:
        uid = schedules.on_call_now(s, t)["user_id"]
        u = SessionLocal.get(User, uid) if uid else None
        who = (f"<@{u.slack_user_id}>" if u and u.slack_user_id else _esc(u.name or u.email)) if u \
            else ":warning: *nobody*"
        until = schedules.next_change(s, t)
        out.append(f"• *{_esc(s.name)}*: {who}" + (f" until {_when(until)}" if until else ""))
    return "\n".join(out)


def _incident_list() -> str:
    rows = SessionLocal.scalars(select(Incident).where(Incident.resolved_at.is_(None))
                                .order_by(Incident.opened_at.desc()).limit(15)).all()
    if not rows:
        return ":large_green_circle: No open incidents."
    lines = []
    for i in rows:
        emoji = _STATUS.get(i.status, (":white_circle:", ""))[0]
        url = _url(i)
        name = f"#{i.number} {_esc(i.title)}"
        lines.append(f"{emoji} " + (f"<{url}|{name}>" if url else name)
                     + (f" · *{_esc(i.level)}*" if i.level else "")
                     + (f" · acked by {_esc(i.acked_by)}" if i.acked_by else ""))
    return "\n".join(lines)


def _teams_line() -> str:
    teams = SessionLocal.scalars(select(Team).order_by(Team.slug)).all()
    return ", ".join(f"`{t.slug}`" for t in teams) or "(no teams yet)"


def _post_live(inc: Incident, channel: str | None) -> bool:
    """Post the incident's live message into `channel` (the one the command came from), so
    the conversation about it happens where it was asked for. False when Slack refused."""
    if not channel:
        return False
    if SessionLocal.scalar(select(SlackMessage).where(SlackMessage.incident_id == inc.id,
                                                      SlackMessage.conversation == channel)):
        return True
    t, blocks = render(inc)
    try:
        posted = api("chat.postMessage", channel=channel, text=t, blocks=blocks, unfurl_links=False)
    except SlackError as e:
        logger.info("slack: could not post #%s into %s: %s", inc.number, channel, e)
        return False
    SessionLocal.add(SlackMessage(incident_id=inc.id, conversation=posted.get("channel") or channel,
                                  ts=posted["ts"], pending_replies=[], updated_at=now()))
    SessionLocal.commit()
    return True


def _ping(user: User, args: list[str], payload: dict) -> dict:
    """`ping <team> [level] <what you need>` — a new incident for that team, paged now."""
    def reply(t: str) -> dict:
        return {"response_type": "ephemeral", "text": t[:MAX_TEXT]}
    _need(user, "alerts:write")
    if not args:
        return reply(f"Which team? `{cfg.SLACK_COMMAND} ping <team> [level] what you need` — teams: {_teams_line()}")
    team = incidents.find_team(args[0])
    if team is None:
        return reply(f"No team `{_esc(args[0])}` — teams: {_teams_line()}")
    rest = args[1:]
    level = rest[0] if rest and lv.find(st.incident_levels(), rest[0]) else None
    if level:
        rest = rest[1:]
    message = " ".join(rest).strip()
    if not message:
        return reply(f"Say what you need: `{cfg.SLACK_COMMAND} ping {team.slug} the database is down`")
    channel = payload.get("channel_id")
    where = f"Slack #{payload['channel_name']}" if payload.get("channel_name") else "Slack"
    inc = incidents.page_team(team.id, user.email, message, level=level, via=where)
    record(user.email, "incident.ping", target=inc.id, detail=f"{team.slug}: {message[:200]} (via Slack)")
    ACTIONS.labels("cmd_ping", "ok").inc()
    posted = _post_live(inc, channel)
    url = _url(inc)
    name = f"#{inc.number}"
    link = f"<{url}|{name}>" if url else name
    return reply(f":sos: Paging *{_esc(team.name)}* now ({inc.level}) — incident {link}."
                 + ("" if posted else " (Could not post it in this channel — invite the app here "
                    "to follow it in the channel.)"))


def handle_command(payload: dict) -> dict:
    """`/shorthop …` — answered privately (ephemeral), except `show`, which posts the
    incident's live message into the channel."""
    text = (payload.get("text") or "").strip()
    parts = text.split()
    sub = parts[0].lower() if parts else "help"
    rest = parts[1:]

    def reply(t: str) -> dict:
        return {"response_type": "ephemeral", "text": t[:MAX_TEXT]}
    try:
        if sub in ("help", "?"):
            return reply(HELP)
        user = identify(payload.get("user_id"), payload.get("team_id"))
        if sub in ("oncall", "on-call", "who"):
            _need(user, "oncall:read")
            return reply(_oncall(" ".join(rest)))
        if sub in ("incidents", "open", "list"):
            _need(user, "alerts:read")
            return reply(_incident_list())
        if sub in ("ping", "page", "help-me", "sos"):
            return _ping(user, rest, payload)
        if not rest:
            return reply(HELP)
        inc = _by_number(rest[0])
        if sub == "show":
            _need(user, "alerts:read")
            channel = payload.get("channel_id")
            existing = SessionLocal.scalar(select(SlackMessage).where(
                SlackMessage.incident_id == inc.id, SlackMessage.conversation == channel))
            if existing:
                touch(SessionLocal, inc.id)
                SessionLocal.commit()
                return reply(f"#{inc.number} is already in this channel — it was redrawn.")
            t, blocks = render(inc)
            posted = api("chat.postMessage", channel=channel, text=t, blocks=blocks,
                         unfurl_links=False)
            SessionLocal.add(SlackMessage(incident_id=inc.id, conversation=posted.get("channel") or channel,
                                          ts=posted["ts"], pending_replies=[], updated_at=now()))
            SessionLocal.commit()
            return {"response_type": "ephemeral", "text": "Posted."}
        _need(user, "alerts:write")
        if sub in ("ack", "acknowledge"):
            incidents.acknowledge(inc.id, user.email)
            done = "acknowledged"
        elif sub == "resolve":
            note = " ".join(rest[1:]) or None
            incidents.resolve(inc.id, user.email, note=note)
            done = "resolved"
        elif sub == "level":
            if len(rest) < 2:
                return reply("Which level? " + ", ".join(x["name"] for x in st.incident_levels()))
            incidents.set_level(inc.id, user.email, rest[1])
            done = f"set to {rest[1]}"
        elif sub == "escalate":
            incidents.escalate_now(inc.id, user.email)
            done = "escalated — the next step runs now"
        elif sub == "snooze":
            m = _DURATION.match(rest[1] if len(rest) > 1 else "1h")
            if not m:
                return reply("For how long? e.g. `snooze 42 30m` or `snooze 42 2h`")
            minutes = int(m.group(1)) * (60 if (m.group(2) or "h").lower().startswith("h") else 1)
            incidents.snooze(inc.id, user.email, minutes, comment=" ".join(rest[2:]) or None)
            done = f"snoozed for {rest[1] if len(rest) > 1 else '1h'}"
        elif sub == "note":
            incidents.add_note(inc.id, user.email, " ".join(rest[1:]))
            done = "noted"
        else:
            return reply(HELP)
        record(user.email, f"incident.{sub}", target=inc.id, detail="via Slack command")
        ACTIONS.labels(f"cmd_{sub}", "ok").inc()
        return reply(f"#{inc.number} {done}.")
    except NotAllowed as e:
        ACTIONS.labels(f"cmd_{sub}", "refused").inc()
        return reply(f":no_entry: {e}")
    except (alert_ops.AlertError, LookupError) as e:
        ACTIONS.labels(f"cmd_{sub}", "error").inc()
        return reply(f":warning: {e}")
    except SlackError as e:
        return reply(f":warning: Slack: {e}")


# --- Socket Mode ------------------------------------------------------------------------------

def handle_request(req_type: str, payload: dict) -> dict | None:
    """One Socket Mode envelope → the payload to ack with (or None). The DB session is the
    thread's own and is removed afterwards — these run on the socket client's thread pool."""
    try:
        if req_type == "slash_commands":
            return handle_command(payload)
        if req_type == "interactive":
            kind = payload.get("type")
            if kind == "view_submission":
                return handle_view_submission(payload)
        return None
    finally:
        SessionLocal.remove()


def _listener(client, req) -> None:
    from slack_sdk.socket_mode.response import SocketModeResponse

    payload = req.payload or {}
    if req.type == "interactive" and payload.get("type") == "block_actions":
        # Ack at once (Slack wants it within 3 s), then work: opening a modal, a DB write.
        client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        try:
            handle_block_action(payload)
        except Exception:  # noqa: BLE001
            logger.exception("slack: block action failed")
        finally:
            SessionLocal.remove()
        return
    try:
        body = handle_request(req.type, payload)
    except Exception:  # noqa: BLE001
        logger.exception("slack: %s failed", req.type)
        body = ({"response_type": "ephemeral", "text": ":warning: Shorthop failed to do that — "
                 "see its logs"} if req.type == "slash_commands" else None)
    client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id, payload=body))


def _stop() -> None:
    with _state_lock:
        client, _state["client"], _state["tokens"] = _state["client"], None, None
        _state["team_id"] = _state["team"] = _state["bot_user"] = None
    if client is not None:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            logger.exception("slack: closing the socket failed")
    CONNECTED.set(0)


def _start(bot: str, app_token: str) -> None:
    from slack_sdk import WebClient
    from slack_sdk.socket_mode import SocketModeClient

    try:
        info = WebClient(token=bot, timeout=TIMEOUT).auth_test()
        client = SocketModeClient(app_token=app_token, web_client=WebClient(token=bot, timeout=TIMEOUT))
        client.socket_mode_request_listeners.append(_listener)
        client.connect()
    except Exception as e:  # noqa: BLE001 — never let a bad token stop the reconcile loop
        msg = getattr(getattr(e, "response", None), "get", lambda *_: None)("error") or type(e).__name__
        with _state_lock:
            _state.update(client=None, tokens=(bot, app_token), error=_ERRORS.get(msg, str(msg)),
                          since=now())
        CONNECTED.set(0)
        logger.warning("slack: could not connect: %s", msg)
        return
    with _state_lock:
        _state.update(client=client, tokens=(bot, app_token), team_id=info.get("team_id"),
                      team=info.get("team"), bot_user=info.get("user"), error=None, since=now())
    CONNECTED.set(1)
    logger.info("slack: connected to %s as %s (Socket Mode)", info.get("team"), info.get("user"))


def reconcile() -> None:
    """Connect, reconnect or disconnect to match the current settings."""
    s = st.slack()
    SessionLocal.remove()
    tokens = (s.bot_token, s.app_token) if s.configured else None
    with _state_lock:
        current, client = _state["tokens"], _state["client"]
    if tokens is None:
        if current is not None:
            _stop()
            with _state_lock:
                _state["error"] = None
        return
    if tokens != current:
        _stop()
        _start(*tokens)
        return
    if client is not None and not client.is_connected():
        CONNECTED.set(0)   # the client reconnects by itself; the gauge says it is down meanwhile
    elif client is not None:
        CONNECTED.set(1)
    elif client is None and current is not None:
        # The last attempt failed; try again on the next pass (a minute of back-off is fine).
        with _state_lock:
            since = _state["since"]
        if since is None or (now() - aware(since)).total_seconds() >= 60:
            _start(*tokens)


def status() -> dict:
    """What THIS replica sees — for the Settings page."""
    s = st.slack()
    with _state_lock:
        client = _state["client"]
        out = {"configured": s.configured, "connected": bool(client and client.is_connected()),
               "team": _state["team"], "team_id": _state["team_id"], "bot_user": _state["bot_user"],
               "error": _state["error"],
               "since": aware(_state["since"]).isoformat() if _state["since"] else None,
               "command": cfg.SLACK_COMMAND}
    return out


_started = False


def start() -> None:
    global _started
    if _started:
        return
    _started = True

    def loop():
        while True:
            try:
                reconcile()
            except Exception:  # noqa: BLE001
                logger.exception("slack: reconcile failed")
            time.sleep(cfg.SLACK_RECONCILE_SECONDS)
    threading.Thread(target=loop, name="slack", daemon=True).start()


# --- the app manifest --------------------------------------------------------------------------

def manifest(name: str = "Shorthop") -> dict:
    """Paste into api.slack.com/apps → Create New App → From a manifest. Socket Mode, so no
    URLs. Scopes: post and update messages (and in public channels without being invited),
    DM people, read users' emails to know who clicked, list channels for the picker."""
    return {
        "display_information": {"name": name, "description": "Alerts and on-call — act on incidents from Slack",
                                "background_color": "#0f172a"},
        "features": {
            "bot_user": {"display_name": name.lower(), "always_online": True},
            "slash_commands": [{"command": cfg.SLACK_COMMAND, "description": "Incidents and on-call",
                                "usage_hint": "ping techops <what you need> | oncall | incidents | ack 42", "should_escape": False}],
        },
        "oauth_config": {"scopes": {"bot": [
            "chat:write", "chat:write.public", "commands", "im:write", "users:read",
            "users:read.email", "channels:read", "groups:read"]}},
        "settings": {"interactivity": {"is_enabled": True}, "org_deploy_enabled": False,
                     "socket_mode_enabled": True, "token_rotation_enabled": False},
    }


def list_channels() -> list[dict]:
    """Public and private channels the app can see, for the channel picker (≤ 1000)."""
    out, cursor = [], None
    for _ in range(5):
        params = {"types": "public_channel,private_channel", "exclude_archived": "true", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        r = api("conversations.list", **params)
        out += [{"id": c["id"], "name": c.get("name", ""), "private": bool(c.get("is_private")),
                 "member": bool(c.get("is_member"))} for c in r.get("channels") or []]
        cursor = (r.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
    return sorted(out, key=lambda c: c["name"])
