"""label rules + occurrences.rule_labels

Revision ID: e5b2a9c4d713
Revises: d81f4c2a6b90
Create Date: 2026-10-16
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'e5b2a9c4d713'
down_revision: Union[str, None] = 'd81f4c2a6b90'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSONB = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')


def upgrade() -> None:
    op.create_table(
        'label_rules',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('matchers', JSONB, nullable=False),
        sa.Column('text_pattern', sa.String(length=300), nullable=True),
        sa.Column('text_field', sa.String(length=100), nullable=True),
        sa.Column('set_labels', JSONB, nullable=False),
        sa.Column('integration_ids', JSONB, nullable=True),
        sa.Column('stop', sa.Boolean(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('label_rules', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_label_rules_position'), ['position'], unique=False)
    with op.batch_alter_table('occurrences', schema=None) as batch_op:
        batch_op.add_column(sa.Column('rule_labels', JSONB, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('occurrences', schema=None) as batch_op:
        batch_op.drop_column('rule_labels')
    with op.batch_alter_table('label_rules', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_label_rules_position'))
    op.drop_table('label_rules')
