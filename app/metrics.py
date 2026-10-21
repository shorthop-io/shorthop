"""Prometheus metrics.

One plain in-process registry: the service runs as a single process (see `serve.py`), so
there is no multiprocess collector to coordinate.

Scraped at **GET /metrics**, which the api-gateway deliberately does not route — like
`/internal/*` it is reachable in-cluster only.

Cardinality rules for anything added here:
  * label with the Flask *url rule* (`/api/v1/boats/<boat_id>`), never `request.path` —
    every id on this platform is a UUID, so raw paths are an unbounded label;
  * never label with boat_id / vessel_id / user_id / mmsi / invoice number / token;
  * `org_id` only on a metric where the org IS the subject (and the org count is small),
    never as an extra dimension on a general one.
"""

import datetime as dt
import hmac
import logging
import os
import time

from flask import Response, g, request
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, Info
from prometheus_client import generate_latest
from prometheus_client.core import REGISTRY, CounterMetricFamily, GaugeMetricFamily

from . import config as cfg

logger = logging.getLogger(__name__)

SERVICE = cfg.API_TITLE

# Latency buckets tuned for this platform rather than the library defaults (.005 … 10):
# in-LAN MySQL round-trips dominate, so we want resolution from ~10 ms upward.
_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

# --- the shared RED baseline -------------------------------------------------------
REQUESTS = Counter(
    "http_requests_total",
    "HTTP requests handled, by outcome.",
    ["service", "method", "route", "status"],
)
LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency.",
    ["service", "method", "route"],
    buckets=_BUCKETS,
)
IN_FLIGHT = Gauge(
    "http_requests_in_flight",
    "Requests currently being served.",
    ["service"],
)
EXCEPTIONS = Counter(
    "http_request_exceptions_total",
    "Requests that ended in an unhandled exception.",
    ["service", "route", "exc_type"],
)

SERVICE_INFO = Info("service", "Build and schema identity of this service.")

# --- periodic ticks ----------------------------------------------------------------
# Work actually done by a CronJob tick. A tick that runs green but processes nothing is
# the silent failure mode the JobRun timestamps alone cannot show.
JOB_ITEMS = Counter(
    "job_items_processed_total",
    "Items processed by a periodic job run (invoices raised, trips derived, …).",
    ["service", "job"],
)


def record_job_items(job: str, count: int) -> None:
    """Count the work a periodic tick actually did. Call from `record_job_run`.

        alert:  increase(job_items_processed_total{job="generate-recurring"}[36h]) == 0
    """
    if count:
        JOB_ITEMS.labels(SERVICE, job).inc(count)


_METRICS_PATH = "/metrics"
_job_collector_registered = False


def _route() -> str:
    """The matched url rule. Unmatched requests collapse to one label so that a scanner
    hammering random paths cannot blow up the series count."""
    rule = getattr(request, "url_rule", None)
    return rule.rule if rule is not None else "<unmatched>"


class _JobRunCollector:
    """Projects this service's `job_runs` table into metrics at scrape time.

    Read from the table rather than counted in memory on purpose: the ticks arrive as an
    external curl to a ClusterIP, and the numbers must survive a pod restart.

        alert:  time() - job_last_success_timestamp_seconds{job="..."} > 2.5 * schedule
    """

    def __init__(self, session_factory, job_run_model):
        self._session = session_factory
        self._model = job_run_model

    def collect(self):
        last_success = GaugeMetricFamily(
            "job_last_success_timestamp_seconds",
            "Unix time of the last successful run of a periodic job.",
            labels=["service", "job"],
        )
        last_run = GaugeMetricFamily(
            "job_last_run_timestamp_seconds",
            "Unix time of the last run of a periodic job, successful or not.",
            labels=["service", "job"],
        )
        runs = CounterMetricFamily(
            "job_runs",
            "Total runs of a periodic job.",
            labels=["service", "job"],
        )
        ok = GaugeMetricFamily(
            "job_last_run_ok",
            "1 if the last run of a periodic job succeeded, 0 if it failed.",
            labels=["service", "job"],
        )
        # The row loop is inside the guard too, not just the query: reading the columns is
        # exactly where an unexpected model shape blows up, and this same collect() runs at
        # REGISTRY.register() time, so an escape here takes the whole service down rather
        # than one scrape.
        try:
            from sqlalchemy import select

            db = self._session()
            rows = db.scalars(select(self._model)).all()
            for row in rows:
                labels = [SERVICE, row.job]
                if row.last_success_at:
                    last_success.add_metric(labels, _epoch(row.last_success_at))
                if row.last_run_at:
                    last_run.add_metric(labels, _epoch(row.last_run_at))
                runs.add_metric(labels, row.runs or 0)
                ok.add_metric(labels, 1.0 if row.last_status == "ok" else 0.0)
        except Exception:  # noqa: BLE001 — a scrape must never take the service down
            logger.exception("job metrics unavailable")
            return

        yield last_success
        yield last_run
        yield runs
        yield ok


