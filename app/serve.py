"""Production WSGI serving (waitress).

One process, many threads — deliberately. The in-process entitlement cache, the public
rate-limit buckets, the startup Alembic migration (no cross-pod lock) and the background
consumer threads all assume exactly one instance per pod, so concurrency comes from
threads and never from worker processes. That also keeps metrics on a plain in-process
registry, with no multiprocess collector to coordinate.

DEBUG=true still uses the Werkzeug development server, for the reloader.
"""

import logging
import os
import signal
import sys
import threading
import time

from . import config as cfg

logger = logging.getLogger(__name__)


def _install_graceful_shutdown(server) -> None:
    """Finish in-flight requests on SIGTERM instead of dropping them mid-response.

    The signal handler only sets an Event: touching sockets from signal context races
    the server's select() loop and blows up with "Bad file descriptor". A watcher thread
    does the real work.

    It first keeps serving for SHUTDOWN_DELAY_SECONDS — the in-code equivalent of a
    preStop sleep — so kubernetes has time to drop this pod from the Service endpoints
    before the listener goes away; otherwise a rolling deploy can still route a request
    at a process that is about to exit.
    """
    sigterm = threading.Event()

    def _handler(signum, frame):  # noqa: ANN001, ARG001
        sigterm.set()

    def _drain() -> None:
        sigterm.wait()
        logger.info(
            "SIGTERM received — serving for %ss, then draining for up to %ss",
            cfg.SHUTDOWN_DELAY_SECONDS,
            cfg.SHUTDOWN_GRACE_SECONDS,
        )
        time.sleep(cfg.SHUTDOWN_DELAY_SECONDS)

        dispatcher = server.task_dispatcher
        deadline = time.monotonic() + cfg.SHUTDOWN_GRACE_SECONDS
        while dispatcher.active_count and time.monotonic() < deadline:
            time.sleep(0.1)
        if dispatcher.active_count:
            logger.warning(
                "%s request(s) still running at the grace deadline — cancelling",
                dispatcher.active_count,
            )
        dispatcher.shutdown(cancel_pending=True, timeout=2)

        logger.info("Shutdown complete")
        logging.shutdown()
        sys.stdout.flush()
        # Hard exit: we are past the drain and must not unwind back through the
        # select() loop, which would fault on the sockets we just tore down.
        os._exit(0)

    signal.signal(signal.SIGTERM, _handler)  # main thread only — run() is the entrypoint
    threading.Thread(target=_drain, name="shutdown", daemon=True).start()


def run(app) -> None:  # noqa: ANN001
    """Serve `app` forever. Blocks."""
    if cfg.DEBUG:
        logger.info("Starting %s on port %s (werkzeug, debug)", cfg.API_TITLE, cfg.PORT)
        app.run(host="0.0.0.0", port=cfg.PORT, debug=True)
        return

    from waitress.server import create_server

    logger.info(
        "Starting %s on port %s (waitress, %s threads)",
        cfg.API_TITLE,
        cfg.PORT,
        cfg.THREADS,
    )
    # PUBLIC_PORT is a second LISTENER, not a second app: waitress gives each socket its own
    # SERVER_PORT, which is what app/exposure.py keys the public door on.
    listen = f"0.0.0.0:{cfg.PORT}"
    if cfg.PUBLIC_PORT:
        if cfg.PUBLIC_PORT == cfg.PORT:
            raise SystemExit("PUBLIC_PORT must differ from PORT")
        listen += f" 0.0.0.0:{cfg.PUBLIC_PORT}"
        logger.info("Public listener on port %s (phone API, pairing, calendar feeds only)",
                    cfg.PUBLIC_PORT)
    server = create_server(
        app,
        listen=listen,
        threads=cfg.THREADS,
        connection_limit=cfg.CONNECTION_LIMIT,
        channel_timeout=cfg.CHANNEL_TIMEOUT,
        # Don't advertise the server and its version in every response.
        ident=None,
        # Keep the X-Forwarded-* headers: waitress drops them by default unless the peer
        # is a configured trusted_proxy, and the public rate limiters read the client IP
        # out of X-Forwarded-For. Trust is unchanged from the Werkzeug server — the
        # header was, and remains, advisory only.
        clear_untrusted_proxy_headers=False,
    )
    _install_graceful_shutdown(server)
    server.run()
