"""Add Claude Matching v4 fields to matches table.

Adds empfehlung, wow_faktor, wow_grund for Claude-based matching.

Revision ID: 023
Revises: 022
Create Date: 2026-02-20
"""

from alembic import op
import sqlalchemy as sa

revision = "023"
down_revision = "022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("matches", sa.Column("empfehlung", sa.String(20), nullable=True))
    op.add_column("matches", sa.Column("wow_faktor", sa.Boolean(), server_default="false", nullable=False))
    op.add_column("matches", sa.Column("wow_grund", sa.Text(), nullable=True))

    # Index fuer Action Board Queries (empfehlung + wow_faktor)
    op.create_index("ix_matches_empfehlung", "matches", ["empfehlung"])
    op.create_index("ix_matches_wow_faktor", "matches", ["wow_faktor"])


def downgrade() -> None:
    op.drop_index("ix_matches_wow_faktor", table_name="matches")
    op.drop_index("ix_matches_empfehlung", table_name="matches")
    op.drop_column("matches", "wow_grund")
    op.drop_column("matches", "wow_faktor")
    op.drop_column("matches", "empfehlung")
