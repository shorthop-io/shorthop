"""Reading a Prometheus server's own view: what is firing, and which labels it adds.

The URL comes from an integration's `prometheus_url`, which is set through the API — so it
is user-controlled and every request is checked against the allowed hosts first (Settings →
Prometheus, falling back to PROMETHEUS_ALLOWED_HOSTS).
"""
import json
from urllib.parse import urljoin, urlparse

import requests
import yaml

from . import settings

TIMEOUT = 10
MAX_BYTES = 20 * 1024 * 1024


class PrometheusError(Exception):
    """Caller-safe: the message is shown in the UI."""


def check_url(url: str) -> str:
    """Normalise and validate a Prometheus base URL. Raises PrometheusError."""
    url = (url or "").strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise PrometheusError("use an http(s) URL, e.g. http://prometheus.monitoring:9090")
    if parsed.username or parsed.password:
        raise PrometheusError("the URL must not carry credentials")
    allowed = settings.prometheus_allowed_hosts()
    if parsed.hostname.lower() not in allowed:
        raise PrometheusError(
            f"host '{parsed.hostname}' is not allowed; allowed: {', '.join(allowed) or 'none'} "
            "(Settings → Prometheus hosts)")
    return url


def _get_json(base: str, path: str) -> dict:
    url = urljoin(base + "/", path.lstrip("/"))
    parsed = urlparse(url)
    allowed = settings.prometheus_allowed_hosts()
    # OX Agent: SSRF prevented by hostname allowlist (Settings / PROMETHEUS_ALLOWED_HOSTS), checked here
    if parsed.scheme not in ("http", "https") or (parsed.hostname or "").lower() not in allowed:
        raise PrometheusError("Prometheus host is not allowed")
    try:
        resp = requests.get(url, timeout=TIMEOUT, allow_redirects=False, stream=True)
        body = resp.raw.read(MAX_BYTES + 1, decode_content=True)
    except requests.RequestException as e:
        raise PrometheusError(f"could not reach Prometheus ({type(e).__name__})") from None
    if resp.status_code != 200:
        raise PrometheusError(f"Prometheus answered HTTP {resp.status_code}")
    if len(body) > MAX_BYTES:
        raise PrometheusError("Prometheus response too large")
    try:
        data = json.loads(body)
    except ValueError:
        raise PrometheusError("Prometheus did not answer JSON") from None
    if data.get("status") != "success":
        raise PrometheusError("Prometheus reported an error")
    return data.get("data") or {}


def firing_alerts(base: str) -> list[dict[str, str]]:
    """Label sets of every alert Prometheus considers FIRING (pending ones are not sent)."""
    data = _get_json(base, "/api/v1/alerts")
    return [{str(k): str(v) for k, v in (a.get("labels") or {}).items()}
            for a in data.get("alerts", []) if a.get("state") == "firing"]


def external_label_names(base: str) -> set[str]:
    """The labels Prometheus adds to every alert it SENDS but not to /api/v1/alerts.
    They are left out on both sides of the comparison."""
    data = _get_json(base, "/api/v1/status/config")
    try:
        conf = yaml.safe_load(data.get("yaml") or "") or {}
    except yaml.YAMLError:
        raise PrometheusError("could not read Prometheus's config") from None
    return set(((conf.get("global") or {}).get("external_labels") or {}).keys())
