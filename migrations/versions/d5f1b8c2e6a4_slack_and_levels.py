"""Slack app (messages per incident, users.slack_user_id) and incident levels

Revision ID: d5f1b8c2e6a4
Revises: c4e9a1d7b3f2
Create Date: 2026-10-20
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'd5f1b8c2e6a4'
down_revision: Union[str, None] = 'c4e9a1d7b3f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSONB = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')


def upgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('slack_user_id', sa.String(length=40), nullable=True))
        batch_op.create_index(batch_op.f('ix_users_slack_user_id'), ['slack_user_id'], unique=False)
    with op.batch_alter_table('incidents', schema=None) as batch_op:
        batch_op.add_column(sa.Column('level', sa.String(length=20), nullable=True))
        # Every existing incident's level is derived, never set by a person.
        batch_op.add_column(sa.Column('level_manual', sa.Boolean(), nullable=False,
                                      server_default=sa.false()))
        batch_op.add_column(sa.Column('level_set_by', sa.String(length=254), nullable=True))
        batch_op.add_column(sa.Column('level_set_at', sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        'slack_messages',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('incident_id', sa.String(length=36), nullable=False),
        sa.Column('conversation', sa.String(length=40), nullable=False),
        sa.Column('ts', sa.String(length=40), nullable=False),
        sa.Column('channel_id', sa.String(length=36), nullable=True),
        sa.Column('user_id', sa.String(length=36), nullable=True),
        sa.Column('dirty', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('dirty_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('pending_replies', JSONB, nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['channel_id'], ['channels.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['incident_id'], ['incidents.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('incident_id', 'conversation', name='uq_slack_message_conv'),
    )
    with op.batch_alter_table('slack_messages', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_slack_messages_incident_id'), ['incident_id'], unique=False)
        batch_op.create_index('ix_slack_message_dirty', ['dirty', 'dirty_at'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('slack_messages', schema=None) as batch_op:
        batch_op.drop_index('ix_slack_message_dirty')
        batch_op.drop_index(batch_op.f('ix_slack_messages_incident_id'))
    op.drop_table('slack_messages')
    with op.batch_alter_table('incidents', schema=None) as batch_op:
        batch_op.drop_column('level_set_at')
        batch_op.drop_column('level_set_by')
        batch_op.drop_column('level_manual')
        batch_op.drop_column('level')
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_users_slack_user_id'))
        batch_op.drop_column('slack_user_id')
