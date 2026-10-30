"""jira_issues: which Jira issue stands for which incident (the Jira channel)

Revision ID: a7c4e2f9b310
Revises: d94963af1d5f
Create Date: 2026-10-26

One new table, expand-only: the previous release runs against it unchanged.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'a7c4e2f9b310'
down_revision: Union[str, None] = 'd94963af1d5f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSONB = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        'jira_issues',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('incident_id', sa.String(length=36), nullable=False),
        sa.Column('channel_id', sa.String(length=36), nullable=True),
        sa.Column('issue_key', sa.String(length=64), nullable=False),
        sa.Column('issue_url', sa.String(length=400), nullable=False),
        sa.Column('dirty', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('dirty_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('pending_comments', JSONB, nullable=False),
        sa.Column('close_pending', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['channel_id'], ['channels.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['incident_id'], ['incidents.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('incident_id', 'channel_id', name='uq_jira_issue_channel'),
    )
    with op.batch_alter_table('jira_issues', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_jira_issues_incident_id'), ['incident_id'], unique=False)
        batch_op.create_index('ix_jira_issue_dirty', ['dirty', 'dirty_at'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('jira_issues', schema=None) as batch_op:
        batch_op.drop_index('ix_jira_issue_dirty')
        batch_op.drop_index(batch_op.f('ix_jira_issues_incident_id'))
    op.drop_table('jira_issues')
