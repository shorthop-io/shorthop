"""Alert identity: fingerprint and signature. Pure — no database, no Flask.

fingerprint = sha256(integration id + every label). Changes when any label changes.
signature   = sha256(the labels that name the PROBLEM). Survives a pod being replaced.

⚠️ The signature is computed and STORED when an occurrence is written. Changing the ignore
list later must not quietly rewrite which past occurrences count as "the same alert"; a
recompute is a deliberate act.
"""
import hashlib
import json


def _digest(payload) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def fingerprint(integration_id: str, labels: dict[str, str]) -> str:
    return _digest({"i": integration_id, "l": labels})


def signature_labels(labels: dict[str, str], ignore: list[str]) -> dict[str, str]:
    drop = set(ignore)
    # alertname always stays — without it two different rules on one pod would collide.
    return {k: v for k, v in labels.items() if k == "alertname" or k not in drop}


def signature(labels: dict[str, str], ignore: list[str]) -> tuple[str, dict[str, str]]:
    """(signature hash, the labels it was computed from). Not scoped to the integration on
    purpose: the same rule evaluated by two Prometheus servers is the same problem when the
    distinguishing label (e.g. `cluster`) is part of the label set."""
    kept = signature_labels(labels, ignore)
    return _digest(kept), kept
