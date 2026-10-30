"""Shorthop configuration — everything comes from the environment.

Unset values degrade rather than break: no POSTGRES_HOST means a local SQLite file (dev and
tests need zero infra), no GOOGLE_CLIENT_ID/SECRET hides the Google button, no
HEARTBEAT_URL skips the dead-man's ping.
"""
import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _list(name: str, default: str = "") -> list[str]:
    return [v.strip() for v in os.getenv(name, default).split(",") if v.strip()]


API_TITLE = "shorthop"
API_VERSION = "v1"
OPENAPI_VERSION = "3.0.3"
OPENAPI_URL_PREFIX = "/"
OPENAPI_SWAGGER_UI_PATH = "/docs"
# Pinned: an unpinned CDN path serves whatever was published last, into a page that holds
# a token. The CSP for /docs allows exactly this origin (app/exposure.py).
OPENAPI_SWAGGER_UI_URL = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.17.14/"

DEBUG = os.getenv("DEBUG", "false").lower() == "true"
PORT = _int("PORT", 8080)

# --- exposure (app/exposure.py) -------------------------------------------------------
# A SECOND listener for the internet. Only what phones and calendar apps need answers there:
# the API with DEVICE tokens (shd_…) only, phone pairing and calendar feeds. The web UI,
# sign-in, ingest, MCP, /metrics and /docs answer only on PORT, which stays on the private
# network. 0 = no public listener (everything on PORT, as before).
PUBLIC_PORT = _int("PUBLIC_PORT", 0)
# The URL phones and calendar apps reach the PUBLIC listener at (pairing links, feed URLs).
# Unset = PUBLIC_URL.
PUBLIC_API_URL = os.getenv("PUBLIC_API_URL", "").rstrip("/")
# Peers allowed to tell us the client address (X-Forwarded-For, or CLIENT_IP_HEADER). Only
# a request whose socket peer is in one of these is believed; the chain is then read right
# to left, skipping trusted hops. Default: loopback + private ranges (a proxy in front).
TRUSTED_PROXIES = _list(
    "TRUSTED_PROXIES",
    "127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7",
)
# Optional single-value header a trusted proxy sets to the client address, e.g.
# CF-Connecting-IP or X-Real-IP. Read only from a trusted peer.
CLIENT_IP_HEADER = os.getenv("CLIENT_IP_HEADER", "")
# Rate limits on the public surface (counted in the database: one budget across replicas).
DEVICE_REQUESTS_PER_MINUTE = _int("DEVICE_REQUESTS_PER_MINUTE", 600)
PUBLIC_AUTH_FAILURES_PER_10MIN = _int("PUBLIC_AUTH_FAILURES_PER_10MIN", 300)
FEED_FETCHES_PER_HOUR = _int("FEED_FETCHES_PER_HOUR", 120)
FEED_MISSES_PER_10MIN = _int("FEED_MISSES_PER_10MIN", 60)
# Break-glass password sign-in: failures per client address and per email, per 15 minutes.
LOGIN_FAILURES_PER_IP = _int("LOGIN_FAILURES_PER_IP", 20)
LOGIN_FAILURES_PER_EMAIL = _int("LOGIN_FAILURES_PER_EMAIL", 10)

# --- serving (waitress, one process, many threads — see serve.py) -------------------
THREADS = _int("THREADS", 8)
CONNECTION_LIMIT = _int("CONNECTION_LIMIT", 200)
CHANNEL_TIMEOUT = _int("CHANNEL_TIMEOUT", 120)
SHUTDOWN_DELAY_SECONDS = _int("SHUTDOWN_DELAY_SECONDS", 5)
SHUTDOWN_GRACE_SECONDS = _int("SHUTDOWN_GRACE_SECONDS", 20)

# --- database ----------------------------------------------------------------------
# The Postgres server. Set it for anything real; unset = a local SQLite file (DATABASE_URL),
# for trying Shorthop out. More than one replica needs Postgres.
POSTGRES_HOST = os.getenv("POSTGRES_HOST", "")
POSTGRES_PORT = _int("POSTGRES_PORT", 5432)
POSTGRES_DB = os.getenv("POSTGRES_DB", "shorthop")
POSTGRES_USER = os.getenv("POSTGRES_USER", "shorthop")
# The Postgres password. POSTGRESQL_PASSWORD is accepted too; POSTGRES_PASSWORD wins.
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD") or os.getenv("POSTGRESQL_PASSWORD", "")
POSTGRES_SSLMODE = os.getenv("POSTGRES_SSLMODE", "disable")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///shorthop.db")
SQL_ECHO = os.getenv("SQL_ECHO", "false").lower() == "true"

