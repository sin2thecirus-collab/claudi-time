"""Add postal_code column to companies table.

Die Spalte war im Model definiert aber fehlte in der DB.

Revision ID: 042
Revises: 041
Create Date: 2026-03-17
"""

from alembic import op
import sqlalchemy as sa

revision = "042"
down_revision = "041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "companies",
        sa.Column("postal_code", sa.String(10), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("companies", "postal_code")
