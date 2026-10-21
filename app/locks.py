"""Cross-pod coordination through Postgres advisory locks.

Shorthop keeps ALL its state in Postgres, so two replicas never disagree about an alert, a
silence or an incident — the problem Alertmanager's gossip mesh has. What two replicas CAN do
is the same background work twice. Three tools, each for one shape of work:

  * **Row claiming** (`FOR UPDATE SKIP LOCKED`, in incidents.run_due / delivery.deliver_pending)
    for queues: escalation steps and notifications. Every replica works the queue; a row is
    claimed by exactly one. No leader needed, and it scales out.
  * **Transaction locks** (`xact_lock`) for a short critical section inside a request: the
    ingest of one integration, numbering a new incident. Released by the commit.
  * **The leader** (`Leader`) for singleton housekeeping: the expiry sweep, watchdogs,
    handovers, the shadow comparison, the heartbeat. One replica runs it; when that replica's
    session dies, Postgres releases the lock and another takes over on its next tick.

MySQL readers: `pg_advisory_lock` ≈ `GET_LOCK()`, `pg_advisory_xact_lock` has no MySQL twin
(GET_LOCK is session-scoped only), and `SKIP LOCKED` is the same in MySQL 8.

On SQLite (tests, local dev) every helper is a no-op and this process is always the leader:
there is only ever one process, and SQLite serialises writers by itself.

Lock keys are small fixed integers in one namespace (classid 0), so they are easy to find in
`pg_locks` — `select * from pg_locks where locktype = 'advisory'`.
"""
import hashlib
import logging
import threading

from prometheus_client import Gauge
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from . import db as dbmod

logger = logging.getLogger(__name__)

KEY_MIGRATE = 7_401_001
KEY_LEADER = 7_401_002
KEY_INCIDENT_NUMBER = 7_401_003

IS_LEADER = Gauge("shorthop_leader",
                  "1 when this replica holds the leader lock and runs the housekeeping tick.")


def key_for(name: str) -> int:
    """A stable signed 64-bit lock key for a string (an integration id, …)."""
    return int.from_bytes(hashlib.sha256(name.encode()).digest()[:8], "big", signed=True)


def xact_lock(session, key: int) -> None:
    """Block until this transaction holds `key`; released at commit or rollback."""
    if dbmod.IS_SQLITE:
        return
    session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})


class Leader:
    """Leader election by a session-level advisory lock on a DEDICATED connection.

    ⚠️ The connection is not from the app's pool: a pooled connection is returned, recycled
    and pre-pinged, and the lock lives exactly as long as the session that took it.

    ⚠️ After a dbdeck switchover the old primary is FENCED (read-only) but alive, so a lock
    held there would still look held while the other replica takes the same lock on the new
    primary — two leaders. `is_leader()` therefore also checks that the connection is on a
    writable primary, and gives the lock up if not.
    """

    def __init__(self, key: int = KEY_LEADER):
        self.key = key
        self._conn = None
        self._engine = None
        self._mu = threading.Lock()

    def _connect(self):
        if self._engine is None:
            self._engine = create_engine(dbmod.engine.url, poolclass=NullPool)
        return self._engine.connect()

    def _drop(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001 — it is being dropped because it is broken
                pass
        self._conn = None
        IS_LEADER.set(0)

    def _writable(self, conn) -> bool:
        row = conn.execute(text(
            "SELECT pg_is_in_recovery(), current_setting('default_transaction_read_only')"
        )).one()
        return (not row[0]) and row[1] == "off"

    def _still_held(self, conn) -> bool:
        return bool(conn.execute(text(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted"
            " AND pid = pg_backend_pid() AND classid = 0 AND objid::bigint = :k AND objsubid = 1"
        ), {"k": self.key}).scalar())

    def is_leader(self) -> bool:
        if dbmod.IS_SQLITE:
            IS_LEADER.set(1)
            return True
        with self._mu:
            try:
                if self._conn is not None:
                    if self._writable(self._conn) and self._still_held(self._conn):
                        self._conn.commit()  # end the read transaction; the lock is session-level
                        return True
                    logger.warning("leader: lost the lock or the primary — stepping down")
                    self._drop()
                conn = self._connect()
                if not self._writable(conn):
                    conn.close()
                    return False
                got = conn.execute(text("SELECT pg_try_advisory_lock(:k)"),
                                   {"k": self.key}).scalar()
                conn.commit()
                if not got:
                    conn.close()
                    return False
                self._conn = conn
                IS_LEADER.set(1)
                logger.info("leader: this replica now runs the housekeeping tick")
                return True
            except Exception:  # noqa: BLE001 — a DB blip means "not leader", never a crash
                logger.exception("leader: check failed")
                self._drop()
                return False

    def release(self) -> None:
        with self._mu:
            if self._conn is not None and not dbmod.IS_SQLITE:
                try:
                    self._conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": self.key})
                    self._conn.commit()
                except Exception:  # noqa: BLE001
                    pass
            self._drop()


leader = Leader()
