"""Structured logging.

Copy-paste-per-service, like `db.py` / `metrics.py` / `storage.py`. Two jobs:

  * `setup_logging()`        — one JSON object per line in a cluster, plain text in dev.
  * `init_request_logging(app)` — a request id on **every** line emitted while serving a
    request, plus exactly one access line per request.

⚠️ **Why this file exists in this shape.** Before 2026-09 it was `basicConfig` plus
`logging.getLogger("werkzeug").setLevel(WARNING)` and nothing else, which meant the platform
had *no request log at all*: werkzeug's access log was deliberately silenced (right — it is
the dev server) and **production runs waitress, which has no access log to silence**. So
neither environment logged a request, in either direction. Measured on lab before the fix:
the marina service produced **17 log lines in 24 hours**, all of them startup banners, on a
day it served a full marina. That is the whole reason Loki looked empty.

⚠️ **`force=True` on `basicConfig` is load-bearing.** `basicConfig` is a NO-OP when the root
logger already has a handler, and several things here install one before `create_app` runs
(alembic's own config in `init_db`, and any library that calls `basicConfig` first). Without
`force` the JSON formatter is silently not applied and the service keeps emitting the old
text format — which looks exactly like "the deploy did not land".

⚠️ **The request id lives in a `ContextVar`, not in `flask.g`.** A logging filter that reads
`g` raises outside a request context, so every background thread (marina's AIS consumer, the
shutdown handler) would have to be special-cased. A ContextVar is per-thread by default,
which is exactly right for waitress's `THREADS` model, and defaults cleanly to "" off-request.

⚠️ **A raw path is never logged.** Several public surfaces carry a bearer capability *in the
path* — `/public/marina/threads/<token>`, `/topup/<token>`, `/sign/<token>` — so writing
`request.path` into a log line files a live credential into Loki for the retention period.
Lines carry the matched **url rule** instead, and where there is no useful rule the path goes
through `scrub_path()`, which replaces ids, numbers and opaque segments with placeholders.

⚠️ **`/metrics` is excluded from the access log and that is not an optimisation.** Prometheus
scrapes every 15 s, which is 5 760 lines per service per day — more log volume than the
product itself generates, on every service, forever.

Env knobs (all optional):
    LOG_LEVEL     INFO            root level
    LOG_FORMAT    json | text     defaults to json when deployed, text in local dev
    ACCESS_LOG    true | false    the one-line-per-request log
"""

from __future__ import annotations

import contextvars
import datetime as dt
import json
import logging
import os
import re
import sys
import time
import uuid

from . import config as cfg

SERVICE = cfg.API_TITLE

REQUEST_ID_HEADER = "X-Request-Id"

# Paths that must never produce an access line. `/metrics` is the scrape (see the module
# docstring); the health endpoints are hit by kubelet probes on the same kind of schedule.
_ACCESS_EXCLUDE = ("/metrics", "/healthcheck", "/health", "/healthz")

# The id of the request being served on THIS thread. "" when not serving one.
_request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")

# Everything a handler chose to attach to the access line (org, user, upstream, …).
_request_ctx: contextvars.ContextVar[dict] = contextvars.ContextVar("request_ctx", default={})

# A caller-supplied request id is echoed into a response header and written into a log line,
# so it is untrusted input on two paths that are easy to forget: a CR/LF in it forges log
# lines in text mode (JSON escapes them, dev format does not) and would be a header-injection
# attempt on the way back. Anything not matching this is replaced rather than cleaned — a
# request id has no meaning to us beyond joining lines, so there is nothing to salvage.
_RID_RE = re.compile(r"\A[A-Za-z0-9._-]{8,64}\Z")

