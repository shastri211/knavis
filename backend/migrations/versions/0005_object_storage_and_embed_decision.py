"""object storage for table files, embedding decision on jobs

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-03 10:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('data_tables', schema=None) as batch_op:
        batch_op.add_column(sa.Column('file_key', sa.Text(), nullable=True))

    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('embed_mode', sa.String(length=20), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.drop_column('embed_mode')

    with op.batch_alter_table('data_tables', schema=None) as batch_op:
        batch_op.drop_column('file_key')
