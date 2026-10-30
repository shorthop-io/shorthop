"""The Jira channel: one Jira issue per incident, kept in step with it.

What it does
  * A `jira` channel in an escalation step OPENS an issue when it is first paged about an
    incident, and REMEMBERS it (`jira_issues`): which issue stands for which incident.
  * Everything that happens to the incident afterwards is a COMMENT on that issue —
    acknowledged, escalated, moved, notes, AI triage suggestions — the same lines the Slack
    thread gets, from whichever door the change came through.
  * When the incident resolves, the fix note is commented and the issue is TRANSITIONED to
    done (a named transition, or else the first one leading to a "done" status).

Why a channel kind and not a webhook preset
  A webhook is fire-and-forget: it can open an issue but never learns its key, so it cannot
  comment on it or close it. Keeping the key is the whole difference.

Direction
  ⚠️ One way, Shorthop → Jira. Shorthop owns the alert state; closing the Jira issue does NOT
  resolve the incident — alerts that are still firing must keep paging. Jira owns the incident
  process (follow-up, post-mortem).

API
  The v2 REST API with plain-text bodies, because it is the one Jira Cloud AND Jira Data
  Center/Server both serve (v3 is Cloud-only and needs Atlassian Document Format). Auth:
  email + API token (Cloud, Basic) or a personal access token (Data Center, Bearer).

Comments are posted by the DISPATCH loop, never by the request that changed the incident —
`touch()` queues them in the caller's transaction and `refresh_dirty()` claims rows with
SKIP LOCKED, exactly like the Slack messages (slack.py), so two replicas never post the same
comment twice.
"""
from __future__ import annotations

import base64
import datetime as dt
import logging
import re
from urllib.parse import urlparse

import requests
from prometheus_client import Counter
from sqlalchemy import select

from . import delivery
from .db import SessionLocal
from .models import Channel, Incident, JiraIssue, Notification, now

logger = logging.getLogger(__name__)

CALLS = Counter("shorthop_jira_calls_total", "Jira API calls, by action and outcome.",
                ["action", "outcome"])

TIMEOUT = 10
MAX_REFRESH_ATTEMPTS = 8
MAX_COMMENT = 30000   # Jira's comment limit is 32767 characters
MAX_SUMMARY = 250     # Jira's summary limit is 255
PROJECT_KEY = re.compile(r"[A-Z][A-Z0-9_]{1,19}")
ISSUE_KEY = re.compile(r"[A-Z][A-Z0-9_]+-\d+")
LABEL = re.compile(r"[^\s]{1,100}")
# The Slack lines start with an emoji code (":memo: ann: …"); Jira shows those literally.
_EMOJI = re.compile(r"^:[a-z0-9_+-]+:\s*")


class JiraError(Exception):
    """Caller-safe: what Jira (or the lack of it) said."""


# --- talking to Jira ---------------------------------------------------------------------------

def _auth_header(config: dict) -> str:
    token = config.get("api_token") or ""
    if not token:
        raise JiraError("the channel has no API token")
    if config.get("email"):
        raw = f"{config['email']}:{token}".encode()
        return "Basic " + base64.b64encode(raw).decode()
    return f"Bearer {token}"


def api(config: dict, method: str, path: str, action: str, json: dict | None = None) -> dict:
    site = str(config.get("site") or "").rstrip("/")
    err = delivery.webhook_destination_error(site)
    if err:
        raise JiraError(err.replace("the webhook URL", "the Jira site"))
    headers = {"Authorization": _auth_header(config), "Accept": "application/json",
               "Content-Type": "application/json", "User-Agent": "shorthop"}
    try:
        resp = requests.request(method, site + path, json=json, headers=headers,
                                timeout=TIMEOUT, allow_redirects=False)
    except requests.RequestException as e:
        CALLS.labels(action, "error").inc()
        raise JiraError(f"could not reach Jira ({type(e).__name__})") from None
    if not 200 <= resp.status_code < 300:
        CALLS.labels(action, "error").inc()
        raise JiraError(_explain(resp))
    CALLS.labels(action, "ok").inc()
    if resp.status_code == 204 or not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError:
        return {}


def _explain(resp) -> str:
    """Jira's own reason, which names the field it refused — far more useful than the code."""
    hint = {401: "the email/token was refused", 403: "the account may not do this",
            404: "not found (site, project or issue)"}.get(resp.status_code, "")
    detail = ""
    try:
        body = resp.json()
        parts = list(body.get("errorMessages") or [])
        parts += [f"{k}: {v}" for k, v in (body.get("errors") or {}).items()]
        detail = "; ".join(str(p) for p in parts)
    except ValueError:
        detail = (resp.text or "").strip().replace("\n", " ")
    msg = f"Jira answered HTTP {resp.status_code}"
    if hint:
        msg += f" — {hint}"
    if detail:
        msg += f": {detail[:300]}"
    return msg


def issue_url(config: dict, key: str) -> str:
    return f"{str(config.get('site') or '').rstrip('/')}/browse/{key}"


# --- what goes into the issue -------------------------------------------------------------------

