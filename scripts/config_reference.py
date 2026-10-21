"""Generate docs/configuration.md from app/config.py — the settings reference is never written
by hand, so it cannot drift from the code. tests/test_docs.py fails when it is stale.

    python scripts/config_reference.py          # rewrite docs/configuration.md
    python scripts/config_reference.py --check  # exit 1 if it is out of date

Reads, for every setting: its environment variable, its default, and the comment block
directly above it. Sections come from the `# --- name ---` headers in config.py.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "app" / "config.py"
OUT = ROOT / "docs" / "configuration.md"

# Internal-only knobs that a self-hoster should never need; kept out of the public reference.
HIDDEN = {"SQL_ECHO", "DEBUG", "SCHEDULER_ENABLED", "POSTGRESQL_PASSWORD"}
# Settings that are secrets: shown, but flagged so nobody puts them in a values file.
SECRETS = {"SECRET_KEY", "POSTGRES_PASSWORD", "POSTGRESQL_PASSWORD", "SMTP_PASSWORD", "RELAY_KEY",
           "GOOGLE_CLIENT_SECRET", "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "METRICS_TOKEN",
           "EXPO_ACCESS_TOKEN"}

# One line for settings whose name and default say almost everything and that have no comment
# of their own in config.py. Anything that needs explaining belongs in config.py instead.
SHORT = {
    "PORT": "The private listener: web UI, sign-in, alert ingest, MCP, API, /metrics. Keep it off the internet.",
    "PUBLIC_AUTH_FAILURES_PER_10MIN": "Failed credentials per client address on the public listener (generous: phones share carrier NAT).",
    "FEED_FETCHES_PER_HOUR": "Fetches per calendar feed per hour.",
    "FEED_MISSES_PER_10MIN": "Unknown calendar-feed tokens per client address.",
    "LOGIN_FAILURES_PER_EMAIL": "Failed password sign-ins per email address per 15 minutes.",
    "THREADS": "Request threads per process. Scale with replicas, never with processes.",
    "CONNECTION_LIMIT": "Open connections per process.",
    "CHANNEL_TIMEOUT": "Seconds an idle connection is kept.",
    "SHUTDOWN_DELAY_SECONDS": "On SIGTERM, keep serving this long so load balancers stop sending first.",
    "SHUTDOWN_GRACE_SECONDS": "Then finish in-flight requests for up to this long.",
    "POSTGRES_PORT": "Postgres port.",
    "POSTGRES_DB": "Postgres database.",
    "POSTGRES_USER": "Postgres user.",
    "POSTGRES_PASSWORD": "Postgres password (POSTGRESQL_PASSWORD is accepted too).",
    "POSTGRES_SSLMODE": "libpq sslmode: disable, prefer, require, verify-full …",
    "DATABASE_URL": "Used only when POSTGRES_HOST is unset: a local SQLite file, for trying Shorthop out. Run Postgres for anything real.",
    "SESSION_COOKIE_SECURE": "Send the session cookie over HTTPS only. Leave true; browsers accept it on http://localhost too.",
    "GOOGLE_CLIENT_ID": "Google sign-in (optional). Without it people sign in with a password.",
    "GOOGLE_CLIENT_SECRET": "Google sign-in client secret.",
    "GOOGLE_REDIRECT_URL": "Must be <PUBLIC_URL>/auth/google/callback, and registered on the Google client.",
    "GOOGLE_ALLOWED_DOMAINS": "Only these email domains may sign in with Google (comma-separated). Empty = anyone you have invited.",
    "MAX_SILENCE_DAYS": "Longest silence anyone can set.",
    "SCHEDULER_INTERVAL_SECONDS": "How often the leader runs housekeeping (expiry, watchdogs, handovers).",
    "SHADOW_CHECK_INTERVAL_SECONDS": "How often the shadow comparison asks Prometheus what is firing.",
    "DELIVERY_MAX_ATTEMPTS": "Tries per notification before it is marked failed.",
    "DELIVERY_RETRY_BASE_SECONDS": "Back-off between tries (doubles each time).",
    "SMTP_PORT": "SMTP port (587 STARTTLS, 465 TLS).",
    "SMTP_USER": "SMTP user.",
    "SMTP_PASSWORD": "SMTP password.",
    "SMTP_FROM": "Sender, e.g. `Shorthop <alerts@example.com>`.",
    "SMTP_STARTTLS": "Use STARTTLS.",
    "RELAY_KEY": "Your installation key (shr_…) from https://account.shorthop.io.",
    "SLACK_APP_TOKEN": "Slack app-level token (xapp-…), for Socket Mode.",
    "METRICS_TOKEN": "If set, /metrics needs `Authorization: Bearer <token>`.",
}

_SECTION = re.compile(r"^#\s*-{2,}\s*(.+?)\s*-{2,}\s*$")
_GETENV = re.compile(r'(?:os\.getenv|_int|_list|_bool)\(\s*"([A-Z0-9_]+)"\s*(?:,\s*(.+?))?\)')


def _default(raw: str | None) -> str:
    if raw is None:
        return ""
    raw = raw.strip().rstrip(")").strip()
    m = re.match(r'^"(.*)"$', raw) or re.match(r"^'(.*)'$", raw)
    if m:
        return m.group(1)
    return raw if re.fullmatch(r"-?\d+(\.\d+)?", raw) else ""


def parse() -> list[tuple[str, list[tuple[str, str, str]]]]:
    sections: list[tuple[str, list[tuple[str, str, str]]]] = [("General", [])]
    comment: list[str] = []
    seen: set[str] = set()
    lines = iter(SRC.read_text().splitlines())
    for line in lines:
        s = line.strip()
        # A call spread over several lines (`_list(\n "NAME",\n "default")`) is joined first.
        while not s.startswith("#") and s.count("(") > s.count(")"):
            nxt = next(lines, None)
            if nxt is None:
                break
            s += " " + nxt.strip()
        m = _SECTION.match(s)
        if m:
            title = m.group(1).strip()
            title = re.sub(r"\s*\(.*?\)\s*$", "", title)        # drop "(M2)" style tags
            sections.append((title[:1].upper() + title[1:], []))
            comment = []
            continue
        if s.startswith("#"):
            text = s.lstrip("#").strip()
            if not text.startswith("OX Agent"):
                comment.append(text)
            continue
        if not s:
            comment = []
            continue
        for name, default in _GETENV.findall(s):
            if name in seen or name in HIDDEN:
                continue
            seen.add(name)
            desc = " ".join(c for c in comment if c) or SHORT.get(name, "")
            desc = desc.replace("|", "\\|")
            desc = re.sub(r"[⚠️☠️]", "", desc).strip()
            sections[-1][1].append((name, _default(default or None), desc))
        if "=" in s and not s.startswith(("if", "def", "return")):
            comment = []
    return [(t, rows) for t, rows in sections if rows]


def render() -> str:
    out = ["# Configuration",
           "",
           "Every setting is an environment variable. Most have a sensible default; an install needs",
           "only the database, `SECRET_KEY` and `BOOTSTRAP_ADMIN_EMAIL` to start. Several (email, the",
           "push relay, Slack, incident levels) can also be changed later on the **Settings** page,",
           "which overrides the environment.",
           "",
           "Settings marked 🔒 are secrets: put them in a Kubernetes Secret or your `.env`, never in a",
           "values file or a repository.",
           "",
           "<!-- GENERATED by scripts/config_reference.py from app/config.py — do not edit by hand. -->",
           ""]
    for title, rows in parse():
        out += [f"## {title}", "", "| Variable | Default | What it does |", "|---|---|---|"]
        for name, default, desc in rows:
            lock = " 🔒" if name in SECRETS else ""
            d = f"`{default}`" if default != "" else "—"
            out.append(f"| `{name}`{lock} | {d} | {desc or '—'} |")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def main() -> int:
    text = render()
    if "--check" in sys.argv:
        if not OUT.exists() or OUT.read_text() != text:
            print("docs/configuration.md is out of date: run python scripts/config_reference.py")
            return 1
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text)
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
