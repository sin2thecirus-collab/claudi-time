"""Add availability_status and excluded_companies to candidates.

New fields for Claude Code matching system:
- availability_status: Track if candidate is available for matching
- excluded_companies: Companies the candidate should never be matched with

Revision ID: 027
Revises: 026
Create Date: 2026-02-24
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "027"
down_revision = "026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # availability_status: available / placed / paused / not_interested
    op.add_column(
        "candidates",
        sa.Column("availability_status", sa.String(30), server_default="available", nullable=False),
    )
    # excluded_companies: JSONB array of company names/IDs
    op.add_column(
        "candidates",
        sa.Column("excluded_companies", JSONB, server_default="[]", nullable=False),
    )

    op.create_index("ix_candidates_availability_status", "candidates", ["availability_status"])


def downgrade() -> None:
    op.drop_index("ix_candidates_availability_status", table_name="candidates")
    op.drop_column("candidates", "excluded_companies")
    op.drop_column("candidates", "availability_status")
