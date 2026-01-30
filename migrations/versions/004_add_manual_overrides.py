"""Add manual_overrides column to candidates for protecting manual edits from sync/parsing.

Revision ID: 004
Revises: 003
Create Date: 2026-01-30
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers
revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'candidates' AND column_name = 'manual_overrides'"
        )
    )
    if not result.fetchone():
        op.add_column(
            "candidates",
            sa.Column("manual_overrides", JSONB, nullable=True),
        )


def downgrade() -> None:
    op.drop_column("candidates", "manual_overrides")
