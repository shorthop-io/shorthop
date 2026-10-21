"""my account: timezone, phone, contact rules; delayed pages (notifications.until_ack)

Revision ID: a4d9e6b1c085
Revises: f2c7d0a8e351
Create Date: 2026-10-16
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'a4d9e6b1c085'
down_revision: Union[str, None] = 'f2c7d0a8e351'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSONB = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')


def upgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('timezone', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('phone', sa.String(length=40), nullable=True))
        batch_op.add_column(sa.Column('contact_rules', JSONB, nullable=True))
    with op.batch_alter_table('notifications', schema=None) as batch_op:
        # server_default: every existing page is an immediate one.
        batch_op.add_column(sa.Column('until_ack', sa.Boolean(), nullable=False,
                                      server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table('notifications', schema=None) as batch_op:
        batch_op.drop_column('until_ack')
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('contact_rules')
        batch_op.drop_column('phone')
        batch_op.drop_column('timezone')
