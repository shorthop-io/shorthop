"""How a person is paged: their own notification rules. Pure.

    [{"method": "push",  "delay_minutes": 0},
     {"method": "email", "delay_minutes": 5},
     {"method": "push",  "delay_minutes": 0, "severities": ["critical"]},
     {"method": "slack", "delay_minutes": 0}]   # a Slack DM, when the Slack app is set up

When an escalation step reaches a person, every rule whose severity filter admits the
incident becomes one page, sent `delay_minutes` later. A delayed page is DROPPED if the
incident is acknowledged or resolved before it is due — "email me if I have not reacted in
5 minutes" — while an immediate one always goes.

No rules (NULL) = DEFAULT: email and every paired phone at once, which is what Shorthop did
before rules existed, so nobody becomes unreachable by not visiting their account page.
"""

METHODS = ("push", "email", "slack")
MAX_RULES = 10
MAX_DELAY_MINUTES = 240
DEFAULT_RULES = [{"method": "push", "delay_minutes": 0, "severities": None},
                 {"method": "email", "delay_minutes": 0, "severities": None}]


class RuleError(ValueError):
    pass


def validate(rules) -> list[dict]:
    if not isinstance(rules, list) or not rules:
        raise RuleError("at least one rule — otherwise nobody can reach you")
    if len(rules) > MAX_RULES:
        raise RuleError(f"at most {MAX_RULES} rules")
    out, seen = [], set()
    for i, r in enumerate(rules, 1):
        if not isinstance(r, dict):
            raise RuleError(f"rule #{i} must be an object")
        method = r.get("method")
        if method not in METHODS:
            raise RuleError(f"rule #{i}: method must be one of {', '.join(METHODS)}")
        try:
            delay = int(r.get("delay_minutes", 0))
        except (TypeError, ValueError):
            raise RuleError(f"rule #{i}: delay_minutes must be a number") from None
        if not 0 <= delay <= MAX_DELAY_MINUTES:
            raise RuleError(f"rule #{i}: delay must be 0–{MAX_DELAY_MINUTES} minutes")
        sev = r.get("severities")
        if sev is not None:
            if not isinstance(sev, list) or not all(isinstance(s, str) and s.strip() for s in sev):
                raise RuleError(f"rule #{i}: severities must be a list of names, or omitted for all")
            sev = sorted({s.strip().lower() for s in sev}) or None
        key = (method, delay, tuple(sev or ()))
        if key in seen:
            continue  # an exact duplicate would page twice for nothing
        seen.add(key)
        out.append({"method": method, "delay_minutes": delay, "severities": sev})
    return sorted(out, key=lambda r: (r["delay_minutes"], r["method"]))


def immediate_gap(rules: list[dict]) -> bool:
    """True when some severity would reach this person only after a delay (or never at
    once). The page shows it as a warning; the rules are still saved."""
    return not any(r["delay_minutes"] == 0 and r["severities"] is None for r in rules)


def applicable(rules: list[dict] | None, severity: str) -> list[dict]:
    """The rules that fire for an incident of this severity."""
    sev = (severity or "").lower()
    return [r for r in (rules or DEFAULT_RULES)
            if r.get("severities") is None or sev in r["severities"]]