def _epoch(value: dt.datetime) -> float:
    """job_runs timestamps are stored naive-UTC (see record_job_run)."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.timestamp()


def _schema_revision() -> str:
    """The Alembic revision this pod is actually running — the fastest way to spot a pod
    that adopted the schema but never got a migration (see the adoption pitfall)."""
    try:
        from sqlalchemy import text

        from .db import engine

        with engine.connect() as conn:
            row = conn.execute(text("SELECT version_num FROM alembic_version")).first()
        return row[0] if row else "none"
    except Exception:  # noqa: BLE001 — telemetry has no SQL schema; nothing here is critical
        return "unknown"


# The columns the tick-bookkeeping JobRun actually has (see any service with CronJob ticks).
_TICK_FIELDS = ("job", "last_run_at", "last_success_at", "last_status", "runs")


def _register_job_collector() -> None:
    """Wire up the JobRun projection, for the services that have periodic ticks."""
    global _job_collector_registered
    if _job_collector_registered:
        return
    try:
        from .db import SessionLocal
        from .models import JobRun
    except ImportError:
        return  # service has no periodic jobs (or no SQL models) — nothing to project
    # Match on SHAPE, not just the class name. `JobRun` is generic enough that a service can
    # mean something else by it — another service's is an async job record (id/action/
    # target/status/log), not tick bookkeeping — and registering the collector against that
    # model raised AttributeError from inside REGISTRY.register(), which calls collect() to
    # read the metric names. That propagated out of init_metrics() → create_app() and the
    # service could not boot at all. A metrics collector must never be able to do that.
    missing = [f for f in _TICK_FIELDS if not hasattr(JobRun, f)]
    if missing:
        logger.info(
            "models.JobRun is not the periodic-tick shape (no %s); job metrics not registered",
            ", ".join(missing),
        )
        return
    REGISTRY.register(_JobRunCollector(SessionLocal, JobRun))
    _job_collector_registered = True


def init_metrics(app) -> None:  # noqa: ANN001
    """Instrument `app` and expose /metrics. Safe to call once per created app."""

    SERVICE_INFO.info(
        {
            "service": SERVICE,
            # The API contract version ("v1") — NOT the build. `build` below is the one
            # that answers "which commit is this pod running".
            "version": cfg.API_VERSION,
            # The image tag, injected by the chart from the same value the deployment
            # pointer pins. Empty on a pod predating that chart change, or in local dev.
            "build": os.getenv("APP_VERSION", ""),
            "schema_revision": _schema_revision(),
        }
    )
    _register_job_collector()

    @app.before_request
    def _metrics_before():  # noqa: ANN202
        if request.path == _METRICS_PATH:
            return
        g._metrics_start = time.perf_counter()
        IN_FLIGHT.labels(SERVICE).inc()

    def _observe(status: int) -> None:
        start = g.pop("_metrics_start", None)
        if start is None or g.pop("_metrics_observed", False):
            return
        g._metrics_observed = True
        route, method = _route(), request.method
        REQUESTS.labels(SERVICE, method, route, str(status)).inc()
        LATENCY.labels(SERVICE, method, route).observe(time.perf_counter() - start)

    @app.after_request
    def _metrics_after(response):  # noqa: ANN202
        _observe(response.status_code)
        return response

    @app.teardown_request
    def _metrics_teardown(exc=None):  # noqa: ANN001
        if not g.get("_metrics_start") and not g.get("_metrics_observed"):
            return  # /metrics itself, or a request that never started
        if exc is not None:
            # An unhandled exception skips after_request entirely, so record it here.
            EXCEPTIONS.labels(SERVICE, _route(), type(exc).__name__).inc()
            _observe(500)
        IN_FLIGHT.labels(SERVICE).dec()

    @app.route(_METRICS_PATH)
    def _metrics_endpoint():  # noqa: ANN202
        # /metrics answers only on the private listener. Set METRICS_TOKEN to require a bearer
        # token there as well (e.g. when the private listener is shared with other networks).
        expected = getattr(cfg, "METRICS_TOKEN", "")
        if expected:
            supplied = request.headers.get("Authorization", "")
            supplied = supplied.removeprefix("Bearer ").strip()
            # compare_digest: constant-time, so the token can't be recovered by timing.
            if not hmac.compare_digest(supplied, expected):
                return Response("unauthorized\n", status=401, mimetype="text/plain")
        return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)
