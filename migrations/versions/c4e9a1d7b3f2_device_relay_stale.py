"""devices: relay stale-phone warning (SHOR-10)

Expand-only: two nullable timestamps. NULL = no warning pending / never emailed.

Revision ID: c4e9a1d7b3f2
Revises: a8d3e5f1c2b7
Create Date: 2026-10-16
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c4e9a1d7b3f2"
down_revision: Union[str, None] = "a8d3e5f1c2b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("devices") as batch:
        batch.add_column(sa.Column("relay_signs_out_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("relay_stale_notified_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("devices") as batch:
        batch.drop_column("relay_stale_notified_at")
        batch.drop_column("relay_signs_out_at")
