"""Add rundmail_eligible_from to candidates.

Manuell angelegte Kandidaten bekommen rundmail_eligible_from = created_at + 6 Monate.
CSV-importierte Kandidaten behalten NULL (= sofort berechtigt).

Revision ID: 031
Revises: 030
Create Date: 2026-02-26
"""

from alembic import op
import sqlalchemy as sa

revision = "031"
down_revision = "030"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "candidates",
        sa.Column("rundmail_eligible_from", sa.Date(), nullable=True),
    )


def downgrade():
    op.drop_column("candidates", "rundmail_eligible_from")
