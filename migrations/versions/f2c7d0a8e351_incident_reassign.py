"""incident reassignment (who moved it, when, why)

Revision ID: f2c7d0a8e351
Revises: e5b2a9c4d713
Create Date: 2026-10-16
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'f2c7d0a8e351'
down_revision: Union[str, None] = 'e5b2a9c4d713'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('incidents', schema=None) as batch_op:
        batch_op.add_column(sa.Column('reassigned_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('reassigned_by', sa.String(length=254), nullable=True))
        batch_op.add_column(sa.Column('reassign_note', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('incidents', schema=None) as batch_op:
        batch_op.drop_column('reassign_note')
        batch_op.drop_column('reassigned_by')
        batch_op.drop_column('reassigned_at')
