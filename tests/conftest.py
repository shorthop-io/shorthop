"""Test setup. SQLite by default; set POSTGRES_HOST (+ POSTGRES_PORT, POSTGRESQL_PASSWORD)
to run the same suite against a real Postgres.

⚠️ The environment is set HERE, before anything imports `app`: `app.db` builds its engine at
import time, so whichever module imports it first decides the database.
"""
import os
import tempfile

os.environ.setdefault("SCHEDULER_ENABLED", "false")
os.environ.setdefault("SESSION_COOKIE_SECURE", "false")
os.environ.setdefault("SECRET_KEY", "test-secret")
if not os.environ.get("POSTGRES_HOST"):
    _tmp = tempfile.mkdtemp()
    os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/shorthop-test.db"

import pytest  # noqa: E402

from app import create_app  # noqa: E402
from app.auth import INTEGRATION_TOKEN_PREFIX, TOKEN_PREFIX, new_token  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import ApiToken, Integration, User  # noqa: E402

_app = create_app(start_background=False)


@pytest.fixture()
def app():
    yield _app
    SessionLocal.remove()
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())


@pytest.fixture()
def client(app):
    return app.test_client()


def make_user(email="ann@example.com", role="responder", **kw) -> User:
    u = User(email=email, role=role, **kw)
    SessionLocal.add(u)
    SessionLocal.commit()
    return u


def make_token(user: User, scopes: list[str]) -> str:
    plaintext, digest, prefix = new_token(TOKEN_PREFIX)
    SessionLocal.add(ApiToken(user_id=user.id, name="t", token_hash=digest, prefix=prefix,
                              scopes=",".join(scopes)))
    SessionLocal.commit()
    return plaintext


def make_integration(kind="prometheus", name=None, **kw) -> tuple[Integration, str]:
    plaintext, digest, prefix = new_token(INTEGRATION_TOKEN_PREFIX)
    row = Integration(name=name or f"{kind}-1", kind=kind, token_hash=digest, prefix=prefix,
                      extra_labels=kw.pop("extra_labels", {}), **kw)
    SessionLocal.add(row)
    SessionLocal.commit()
    return row, plaintext


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    """Webhook destinations are resolved and must be public (delivery.webhook_destination_error).
    No test may depend on real DNS: every name resolves to a documentation-free public address
    unless a test overrides `delivery._resolve` itself."""
    from app import delivery

    monkeypatch.setattr(delivery, "_resolve", lambda host, port: ["93.184.216.34"])
