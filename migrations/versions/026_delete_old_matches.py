"""Delete all pre-V5 matches for a fresh start.

Removes all matches where matching_method is not 'v5_role_geo'.
This clears old V4 Claude matches and legacy V2/V3 matches.

Revision ID: 026
Revises: 025
Create Date: 2026-02-23
"""

from alembic import op

revision = "026"
down_revision = "025"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "DELETE FROM matches WHERE matching_method != 'v5_role_geo' OR matching_method IS NULL"
    )


def downgrade():
    # Geloeschte Daten koennen nicht wiederhergestellt werden
    pass
