"""Label rules: add or change labels on incoming alerts. Pure — plain dicts in, plain dicts
out, so the whole rule set is unit-testable.

A rule = optional label matchers + optional text pattern → labels to set. Rules run TOP TO
BOTTOM and EVERY matching rule applies, a later one overwriting what an earlier one set —
so the general rule goes first and the exception after it:

    1. severity="warning"                         → level=P2
    2. severity="warning", text ~ "silent socket"  → level=P1

A rule sees the labels the rules above it set (it can match `level="P1"`), and `stop` ends
the evaluation for that alert.

⚠️ This runs AFTER the alert's identity is computed: fingerprint and signature come from the
labels the SENDER sent. Otherwise editing a rule — or a description whose text changes
between sends — would split one alert into two and cut it off from its own history.
"""
import functools
import re
from dataclasses import dataclass, field

from . import matchers

MAX_TEXT_PATTERN = 256
MAX_SET_LABELS = 20
MAX_TEXT_SCAN = 20000  # characters of alert text a pattern is run against
# Identity and display depend on alertname; a rule that renamed alerts would make the list,
# the history and silences disagree about what an alert is called.
PROTECTED_LABELS = frozenset({"alertname"})
_LABEL_NAME = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")


class RuleError(ValueError):
    pass


@dataclass
class RuleView:
    id: str
    name: str
    position: int
    matchers: list[dict]
    text_pattern: str | None
    text_field: str | None
    set_labels: dict[str, str]
    integration_ids: list[str] | None = None
    stop: bool = False
    enabled: bool = True


@dataclass
class Result:
    labels: dict[str, str]
    # label -> name of the rule that set it LAST (what the UI shows beside a label)
    set_by: dict[str, str] = field(default_factory=dict)
    applied: list[str] = field(default_factory=list)


@functools.lru_cache(maxsize=512)
def _text_re(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.IGNORECASE)


def validate_text(pattern, text_field) -> tuple[str | None, str | None]:
    pattern = (pattern or "").strip() or None
    text_field = (text_field or "").strip() or None
    if pattern is None:
        return None, None
    if len(pattern) > MAX_TEXT_PATTERN:
        raise RuleError(f"text pattern longer than {MAX_TEXT_PATTERN} characters")
    try:
        _text_re(pattern)
    except re.error as e:
        raise RuleError(f"text pattern is not a valid regex: {e}") from None
    if text_field and not _LABEL_NAME.fullmatch(text_field):
        raise RuleError(f"text field '{text_field}' is not a valid annotation name")
    return pattern, text_field


def validate_set_labels(value) -> dict[str, str]:
    if not isinstance(value, dict) or not value:
        raise RuleError("set_labels: at least one label to set")
    if len(value) > MAX_SET_LABELS:
        raise RuleError(f"set_labels: at most {MAX_SET_LABELS}")
    out = {}
    for k, v in value.items():
        k = str(k).strip()
        if not _LABEL_NAME.fullmatch(k):
            raise RuleError(f"set_labels: '{k}' is not a valid label name")
        if k in PROTECTED_LABELS:
            raise RuleError(f"set_labels: '{k}' cannot be changed by a rule")
        v = str(v)
        if len(v) > 500 or "\n" in v or "\r" in v:
            raise RuleError(f"set_labels: the value of '{k}' is too long or has a line break")
        out[k] = v
    return out


def alert_text(annotations: dict[str, str], text_field: str | None) -> str:
    """What a text pattern is searched in: one annotation, or all of them ("summary",
    "description", "message", …), one per line."""
    annotations = annotations or {}
    if text_field:
        text = str(annotations.get(text_field, ""))
    else:
        text = "\n".join(str(v) for _, v in sorted(annotations.items()))
    return text[:MAX_TEXT_SCAN]


def explain(rule: RuleView, labels: dict[str, str], annotations: dict[str, str],
            integration_id: str) -> tuple[bool, str]:
    if not rule.enabled:
        return False, "disabled"
    if rule.integration_ids is not None and integration_id not in rule.integration_ids:
        return False, "not from one of this rule's integrations"
    for m in rule.matchers:
        if not matchers.match_one(m, labels):
            return False, f"{matchers.describe([m])} does not match"
    if rule.text_pattern:
        where = f"annotation '{rule.text_field}'" if rule.text_field else "the alert text"
        if not _text_re(rule.text_pattern).search(alert_text(annotations, rule.text_field)):
            return False, f"/{rule.text_pattern}/ not found in {where}"
    return True, "matches"


def apply(rules: list[RuleView], labels: dict[str, str], annotations: dict[str, str],
          integration_id: str) -> Result:
    out = Result(labels=dict(labels))
    for r in sorted(rules, key=lambda r: (r.position, r.name)):
        ok, _ = explain(r, out.labels, annotations, integration_id)
        if not ok:
            continue
        for k, v in r.set_labels.items():
            out.labels[k] = v
            out.set_by[k] = r.name
        out.applied.append(r.name)
        if r.stop:
            break
    return out


def trace(rules: list[RuleView], labels: dict[str, str], annotations: dict[str, str],
          integration_id: str) -> dict:
    """For the tester: every rule's verdict in order, and the labels that come out."""
    current = dict(labels)
    rows, stopped = [], False
    for r in sorted(rules, key=lambda r: (r.position, r.name)):
        if stopped:
            rows.append({"id": r.id, "name": r.name, "applied": False,
                         "why": "not evaluated — an earlier rule has 'stop'", "set": {}})
            continue
        ok, why = explain(r, current, annotations, integration_id)
        if ok:
            current.update(r.set_labels)
            stopped = r.stop
        rows.append({"id": r.id, "name": r.name, "applied": ok, "why": why,
                     "set": dict(r.set_labels) if ok else {}})
    added = {k: v for k, v in current.items() if labels.get(k) != v}
    return {"rules": rows, "labels": current, "changed": added}
