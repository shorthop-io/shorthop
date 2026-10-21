"""Integrations: forward the Watchdog to a Shorthop Watch check (relay SHOR-31 / W7)

Expand-only: three nullable columns, NULL = not forwarded — every existing integration.

Revision ID: d94963af1d5f
Revises: d5f1b8c2e6a4
Create Date: 2026-10-20 21:42:52.205707
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd94963af1d5f'
down_revision: Union[str, None] = 'd5f1b8c2e6a4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('integrations', schema=None) as batch_op:
        batch_op.add_column(sa.Column('watch_check_id', sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column('watch_ping_url', sa.String(length=300), nullable=True))
        batch_op.add_column(sa.Column('watch_forwarded_at', sa.DateTime(timezone=True), nullable=True))



def downgrade() -> None:
    with op.batch_alter_table('integrations', schema=None) as batch_op:
        batch_op.drop_column('watch_forwarded_at')
        batch_op.drop_column('watch_ping_url')
        batch_op.drop_column('watch_check_id')

