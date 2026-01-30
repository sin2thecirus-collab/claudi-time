"""Add deleted_at column to candidates for soft-delete with CRM-Sync protection.

Revision ID: 003
Revises: 002
Create Date: 2026-01-30
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "candidates",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_candidates_deleted_at", "candidates", ["deleted_at"])


def downgrade() -> None:
    op.drop_index("ix_candidates_deleted_at", table_name="candidates")
    op.drop_column("candidates", "deleted_at")
