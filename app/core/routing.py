"""Routing and grouping. Pure — takes plain dicts, so the whole rule set is unit-testable.

Routes are a FLAT ordered list: the first enabled route whose matchers all match (and whose
integration filter, if any, includes the alert's integration) takes the alert. No nesting,
no `continue`: that is the Alertmanager feature that made a routing tree impossible to read.
"""
import hashlib
import json
from dataclasses import dataclass

from . import matchers


@dataclass
class RouteView:
    id: str
    name: str
    position: int
    matchers: list[dict]
    integration_ids: list[str] | None
    group_by: list[str]
    enabled: bool = True


def explain(route: RouteView, labels: dict[str, str], integration_id: str) -> tuple[bool, str]:
    """(matches, why) — the "why" is what the routing tester shows."""
    if not route.enabled:
        return False, "disabled"
    if route.integration_ids is not None and integration_id not in route.integration_ids:
        return False, "not from one of this route's integrations"
    for m in route.matchers:
        if not matchers.match_one(m, labels):
            return False, f"{matchers.describe([m])} does not match"
    return True, "all matchers match" if route.matchers else "catch-all (no matchers)"


def pick(routes: list[RouteView], labels: dict[str, str], integration_id: str) -> RouteView | None:
    for r in sorted(routes, key=lambda r: (r.position, r.name)):
        ok, _ = explain(r, labels, integration_id)
        if ok:
            return r
    return None


def group_labels(route: RouteView, labels: dict[str, str]) -> dict[str, str]:
    # A missing label groups as "" — two alerts that both lack it belong together.
    return {k: labels.get(k, "") for k in (route.group_by or [])}


def group_key(route: RouteView, labels: dict[str, str]) -> str:
    raw = json.dumps({"r": route.id, "g": group_labels(route, labels)}, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def inhibited(target: dict[str, str], firing: list[dict[str, str]], rules: list[dict]) -> bool:
    """Is `target` inhibited by any OTHER firing alert under these rules?

    rule = {"source_matchers": [...], "target_matchers": [...], "equal": [label, ...]}
    An alert never inhibits itself — a rule whose source and target both match one alert
    would otherwise mute it."""
    for rule in rules:
        if not matchers.matches(rule["target_matchers"], target):
            continue
        for src in firing:
            if src is target or src == target:
                continue
            if not matchers.matches(rule["source_matchers"], src):
                continue
            if all(src.get(k, "") == target.get(k, "") for k in rule.get("equal", [])):
                return True
    return False
