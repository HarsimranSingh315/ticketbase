"""add kb_state revision for cross-process index consistency

Revision ID: c4a8f0e2d913
Revises: b7d2e4f19a60
Create Date: 2026-09-25
"""
import sqlalchemy as sa
from alembic import op

revision = "c4a8f0e2d913"
down_revision = "b7d2e4f19a60"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kb_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
    )
    op.execute("INSERT INTO kb_state (id, revision) VALUES (1, 0)")


def downgrade() -> None:
    op.drop_table("kb_state")
