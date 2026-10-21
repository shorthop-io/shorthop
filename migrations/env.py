"""Alembic environment: the app's own engine (Postgres in-cluster, SQLite locally) and the
ORM's metadata."""
from logging.config import fileConfig

from alembic import context

from app import models  # noqa: F401 — registers every table on Base.metadata
from app.db import Base, engine

config = context.config
# Only the alembic CLI configures logging from alembic.ini. When the app migrates itself at
# startup it has already set up JSON logging, and fileConfig would replace the root handler
# and set it to WARN — silently dropping every INFO line after the migration.
if config.config_file_name is not None and not config.attributes.get("from_app"):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(url=str(engine.url), target_metadata=target_metadata,
                      literal_binds=True, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata,
                          render_as_batch=True, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
