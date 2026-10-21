"""The three inbound doors. Each one authenticates with an integration token of ITS kind and
normalises its wire format into `alerts.IncomingAlert`; everything after that is shared.

1. **Prometheus, directly** — `POST /ingest/prometheus/api/v2/alerts`, Alertmanager's own
   receive API. Configure Prometheus with
       alerting.alertmanagers[].path_prefix: /ingest/prometheus
       alerting.alertmanagers[].authorization.credentials: shi_…
   and it posts here exactly as it would to an Alertmanager. This is the door that removes
   Alertmanager from the path.
2. **Alertmanager webhook** — `POST /ingest/alertmanager`, the `webhook_configs` payload, for
   anyone keeping their Alertmanager (or running both side by side while migrating).
3. **Generic JSON** — `POST /ingest/webhook`, for everything else (Grafana, scripts, CI).

Malformed rows are skipped and counted, never a reason to refuse the batch: a push carries
many alerts, and dropping forty good ones over one bad label is the worse failure.
"""
import datetime as dt
import logging

from flask import Blueprint, jsonify, request

from .. import alerts as alert_ops
from .. import config as cfg
from ..auth import integration_from_request

logger = logging.getLogger(__name__)

bp = Blueprint("ingest", __name__, url_prefix="/ingest")

MAX_LABELS = 100
MAX_KEY = 200
MAX_VALUE = 4000


class _Skip(Exception):
    pass


def _parse_time(value) -> dt.datetime | None:
    if not value or not isinstance(value, str):
        return None
    if value.startswith("0001-01-01"):  # Go's zero time: "not set"
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _str_map(value, what: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > MAX_LABELS:
        raise _Skip(f"{what} must be an object with at most {MAX_LABELS} keys")
    out = {}
    for k, v in value.items():
        k = str(k)
        if not k or len(k) > MAX_KEY:
            raise _Skip(f"{what} key too long or empty")
        out[k] = str(v)[:MAX_VALUE]
    return out


def _body_list(body, key: str | None = None):
    items = body.get(key) if key and isinstance(body, dict) else body
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        return None
    return items


def _deliver(kind: str, items: list, parse):
    integration = integration_from_request(kind)
    if integration is None:
        # OX Agent: Broken Authentication prevented — a sender needs an integration token of this door's kind
        return jsonify({"code": 401, "message": "missing or invalid integration token"}), 401
    if len(items) > cfg.MAX_ALERTS_PER_REQUEST:
        return jsonify({"code": 413, "message":
                        f"at most {cfg.MAX_ALERTS_PER_REQUEST} alerts per request"}), 413
    parsed, skipped = [], 0
    for item in items:
        try:
            if not isinstance(item, dict):
                raise _Skip("alert must be an object")
            parsed.append(parse(item))
        except _Skip as e:
            skipped += 1
            logger.info("ingest %s: skipped one alert: %s", integration.name, e)
    counts = alert_ops.ingest(integration, parsed)
    counts["ignored"] += skipped
    return jsonify(counts), 200


def _parse_am_alert(item: dict) -> alert_ops.IncomingAlert:
    return alert_ops.IncomingAlert(
        labels=_str_map(item.get("labels"), "labels"),
        annotations=_str_map(item.get("annotations"), "annotations"),
        starts_at=_parse_time(item.get("startsAt")),
        ends_at=_parse_time(item.get("endsAt")),
        generator_url=str(item.get("generatorURL") or "")[:2000],
        resolved=str(item.get("status", "")).lower() == "resolved",
    )


@bp.post("/prometheus/api/v2/alerts")
def prometheus_v2():
    """Alertmanager's receive API, as Prometheus calls it: a JSON array of alerts."""
    items = _body_list(request.get_json(silent=True))
    if items is None:
        return jsonify({"code": 400, "message": "expected a JSON array of alerts"}), 400
    return _deliver("prometheus", items, _parse_am_alert)


@bp.post("/alertmanager")
def alertmanager_webhook():
    """Alertmanager `webhook_configs` payload (version 4)."""
    items = _body_list(request.get_json(silent=True), "alerts")
    if items is None:
        return jsonify({"code": 400, "message": "expected an Alertmanager webhook payload"}), 400
    return _deliver("alertmanager", items, _parse_am_alert)


def _parse_generic(item: dict) -> alert_ops.IncomingAlert:
    labels = _str_map(item.get("labels"), "labels")
    if "alertname" not in labels and item.get("name"):
        labels["alertname"] = str(item["name"])[:200]
    if item.get("severity") and "severity" not in labels:
        labels["severity"] = str(item["severity"])[:40]
    annotations = _str_map(item.get("annotations"), "annotations")
    for key in ("summary", "description", "runbook_url"):
        if item.get(key) and key not in annotations:
            annotations[key] = str(item[key])[:MAX_VALUE]
    return alert_ops.IncomingAlert(
        labels=labels,
        annotations=annotations,
        starts_at=_parse_time(item.get("starts_at")),
        ends_at=_parse_time(item.get("ends_at")),
        generator_url=str(item.get("url") or "")[:2000],
        resolved=str(item.get("status", "")).lower() == "resolved",
    )


@bp.post("/webhook")
def generic_webhook():
    """Generic JSON: one alert object, a list of them, or {"alerts": [...]}.

        {"name": "BackupFailed", "severity": "critical", "status": "firing",
         "labels": {"service": "billing"}, "summary": "nightly backup failed"}
    """
    body = request.get_json(silent=True)
    items = _body_list(body, "alerts") if isinstance(body, dict) and "alerts" in body \
        else _body_list(body)
    if items is None:
        return jsonify({"code": 400, "message": "expected a JSON alert object or list"}), 400
    return _deliver("webhook", items, _parse_generic)
