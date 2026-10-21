"""integration archived_at (delete keeps alert history)

Revision ID: c3a1f0d2b7e4
Revises: e08eb1236c6a
Create Date: 2026-10-16
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'c3a1f0d2b7e4'
down_revision: Union[str, None] = 'e08eb1236c6a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('integrations', schema=None) as batch_op:
        batch_op.add_column(sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('integrations', schema=None) as batch_op:
        batch_op.drop_column('archived_at')
