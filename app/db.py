"""Database engine, session and startup migration.

Postgres in-cluster (`POSTGRES_HOST` set), a local SQLite file otherwise so that dev and the
test suite need no infrastructure. The models are written for Postgres first — JSON columns
become JSONB there — and stay portable enough for SQLite to run the suite.

Startup runs `alembic upgrade head` under a Postgres advisory lock, so two replicas starting
together migrate one after the other: the second waits, then finds nothing left to do.

⚠️ After a database switchover the pool still holds connections to the OLD primary, which is
fenced read-only and alive — so every write fails with "read-only transaction" (SQLSTATE
25006) while every pre-ping passes. `_readonly_is_disconnect` marks that error as a
disconnect, which makes SQLAlchemy throw the whole pool away; the next checkout resolves
the database host again and lands on the new primary. Without it Shorthop pages nobody until a restart.
"""
import logging
from pathlib import Path
from urllib.parse import quote_plus

from sqlalchemy import JSON, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, scoped_session, sessionmaker

from . import config as cfg

logger = logging.getLogger(__name__)

# JSONB on Postgres (indexable with GIN, `@>` containment queries), plain JSON elsewhere.
JSONType = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


def database_url() -> str:
    if cfg.POSTGRES_HOST:
        pwd = quote_plus(cfg.POSTGRES_PASSWORD or "")
        return (
            f"postgresql+psycopg://{cfg.POSTGRES_USER}:{pwd}@{cfg.POSTGRES_HOST}:"
            f"{cfg.POSTGRES_PORT}/{cfg.POSTGRES_DB}?sslmode={cfg.POSTGRES_SSLMODE}"
        )
    return cfg.DATABASE_URL


_url = database_url()
IS_SQLITE = _url.startswith("sqlite")

engine = create_engine(
    _url,
    echo=cfg.SQL_ECHO,
    pool_pre_ping=True,
    connect_args={"check_same_thread": False} if IS_SQLITE else {},
)

if IS_SQLITE:

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - exercised by every test
        # WAL + busy_timeout: request threads and the scheduler thread write concurrently.
        # foreign_keys: SQLite ignores them by default while Postgres enforces them, and a
        # cascade must behave the same in tests as in production.
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

# 25006 read_only_sql_transaction, 57P01 admin_shutdown (dbdeck disconnects clients on a
# switch), 40001 is NOT here: a serialization failure is retryable on the same server.
_STALE_PRIMARY_SQLSTATES = {"25006", "57P01"}


@event.listens_for(engine, "handle_error")
def _readonly_is_disconnect(ctx):  # pragma: no cover - needs a fenced Postgres
    orig = getattr(ctx, "original_exception", None)
    state = getattr(orig, "sqlstate", None) or getattr(getattr(orig, "diag", None), "sqlstate", None)
    if state in _STALE_PRIMARY_SQLSTATES:
        logger.warning("database answered %s — dropping the connection pool (switchover?)", state)
        ctx.is_disconnect = True
        ctx.invalidate_pool_on_disconnect = True


SessionLocal = scoped_session(
    sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
)

_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def _alembic_config():
    from alembic.config import Config

    ini = _MIGRATIONS_DIR.parent / "alembic.ini"
    conf = Config(str(ini))
    conf.set_main_option("script_location", str(_MIGRATIONS_DIR))
    conf.attributes["from_app"] = True
    return conf


def init_db() -> None:
    """Bring the schema to head. A fresh database is built by the baseline migration."""
    from alembic import command

    from . import models  # noqa: F401 — register every table on Base.metadata

    if IS_SQLITE:
        command.upgrade(_alembic_config(), "head")
    else:
        from sqlalchemy import text

        from .locks import KEY_MIGRATE

        # Session-level lock on its own connection, held for the whole upgrade (which opens
        # its own connections). A replica that dies mid-migration releases it with its session.
        with engine.connect() as lock_conn:
            lock_conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": KEY_MIGRATE})
            lock_conn.commit()
            try:
                command.upgrade(_alembic_config(), "head")
            finally:
                lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": KEY_MIGRATE})
                lock_conn.commit()
    logger.info("Database schema at head (%s)", "sqlite" if IS_SQLITE else "postgres")