def _summary(inc: Incident) -> str:
    level = f"[{inc.level}] " if inc.level else ""
    return f"{level}#{inc.number} {inc.title}"[:MAX_SUMMARY]


def _description(inc: Incident) -> str:
    vars_ = delivery.template_vars(delivery.payload(inc, "triggered"))
    lines = [f"Opened by Shorthop for incident #{inc.number}."]
    if vars_["url"]:
        lines.append(f"Incident: {vars_['url']}")
    if vars_["history"]:
        lines.append(vars_["history"])
    if vars_["alerts_text"]:
        lines += ["", "Alerts:", vars_["alerts_text"]]
    lines += ["", "Shorthop comments here as the incident changes and closes this issue when "
                  "it resolves. Closing the issue does not resolve the alerts."]
    return "\n".join(lines)


def _plain(line: str) -> str:
    return _EMOJI.sub("", line or "").strip()[:MAX_COMMENT]


# --- queuing comments (called from incidents.py, in the caller's transaction) -----------------

def touch(db, incident_id: str, line: str | None) -> None:
    """Queue `line` as a comment on every Jira issue standing for this incident.

    ⚠️ Locks the rows (FOR UPDATE): a refresh holding one is posting its pending comments, and
    without the lock this would append to the list it read before the refresh cleared it —
    posting comments twice."""
    if not line:
        return
    # Closed issues too: "what fixed it" is often written after the incident resolved.
    rows = db.scalars(select(JiraIssue).where(JiraIssue.incident_id == incident_id)
                      .with_for_update()).all()
    for r in rows:
        r.pending_comments = list(r.pending_comments or []) + [_plain(line)]
        r.dirty, r.dirty_at = True, now()


# --- sending (the outbox calls this for kind=jira) ------------------------------------------------

def _channel(db, n: Notification) -> Channel:
    ch = db.get(Channel, n.channel_id) if n.channel_id else None
    if ch is None:
        raise JiraError("the channel no longer exists")
    return ch


def _flush(config: dict, row: JiraIssue) -> None:
    """Post queued comments in order, then close the issue if the incident resolved. A failure
    keeps what was not yet posted, so a retry continues where this one stopped."""
    pending = list(row.pending_comments or [])
    try:
        while pending:
            api(config, "POST", f"/rest/api/2/issue/{row.issue_key}/comment", "comment",
                {"body": pending[0]})
            pending.pop(0)
        if row.close_pending:
            _transition_done(config, row.issue_key)
            row.close_pending, row.closed_at = False, now()
    finally:
        row.pending_comments = pending


def _transition_done(config: dict, key: str) -> None:
    found = api(config, "GET", f"/rest/api/2/issue/{key}/transitions", "transitions")
    options = found.get("transitions") or []
    wanted = str(config.get("done_transition") or "").strip().lower()
    if wanted:
        pick = next((t for t in options if str(t.get("name", "")).lower() == wanted), None)
        if pick is None:
            names = ", ".join(t.get("name", "?") for t in options) or "none"
            raise JiraError(f"{key} has no transition named '{config['done_transition']}' "
                            f"from its current status (available: {names})")
    else:
        pick = next((t for t in options if ((t.get("to") or {}).get("statusCategory") or {})
                     .get("key") == "done"), None)
        if pick is None:
            # Already done (closed by hand), or a workflow with no way there from here.
            logger.info("jira %s: no transition to a done status — left as it is", key)
            return
    api(config, "POST", f"/rest/api/2/issue/{key}/transitions", "transition",
        {"transition": {"id": pick["id"]}})


def deliver(db, n: Notification, inc: Incident) -> None:
    """triggered → open the issue (once); escalated/resolved → post what is queued (the
    escalation and resolve lines were queued by touch() in the transaction that made them),
    and on resolved close the issue."""
    ch = _channel(db, n)
    config = ch.config or {}
    row = db.scalar(select(JiraIssue).where(JiraIssue.incident_id == inc.id,
                                            JiraIssue.channel_id == ch.id).with_for_update())
    if row is None:
        if n.event == "resolved":
            return  # never told about it while it was open: nothing to close
        fields = {"project": {"key": config["project_key"]},
                  "issuetype": {"name": config.get("issue_type") or "Task"},
                  "summary": _summary(inc), "description": _description(inc)}
        labels = list(config.get("labels") or []) + ["shorthop"]
        fields["labels"] = sorted(set(labels))
        created = api(config, "POST", "/rest/api/2/issue", "create", {"fields": fields})
        key = created.get("key")
        if not key or not ISSUE_KEY.fullmatch(key):
            raise JiraError("Jira created an issue but did not say its key")
        row = JiraIssue(incident_id=inc.id, channel_id=ch.id, issue_key=key,
                        issue_url=issue_url(config, key), pending_comments=[])
        db.add(row)
        # ⚠️ Recorded BEFORE anything else can fail: the issue exists now, and a retry must
        # comment on it, never open a second one.
        db.flush()
        return
    if n.event == "resolved" and row.closed_at is None:
        row.close_pending = True
    _flush(config, row)
    row.dirty = False
    row.attempts, row.error, row.updated_at = 0, None, now()