# --- web session -------------------------------------------------------------------
# Stable in production (a random one per process logs everybody out on every restart).
SECRET_KEY = os.getenv("SECRET_KEY", "")
SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "true").lower() == "true"
# The address people open the web UI at, e.g. https://oncall.example.com — used for links in
# notifications and MCP output.
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")

# --- Google SSO (copied in shape from dbdeck) -------------------------------------
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URL = os.getenv("GOOGLE_REDIRECT_URL", "")
GOOGLE_ALLOWED_DOMAINS = [d.lower().lstrip("@") for d in _list("GOOGLE_ALLOWED_DOMAINS")]

# First admin on an empty install. Only consulted while the users table is EMPTY, so it
# can stay set forever without re-creating anyone. Signs in with Google (or a password set
# later through the API by that admin).
BOOTSTRAP_ADMIN_EMAIL = os.getenv("BOOTSTRAP_ADMIN_EMAIL", "").strip().lower()

# --- alert handling ----------------------------------------------------------------
# Labels that identify an INSTANCE of a problem rather than the problem itself. They are
# dropped when computing an alert's signature, so the same fault on another pod is "the
# same alert as last time". Overridable per integration.
SIGNATURE_IGNORE_LABELS = _list(
    "SIGNATURE_IGNORE_LABELS",
    "pod,instance,pod_ip,pod_template_hash,container,container_id,endpoint,uid,"
    "controller_revision_hash,statefulset_kubernetes_io_pod_name,node,host_ip,id,image",
)
# A firing alert with no explicit endsAt is resolved this long after it was last seen.
# Prometheus sends its own endsAt (≈4x resend interval) so this only covers webhooks.
DEFAULT_ALERT_TIMEOUT_MINUTES = _int("DEFAULT_ALERT_TIMEOUT_MINUTES", 5)
# Hard cap on alerts in one ingest request: marshmallow materialises every row before a
# handler can count them, so the bound has to be in the schema.
MAX_ALERTS_PER_REQUEST = _int("MAX_ALERTS_PER_REQUEST", 2000)
MAX_SILENCE_DAYS = _int("MAX_SILENCE_DAYS", 30)
# Silences created through MCP are capped tighter: an assistant should not be able to
# mute something for a month.
MCP_MAX_SILENCE_HOURS = _int("MCP_MAX_SILENCE_HOURS", 24)

# --- background loop ---------------------------------------------------------------
SCHEDULER_ENABLED = os.getenv("SCHEDULER_ENABLED", "true").lower() == "true"
SCHEDULER_INTERVAL_SECONDS = _int("SCHEDULER_INTERVAL_SECONDS", 30)
# Dead-man's switch: pinged every loop. Point it at something OUTSIDE this cluster
# (healthchecks.io) so a dead Shorthop is noticed by something other than Shorthop.
HEARTBEAT_URL = os.getenv("HEARTBEAT_URL", "")
# The heartbeat means "I can DELIVER", not "the process is up": it is withheld while this
# process's dispatch loop (escalations + sending) has not completed a tick for this long, so a
# wedged dispatch goes quiet on the outside path too. Several dispatch intervals wide (5 s),
# so one slow send does not cost a ping.
HEARTBEAT_DISPATCH_MAX_AGE_SECONDS = _int("HEARTBEAT_DISPATCH_MAX_AGE_SECONDS", 60)
# An integration whose Watchdog alert has not been seen for this long is reported as silent.
WATCHDOG_STALE_MINUTES = _int("WATCHDOG_STALE_MINUTES", 10)

# --- shadow comparison against a Prometheus ---------------------------------------------
# An integration may name the Prometheus it receives from; Shorthop then asks it what is
# firing and compares. The URL is set through the API, so the HOST must be on this list
# (exact names, comma-separated). Empty = the comparison is off everywhere.
PROMETHEUS_ALLOWED_HOSTS = [h.lower() for h in _list("PROMETHEUS_ALLOWED_HOSTS")]
SHADOW_CHECK_INTERVAL_SECONDS = _int("SHADOW_CHECK_INTERVAL_SECONDS", 60)

