"""devices: push relay registration (relay_device_id, relay_status, relay_message)

Expand-only: three nullable columns. NULL = this phone was never registered with a relay,
which is the truth for every phone paired before this.

Revision ID: a8d3e5f1c2b7
Revises: 9b5b2cb6afdb
Create Date: 2026-10-16
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a8d3e5f1c2b7"
down_revision: Union[str, None] = "9b5b2cb6afdb"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("devices") as batch:
        batch.add_column(sa.Column("relay_device_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("relay_status", sa.String(16), nullable=True))
        batch.add_column(sa.Column("relay_message", sa.String(500), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("devices") as batch:
        batch.drop_column("relay_message")
        batch.drop_column("relay_status")
        batch.drop_column("relay_device_id")
