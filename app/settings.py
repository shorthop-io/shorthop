"""Settings an admin changes at runtime: the Prometheus host allowlist, the webhook private-host
exemptions, SMTP, the push relay, the Slack app and the incident levels.

Each setting has an environment variable (app/config.py) as its DEFAULT; a row in `settings`
overrides it. No row = the environment value applies — so a fresh install configured by its
deployment needs nothing here, and "Reset" on the Settings page hands a value back to the
deployment. The page shows which source each value comes from.

Read from the database on every use, not cached: they are read once per email and once per
Prometheus comparison, and a value changed on the page must apply to the very next send.

⚠️ The SMTP password, the relay key and the Slack tokens are WRITE-ONLY over the API: never
returned, only whether one is set.
"""
import json
import logging
from dataclasses import dataclass

from . import config as cfg
from .core import levels as lv
from .db import SessionLocal
from .models import Setting, now

# key -> (environment variable, getter for the env default). The getter reads `cfg` at call
# time, so a test that monkeypatches cfg still sees its value.
SPECS = {
    "prometheus_allowed_hosts": ("PROMETHEUS_ALLOWED_HOSTS", lambda: list(cfg.PROMETHEUS_ALLOWED_HOSTS)),
    "webhook_private_hosts": ("WEBHOOK_PRIVATE_HOSTS", lambda: list(cfg.WEBHOOK_PRIVATE_HOSTS)),
    "smtp_host": ("SMTP_HOST", lambda: cfg.SMTP_HOST),
    "smtp_port": ("SMTP_PORT", lambda: cfg.SMTP_PORT),
    "smtp_user": ("SMTP_USER", lambda: cfg.SMTP_USER),
    "smtp_password": ("SMTP_PASSWORD", lambda: cfg.SMTP_PASSWORD),
    "smtp_from": ("SMTP_FROM", lambda: cfg.SMTP_FROM),
    "smtp_starttls": ("SMTP_STARTTLS", lambda: cfg.SMTP_STARTTLS),
    "relay_url": ("RELAY_URL", lambda: cfg.RELAY_URL),
    "relay_key": ("RELAY_KEY", lambda: cfg.RELAY_KEY),
    "slack_bot_token": ("SLACK_BOT_TOKEN", lambda: cfg.SLACK_BOT_TOKEN),
    "slack_app_token": ("SLACK_APP_TOKEN", lambda: cfg.SLACK_APP_TOKEN),
    "incident_levels": ("INCIDENT_LEVELS", lambda: _env_levels()),
}
SECRET_KEYS = {"smtp_password", "relay_key", "slack_bot_token", "slack_app_token"}

logger = logging.getLogger(__name__)


def _env_levels() -> list[dict]:
    """INCIDENT_LEVELS as JSON; unset or unreadable = the defaults (and a warning) — a typo in
    the deployment must not leave every incident without a level."""
    if not cfg.INCIDENT_LEVELS:
        return [dict(x) for x in lv.DEFAULT_LEVELS]
    try:
        return lv.validate(json.loads(cfg.INCIDENT_LEVELS))
    except (ValueError, lv.LevelError) as e:
        logger.warning("INCIDENT_LEVELS ignored (%s) — using P1–P4", e)
        return [dict(x) for x in lv.DEFAULT_LEVELS]


def _rows() -> dict[str, Setting]:
    return {r.key: r for r in SessionLocal.query(Setting).all()}


def get(key: str):
    row = SessionLocal.get(Setting, key)
    return row.value if row is not None else SPECS[key][1]()


def describe() -> list[dict]:
    """Every setting with its effective value and where it comes from — for the admin page."""
    rows = _rows()
    out = []
    for key, (env, default) in SPECS.items():
        row = rows.get(key)
        value = row.value if row is not None else default()
        item = {"key": key, "env": env, "source": "settings" if row is not None else "environment",
                "updated_at": row.updated_at.isoformat() if row is not None else None,
                "updated_by": row.updated_by if row is not None else None}
        if key in SECRET_KEYS:
            item["value"] = None
            item["is_set"] = bool(value)
        else:
            item["value"] = value
        out.append(item)
    return out


def put(values: dict, actor: str) -> list[str]:
    """Write the given keys. None = delete the override (back to the environment value).
    Returns the keys that changed. The caller commits."""
    changed = []
    for key, value in values.items():
        row = SessionLocal.get(Setting, key)
        if value is None:
            if row is not None:
                SessionLocal.delete(row)
                changed.append(key)
            continue
        if row is None:
            SessionLocal.add(Setting(key=key, value=value, updated_at=now(), updated_by=actor))
            changed.append(key)
        elif row.value != value:
            row.value, row.updated_at, row.updated_by = value, now(), actor
            changed.append(key)
    return changed


def prometheus_allowed_hosts() -> list[str]:
    return [h.lower() for h in (get("prometheus_allowed_hosts") or [])]


def webhook_private_hosts() -> list[str]:
    return [h.lower() for h in (get("webhook_private_hosts") or [])]


@dataclass
class Smtp:
    host: str
    port: int
    user: str
    password: str
    sender: str
    starttls: bool


def smtp() -> Smtp:
    return Smtp(host=get("smtp_host") or "", port=int(get("smtp_port") or 587),
                user=get("smtp_user") or "", password=get("smtp_password") or "",
                sender=get("smtp_from") or "", starttls=bool(get("smtp_starttls")))


def incident_levels() -> list[dict]:
    """The level list in force, most urgent first. A stored list that no longer validates
    (hand-edited row) falls back to the defaults rather than breaking every incident page."""
    raw = get("incident_levels")
    try:
        return lv.validate(raw)
    except lv.LevelError:
        return [dict(x) for x in lv.DEFAULT_LEVELS]


@dataclass
class Slack:
    bot_token: str
    app_token: str

    @property
    def configured(self) -> bool:
        return bool(self.bot_token and self.app_token)


def slack() -> Slack:
    return Slack(bot_token=get("slack_bot_token") or "", app_token=get("slack_app_token") or "")
