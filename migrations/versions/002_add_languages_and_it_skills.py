"""Add languages and it_skills columns to candidates.

Revision ID: 002
Revises: 001
Create Date: 2026-01-27
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers
revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "candidates",
        sa.Column("languages", postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "candidates",
        sa.Column("it_skills", postgresql.ARRAY(sa.String()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("candidates", "it_skills")
    op.drop_column("candidates", "languages")