def send_test(channel: Channel) -> str:
    """Check the site, the credentials and the project — WITHOUT opening an issue."""
    config = channel.config or {}
    me = api(config, "GET", "/rest/api/2/myself", "test")
    project = api(config, "GET", f"/rest/api/2/project/{config.get('project_key')}", "test")
    types = [t.get("name") for t in project.get("issueTypes") or []]
    wanted = config.get("issue_type") or "Task"
    if types and wanted not in types:
        raise JiraError(f"project {project.get('key')} has no issue type '{wanted}' "
                        f"(it has: {', '.join(t for t in types if t)})")
    who = me.get("emailAddress") or me.get("displayName") or me.get("name") or "?"
    return f"signed in as {who}; project {project.get('key')} ({project.get('name')}) accepts '{wanted}'"


# --- the comment loop (dispatch loop, every replica) ------------------------------------------------

def _claim_dirty(db, t: dt.datetime, skip: set[str]) -> JiraIssue | None:
    return db.scalar(select(JiraIssue).where(
        JiraIssue.dirty.is_(True), JiraIssue.dirty_at <= t,
        JiraIssue.id.not_in(skip or {""}))
        .order_by(JiraIssue.dirty_at).limit(1).with_for_update(skip_locked=True))


def refresh_dirty(limit: int = 30) -> int:
    """Post queued comments (and a pending close). One claimed row per transaction. A failure
    backs off (30 s × 2ⁿ, ≤ 10 min) and keeps what is not yet posted; after
    MAX_REFRESH_ATTEMPTS it stops with the error recorded on the row."""
    db = SessionLocal
    t = now()
    handled: set[str] = set()
    while len(handled) < limit:
        r = _claim_dirty(db, t, handled)
        if r is None:
            db.rollback()
            break
        handled.add(r.id)
        ch = db.get(Channel, r.channel_id) if r.channel_id else None
        try:
            if ch is None or not ch.enabled:
                raise JiraError("the channel was deleted or disabled")
            _flush(ch.config or {}, r)
            r.dirty, r.attempts, r.error, r.updated_at = False, 0, None, now()
        except JiraError as e:
            r.attempts += 1
            r.error = str(e)
            if r.attempts >= MAX_REFRESH_ATTEMPTS:
                r.dirty = False
                logger.warning("jira issue %s: giving up: %s", r.issue_key, e)
            else:
                r.dirty_at = now() + dt.timedelta(seconds=min(600, 30 * 2 ** (r.attempts - 1)))
        except Exception:  # noqa: BLE001 — one broken issue must not stop the others
            logger.exception("jira issue %s: unexpected error", r.issue_key)
            r.attempts += 1
            r.dirty_at = now() + dt.timedelta(seconds=60)
        try:
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception("jira issue %s: could not record the outcome", r.issue_key)
    return len(handled)


# --- config (called by the channel API) -----------------------------------------------------------

def clean_config(config: dict, old: dict | None, mask: str) -> dict:
    """Validated channel config. Raises ValueError with a message naming the field.
    The API token is write-only: sending the mask back keeps the stored one."""
    site = str(config.get("site") or "").strip().rstrip("/")
    parsed = urlparse(site)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or len(site) > 300:
        raise ValueError("config.site: your Jira's address, e.g. https://acme.atlassian.net")
    if parsed.query or parsed.fragment or parsed.path.strip("/").count("/") > 0:
        # One path segment is a Data Center context path (https://host/jira); anything deeper
        # is a pasted board or issue link, and every API call would then 404.
        raise ValueError("config.site: the site's address only, not a board or issue link")
    err = delivery.webhook_destination_error(site)
    if err:
        raise ValueError(f"config.site: {err.replace('the webhook URL', 'the site')}")
    email = str(config.get("email") or "").strip()
    if email and ("@" not in email or len(email) > 254):
        raise ValueError("config.email: the Atlassian account's email (Jira Cloud), "
                         "or empty for a Data Center personal access token")
    token = str(config.get("api_token") or "")
    if token == mask:
        token = (old or {}).get("api_token") or ""
        if not token:
            raise ValueError("config.api_token: there is no stored token to keep")
    if not token or len(token) > 1000 or "\n" in token or "\r" in token:
        raise ValueError("config.api_token: an Atlassian API token (Cloud) or personal "
                         "access token (Data Center)")
    project = str(config.get("project_key") or "").strip().upper()
    if not PROJECT_KEY.fullmatch(project):
        raise ValueError("config.project_key: the project's key, e.g. OPS")
    issue_type = str(config.get("issue_type") or "Task").strip()[:60] or "Task"
    labels = config.get("labels") or []
    if isinstance(labels, str):
        labels = [x for x in re.split(r"[,\s]+", labels) if x]
    if not isinstance(labels, list) or len(labels) > 10 or not all(
            isinstance(x, str) and LABEL.fullmatch(x) for x in labels):
        raise ValueError("config.labels: up to 10 labels, without spaces")
    out = {"site": site, "api_token": token, "project_key": project, "issue_type": issue_type,
           "labels": labels}
    if email:
        out["email"] = email.lower()
    done = str(config.get("done_transition") or "").strip()[:60]
    if done:
        out["done_transition"] = done
    return out
