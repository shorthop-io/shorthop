"""Label matchers, in Alertmanager's silence shape. Pure.

    {"name": "alertname", "value": "KubePodCrashLooping", "isRegex": false, "isEqual": true}

isEqual=false negates (`!=`, `!~`). A regex is anchored on both ends, as in Alertmanager.

⚠️ Regexes are written by people and evaluated against every open alert, so a pathological
one is a CPU problem for the whole service. Python's `re` backtracks (Alertmanager uses RE2,
which cannot), so patterns are length-capped and compiled once, at write time; a pattern
that does not compile is refused with the compiler's message rather than stored.
"""
import functools
import re

MAX_PATTERN_LEN = 256
MAX_MATCHERS = 20
_LABEL_NAME = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")


class MatcherError(ValueError):
    pass


@functools.lru_cache(maxsize=512)
def _compiled(pattern: str) -> re.Pattern:
    return re.compile(pattern)


def validate(matchers, allow_match_all: bool = False) -> list[dict]:
    """Normalise and check a matcher list. Raises MatcherError with a caller-safe message.

    `allow_match_all` is for ROUTES, where an empty list is the catch-all at the bottom. A
    silence never gets it: a silence that mutes everything is almost never what was meant."""
    if not isinstance(matchers, list):
        raise MatcherError("matchers must be a list")
    if not matchers and allow_match_all:
        return []
    if not matchers:
        raise MatcherError("at least one matcher is required")
    if len(matchers) > MAX_MATCHERS:
        raise MatcherError(f"at most {MAX_MATCHERS} matchers")
    out = []
    for i, m in enumerate(matchers, 1):
        if not isinstance(m, dict):
            raise MatcherError(f"matcher #{i} must be an object")
        name = str(m.get("name", "")).strip()
        value = str(m.get("value", ""))
        is_regex = bool(m.get("isRegex", False))
        is_equal = bool(m.get("isEqual", True))
        if not _LABEL_NAME.fullmatch(name):
            raise MatcherError(f"matcher #{i}: '{name}' is not a valid label name")
        if is_regex:
            if len(value) > MAX_PATTERN_LEN:
                raise MatcherError(f"matcher #{i}: regex longer than {MAX_PATTERN_LEN} chars")
            try:
                _compiled(value)
            except re.error as e:
                raise MatcherError(f"matcher #{i}: invalid regex: {e}") from None
        out.append({"name": name, "value": value, "isRegex": is_regex, "isEqual": is_equal})
    # A silence whose every matcher is negative or matches the empty string would mute
    # EVERYTHING, which is almost never what somebody typed it for.
    if not allow_match_all and not any(_needs_label(m) for m in out):
        raise MatcherError(
            "at least one matcher must require a label value (e.g. alertname=\"X\"); "
            "this set would match every alert"
        )
    return out


def _needs_label(m: dict) -> bool:
    if not m["isEqual"]:
        return False
    if m["isRegex"]:
        return not _compiled(m["value"]).fullmatch("")
    return m["value"] != ""


def match_one(m: dict, labels: dict[str, str]) -> bool:
    # A missing label reads as the empty string — Alertmanager's rule.
    actual = labels.get(m["name"], "")
    if m["isRegex"]:
        hit = _compiled(m["value"]).fullmatch(actual) is not None
    else:
        hit = actual == m["value"]
    return hit if m["isEqual"] else not hit


def matches(matchers: list[dict], labels: dict[str, str]) -> bool:
    return all(match_one(m, labels) for m in matchers)


def describe(matchers: list[dict]) -> str:
    ops = {(False, True): "=", (False, False): "!=", (True, True): "=~", (True, False): "!~"}
    return ", ".join(
        f'{m["name"]}{ops[(m["isRegex"], m["isEqual"])]}"{m["value"]}"' for m in matchers
    )
