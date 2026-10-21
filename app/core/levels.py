"""Incident LEVELS (P1, P2, …) and how they map to severity. Pure.

A level is what a person picks ("this is a P1"); a severity is what the contact rules and the
rest of Shorthop read (`critical`, `warning`, …). One list ties them together, in order —
the first entry is the most urgent:

    [{"name": "P1", "severity": "critical", "description": "Customers are affected now"},
     {"name": "P2", "severity": "high"}, …]

Where an incident's level comes from, in this order:
  1. a person set it (Slack, web, app) — it then STICKS: alerts joining later change neither
     the level nor the severity, because a person's judgement beats a label;
  2. a `level` label on its alerts (a label rule usually sets it: severity=warning → level=P2),
     the most urgent one across the incident's alerts;
  3. the incident's severity, read through the list (the first level with that severity).
None when nothing matches — an incident is never given a level nobody chose or derived.
"""
import re

LEVEL_LABEL = "level"
MAX_LEVELS = 10
DEFAULT_LEVELS = [
    {"name": "P1", "severity": "critical", "description": "Customers are affected now"},
    {"name": "P2", "severity": "high", "description": "Broken, but there is a workaround"},
    {"name": "P3", "severity": "warning", "description": "Degraded; fix within the day"},
    {"name": "P4", "severity": "info", "description": "Nothing is broken yet"},
]
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,19}$")


class LevelError(ValueError):
    pass


def validate(levels) -> list[dict]:
    if not isinstance(levels, list) or not levels:
        raise LevelError("at least one level")
    if len(levels) > MAX_LEVELS:
        raise LevelError(f"at most {MAX_LEVELS} levels")
    out, seen = [], set()
    for i, lv in enumerate(levels, 1):
        if not isinstance(lv, dict):
            raise LevelError(f"level #{i} must be an object")
        name = str(lv.get("name") or "").strip()
        if not _NAME.match(name):
            raise LevelError(f"level #{i}: a short name — letters, digits, . _ - (e.g. P1, SEV2)")
        if name.lower() in seen:
            raise LevelError(f"level {name} is listed twice")
        seen.add(name.lower())
        sev = str(lv.get("severity") or "").strip().lower()
        if not sev or len(sev) > 40 or not re.fullmatch(r"[a-z0-9_-]+", sev):
            raise LevelError(f"level {name}: the severity it maps to (e.g. critical, warning)")
        desc = str(lv.get("description") or "").strip()[:200]
        out.append({"name": name, "severity": sev, "description": desc})
    return out


def find(levels: list[dict], name: str | None) -> dict | None:
    n = (name or "").strip().lower()
    return next((lv for lv in levels if lv["name"].lower() == n), None) if n else None


def rank(levels: list[dict], name: str | None) -> int:
    """Position in the list (0 = most urgent); unknown names rank after every known one."""
    n = (name or "").lower()
    for i, lv in enumerate(levels):
        if lv["name"].lower() == n:
            return i
    return len(levels)


def for_severity(levels: list[dict], severity: str | None) -> str | None:
    sev = (severity or "").lower()
    return next((lv["name"] for lv in levels if lv["severity"] == sev), None) if sev else None


def derive(levels: list[dict], label_values: list[str | None], severity: str | None) -> str | None:
    """The level of an incident nobody has set one on: the most urgent `level` label across
    its alerts, else the one its severity maps to."""
    named = [find(levels, v) for v in label_values]
    named = [lv["name"] for lv in named if lv]
    if named:
        return min(named, key=lambda n: rank(levels, n))
    return for_severity(levels, severity)
