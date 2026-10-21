"""Shadow comparison: is what Prometheus says is firing what Shorthop has open? Pure.

Prometheus's /api/v1/alerts returns an alert's labels WITHOUT the external labels it adds on
the way out (and after `alert_relabel_configs`, which on kube-prometheus-stack drops
`prometheus_replica`). So both sides are compared on their labels minus `ignored` — the
external label names, the integration's own extra_labels, and any label Shorthop's label
rules set (those are ours, not the sender's) — never on the fingerprint.

⚠️ A difference only COUNTS once it is seen in two consecutive checks. Prometheus sends on
its own evaluation cycle, so an alert that started firing two seconds before a check is
legitimately not in Shorthop yet, and one that just resolved may still be open here for a
moment. Reporting those would make the check cry wolf every few minutes.
"""
import hashlib
import json

WATCHDOGS = {"Watchdog", "DeadMansSwitch"}
ALWAYS_IGNORED = {"prometheus_replica"}


def key(labels: dict[str, str], ignored: set[str]) -> str:
    kept = {k: v for k, v in labels.items() if k not in ignored and k not in ALWAYS_IGNORED}
    raw = json.dumps(kept, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def compare(prom_firing: list[dict[str, str]], shorthop_open: list[dict[str, str]],
            ignored: set[str], previous: dict | None) -> dict:
    """Returns the status to store: counts, the CONFIRMED differences (seen last time too),
    and this check's raw keys for the next comparison."""
    prom = {key(l, ignored): l for l in prom_firing if l.get("alertname") not in WATCHDOGS}
    mine = {key(l, ignored): l for l in shorthop_open if l.get("alertname") not in WATCHDOGS}
    missing_now = set(prom) - set(mine)
    extra_now = set(mine) - set(prom)
    prev_missing = set((previous or {}).get("missing_keys", []))
    prev_extra = set((previous or {}).get("extra_keys", []))

    def show(keys, source):
        return sorted(({"alertname": source[k].get("alertname", ""),
                        "labels": {kk: vv for kk, vv in source[k].items()
                                   if kk not in ignored and kk not in ALWAYS_IGNORED}}
                       for k in keys), key=lambda x: (x["alertname"], json.dumps(x["labels"], sort_keys=True)))

    missing = missing_now & prev_missing
    extra = extra_now & prev_extra
    return {
        "prometheus_firing": len(prom),
        "shorthop_open": len(mine),
        "missing": show(missing, prom),   # firing in Prometheus, not open in Shorthop
        "extra": show(extra, mine),       # open in Shorthop, not firing in Prometheus
        "unconfirmed": len((missing_now - missing) | (extra_now - extra)),
        "missing_keys": sorted(missing_now),
        "extra_keys": sorted(extra_now),
    }
