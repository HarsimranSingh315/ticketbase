"""add ticket_notes for internal notes

Revision ID: d91e3b6a5f27
Revises: c4a8f0e2d913
Create Date: 2026-09-25
"""
import sqlalchemy as sa
from alembic import op

revision = "d91e3b6a5f27"
down_revision = "c4a8f0e2d913"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ticket_notes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ticket_id", sa.Integer(), sa.ForeignKey("tickets.id"), nullable=False),
        sa.Column("author_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_ticket_notes_id", "ticket_notes", ["id"])
    op.create_index("ix_ticket_notes_ticket_id", "ticket_notes", ["ticket_id"])


def downgrade() -> None:
    op.drop_index("ix_ticket_notes_ticket_id", table_name="ticket_notes")
    op.drop_index("ix_ticket_notes_id", table_name="ticket_notes")
    op.drop_table("ticket_notes")