# --- delivery (M2) -----------------------------------------------------------------------
# The fast loop: due escalation steps and pending notifications. Separate from the 30 s
# housekeeping tick because a page that waits 30 s for the loop to come round is a slow page.
DISPATCH_INTERVAL_SECONDS = _int("DISPATCH_INTERVAL_SECONDS", 5)
DELIVERY_MAX_ATTEMPTS = _int("DELIVERY_MAX_ATTEMPTS", 5)
DELIVERY_RETRY_BASE_SECONDS = _int("DELIVERY_RETRY_BASE_SECONDS", 30)
# A step whose alerts are all silenced or inhibited is held, not skipped, and re-checked
# this often — so when the silence ends, paging resumes where it was.
SUPPRESSED_RECHECK_SECONDS = _int("SUPPRESSED_RECHECK_SECONDS", 60)

# Email over SMTP. Unset SMTP_HOST = email delivery off (notifications are logged as failed
# with a reason, never silently dropped).
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = _int("SMTP_PORT", 587)
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", "")
SMTP_STARTTLS = os.getenv("SMTP_STARTTLS", "true").lower() == "true"

# Webhook channels may not point at private, loopback or link-local addresses (the server
# would otherwise be a way into its own network). Hosts listed here are exempt — e.g. an
# internal chat server. Exact host names or IPs. Overridable on the Settings page.
WEBHOOK_PRIVATE_HOSTS = [h.lower() for h in _list("WEBHOOK_PRIVATE_HOSTS")]

# Optional: Expo's "enhanced push security" access token. Unset works with Expo's defaults.
EXPO_ACCESS_TOKEN = os.getenv("EXPO_ACCESS_TOKEN", "")

# Push relay (app/relay.py). Both set = phones register with the relay and pages go through it
# (how a self-hosted install reaches the published app). Unset = straight to Expo with this
# install's own credentials. Overridable on the admin Settings page.
RELAY_URL = os.getenv("RELAY_URL", "")
RELAY_KEY = os.getenv("RELAY_KEY", "")
# Whether a page through the relay carries the incident's title and summary, so the lock screen
# says WHAT fired or resolved. Off = the relay only ever gets an incident id and the phone shows
# a generic "an incident needs your attention". Overridable on the admin Settings page.
RELAY_SEND_CONTENT = os.getenv("RELAY_SEND_CONTENT", "true").lower() == "true"
# How often the leader tells the relay which phones checked in, and reads back its warnings.
RELAY_SYNC_INTERVAL_SECONDS = int(os.getenv("RELAY_SYNC_INTERVAL_SECONDS", "3600"))

# --- Slack app (app/slack.py) ------------------------------------------------------------
# Socket Mode: Shorthop opens an OUTBOUND WebSocket to Slack, so buttons in Slack work with no
# public URL. Both tokens set = on. Overridable on the admin Settings page.
#   SLACK_BOT_TOKEN  xoxb-…  (OAuth & Permissions → Bot User OAuth Token)
#   SLACK_APP_TOKEN  xapp-…  (Basic Information → App-Level Tokens, scope connections:write)
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN", "")
# How often each replica re-reads the Slack settings and (re)connects if they changed.
SLACK_RECONCILE_SECONDS = _int("SLACK_RECONCILE_SECONDS", 30)
# The slash command the app manifest declares.
SLACK_COMMAND = os.getenv("SLACK_COMMAND", "/shorthop")

# Incident levels (core/levels.py) as JSON; unset = P1–P4 → critical/high/warning/info.
INCIDENT_LEVELS = os.getenv("INCIDENT_LEVELS", "")

METRICS_TOKEN = os.getenv("METRICS_TOKEN", "")

# --- Shorthop Watch (the relay's dead man's switch) ---
# A Watchdog is forwarded to its Watch check at most this often. Prometheus re-sends the
# Watchdog every minute or so; one ping a minute is all a check needs.
WATCH_FORWARD_MIN_SECONDS = _int("WATCH_FORWARD_MIN_SECONDS", 60)
