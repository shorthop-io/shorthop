"""calendar (iCal) feeds

Revision ID: b7e1f4c9a226
Revises: a4d9e6b1c085
Create Date: 2026-10-16
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'b7e1f4c9a226'
down_revision: Union[str, None] = 'a4d9e6b1c085'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'calendar_feeds',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('kind', sa.String(length=20), nullable=False),
        sa.Column('schedule_id', sa.String(length=36), nullable=True),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('prefix', sa.String(length=16), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_fetched_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('revoked', sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(['schedule_id'], ['schedules.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('token_hash'),
    )
    with op.batch_alter_table('calendar_feeds', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_calendar_feeds_user_id'), ['user_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('calendar_feeds', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_calendar_feeds_user_id'))
    op.drop_table('calendar_feeds')