_UUID_RE = re.compile(r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")
_OPAQUE_RE = re.compile(r"\A[A-Za-z0-9_-]{16,}\Z")

# Third-party loggers that say nothing at INFO but say it very often.
_QUIET = {
    # The dev server's access log. We have our own now, on both servers, so this stays off
    # even in dev — two access lines per request is worse than one.
    "werkzeug": logging.WARNING,
    "urllib3": logging.WARNING,
    "botocore": logging.WARNING,
    "boto3": logging.WARNING,
    "s3transfer": logging.WARNING,
    # Seven "setup plugin alembic.autogenerate.x" lines on every single boot, on every
    # service. `alembic.runtime.migration` is deliberately NOT quieted — "Running upgrade
    # x -> y" is the record of what a deploy did to the database.
    "alembic.runtime.plugins": logging.WARNING,
}


def request_id() -> str:
    """The current request's id, or "" outside a request. Safe from any thread."""
    return _request_id.get()


def log_context(**fields) -> None:
    """Attach fields to this request's access line.

    Use for facts the handler learns *while* serving — the resolved org, the upstream a
    proxy picked — so they land on the one line that already carries the outcome, rather
    than as a second line that has to be joined back to it.
    """
    current = dict(_request_ctx.get())
    current.update({k: v for k, v in fields.items() if v is not None})
    _request_ctx.set(current)


def scrub_path(path: str) -> str:
    """A path with its variable segments replaced, so it aggregates and carries no secret.

    `/public/marina/threads/9f2c…` -> `/public/marina/threads/<token>`. Anything opaque and
    long enough to be a credential is treated as one: over-redacting a slug costs nothing,
    under-redacting a token writes it to disk for a week.
    """
    out = []
    for seg in path.split("/"):
        if not seg:
            out.append(seg)
        elif _UUID_RE.match(seg):
            out.append("<id>")
        elif seg.isdigit():
            out.append("<n>")
        elif _OPAQUE_RE.match(seg):
            out.append("<token>")
        else:
            out.append(seg)
    return "/".join(out)


def _install_record_factory() -> None:
    """Stamp the current request id onto every LogRecord as it is created.

    ⚠️ A record factory rather than reading the ContextVar inside the formatter, and rather
    than a Filter on the handler. Formatting can happen later and elsewhere than emission —
    pytest's `caplog` re-formats records after the request has ended, a QueueHandler would
    format on another thread — and by then the ContextVar has been reset, so the rid would
    silently be missing from exactly the lines a test is checking. A filter would fix that
    but only for the handlers it is attached to, which is not every handler in the process.
    The factory is the one hook that runs once, at the moment the record exists, while the
    context is still the request's.

    Idempotent: `setup_logging()` is called per `create_app()`, and tests create many apps,
    so wrapping the wrapper each time would build an ever-deeper call chain.
    """
    if getattr(logging.getLogRecordFactory(), "_navdeck", False):
        return
    base = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = base(*args, **kwargs)
        record.rid = _request_id.get()
        return record

    factory._navdeck = True
    logging.setLogRecordFactory(factory)


class _JsonFormatter(logging.Formatter):
    """One JSON object per line. Queried in Grafana as `{...} | json`."""

    # Set once at import: the pod's own identity, so a line can be tied to a build without
    # joining through the pod name (which changes for reasons other than a new build).
    _base = {
        k: v
        for k, v in {
            "service": SERVICE,
            "env": os.getenv("ENVIRONMENT", ""),
            "build": os.getenv("APP_VERSION", ""),
        }.items()
        if v
    }

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            # RFC3339 with milliseconds. Grafana parses it, `sort` orders it, humans read it.
            "ts": dt.datetime.fromtimestamp(record.created, dt.timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            # Lower-cased so a query reads `| json | level="error"` rather than shouting.
            "level": record.levelname.lower(),
            **self._base,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        rid = getattr(record, "rid", "")
        if rid:
            payload["rid"] = rid
        ctx = getattr(record, "ctx", None)
        if isinstance(ctx, dict):
            payload.update(ctx)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        # default=str: a log line must never be the thing that raises. A Decimal, a
        # datetime or a model instance in `ctx` degrades to its repr instead of taking
        # down the handler that was reporting something else going wrong.
        return json.dumps(payload, default=str, ensure_ascii=False)


class _TextFormatter(logging.Formatter):
    """Human format for local dev — the previous format, plus the request id."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s [%(levelname)s] %(name)s:%(ridtag)s %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        rid = getattr(record, "rid", "")
        # A copy under a different name: the format string consumes `ridtag`, so the raw
        # `rid` stays on the record for anything else that reads it.
        record.ridtag = f" [{rid[:8]}]" if rid else ""
        base = super().format(record)
        ctx = getattr(record, "ctx", None)
        if isinstance(ctx, dict) and ctx:
            base += " " + " ".join(f"{k}={v}" for k, v in ctx.items())
        return base


def _json_default() -> bool:
    """JSON when deployed, text on a laptop.

    ⚠️ Two signals: `APP_VERSION` is injected into every pod by the shared chart, and
    `POSTGRES_HOST` is set wherever a real database is. Both are absent locally and in
    tests. Either one means deployed.
    """
    fmt = os.getenv("LOG_FORMAT", "").strip().lower()
    if fmt in ("json", "text"):
        return fmt == "json"
    return bool(os.getenv("APP_VERSION") or getattr(cfg, "POSTGRES_HOST", None))


def setup_logging() -> None:
    _install_record_factory()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter() if _json_default() else _TextFormatter())
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        handlers=[handler],
        force=True,  # see the module docstring — without this the format silently no-ops
    )
    for name, level in _QUIET.items():
        logging.getLogger(name).setLevel(level)


# --- the access log ------------------------------------------------------------------

_access = logging.getLogger("access")


def _route_of(request) -> str:  # noqa: ANN001
    """The url rule, or a scrubbed path where the rule says nothing.

    A `<path:...>` rule matches everything under a prefix, so on the api-gateway every
    proxied call would otherwise log as the single rule `/api/v1/<path:subpath>` — which
    is the one field the line exists to carry.
    """
    rule = getattr(request, "url_rule", None)
    if rule is not None and "<path:" not in rule.rule:
        return rule.rule
    return scrub_path(request.path)


def init_request_logging(app) -> None:  # noqa: ANN001
    """Assign a request id, echo it, and log one line per request.

    Call from `create_app()` — after `init_metrics(app)`, so the two agree on ordering and
    the access line is emitted before the metrics teardown decrements in-flight.
    """
    from flask import g, request

    enabled = os.getenv("ACCESS_LOG", "true").lower() != "false"

    @app.before_request
    def _log_before():  # noqa: ANN202
        # An id supplied by the gateway is KEPT, never regenerated — that is the entire
        # point: one id spans the edge and the service that answered. Anything that does
        # not look like one is discarded, not sanitised (see `_RID_RE`).
        supplied = request.headers.get(REQUEST_ID_HEADER) or ""
        rid = supplied if _RID_RE.match(supplied) else uuid.uuid4().hex
        g.request_id = rid
        _request_id.set(rid)
        _request_ctx.set({})
        g._log_start = time.perf_counter()

    def _emit(status: int) -> None:
        if not enabled or g.pop("_log_done", False):
            return
        if request.path in _ACCESS_EXCLUDE:
            return
        g._log_done = True
        start = g.get("_log_start")
        ctx = {
            "event": "request",
            "method": request.method,
            "route": _route_of(request),
            "status": status,
            **_request_ctx.get(),
        }
        if start is not None:
            ctx["dur_ms"] = round((time.perf_counter() - start) * 1000, 1)
        # Level by outcome, so `{level="error"}` is a real signal rather than a guess:
        # 5xx is ours, 4xx is the caller's and is worth seeing but is not a fault.
        level = logging.ERROR if status >= 500 else logging.WARNING if status >= 400 else logging.INFO
        _access.log(level, "%s %s %s", request.method, ctx["route"], status, extra={"ctx": ctx})

    @app.after_request
    def _log_after(response):  # noqa: ANN202
        rid = g.get("request_id")
        if rid:
            response.headers.setdefault(REQUEST_ID_HEADER, rid)
        _emit(response.status_code)
        return response

    @app.teardown_request
    def _log_teardown(exc=None):  # noqa: ANN001
        # An unhandled exception skips after_request entirely. Flask has already logged the
        # traceback through app.logger by this point, so this only records the outcome —
        # duplicating the traceback would double the volume of the worst days.
        if exc is not None:
            _emit(500)
        _request_id.set("")
        _request_ctx.set({})
