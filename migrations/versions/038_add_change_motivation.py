"""Add change_motivation field to candidates.

Wechselmotivation: Warum will der Kandidat wechseln?
Wird automatisch aus Webex-Gespraechen extrahiert (n8n -> GPT).

Revision ID: 038
Revises: 037
Create Date: 2026-03-13
"""

from alembic import op
import sqlalchemy as sa

revision = "038"
down_revision = "037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "candidates",
        sa.Column("change_motivation", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("candidates", "change_motivation")
