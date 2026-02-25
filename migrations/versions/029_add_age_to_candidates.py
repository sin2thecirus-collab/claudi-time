"""Add age column to candidates.

Stores calculated age as integer for fast DB-level filtering.
Must be refreshed before matching runs (UPDATE from birth_date).

- age IS NULL = kein Geburtsdatum bekannt -> wird trotzdem gematcht
- age >= 58 = wird vom Matching ausgeschlossen

Revision ID: 029
Revises: 028
Create Date: 2026-02-25
"""

from alembic import op
import sqlalchemy as sa

revision = "029"
down_revision = "028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # age: Integer, nullable (NULL = kein Geburtsdatum bekannt)
    op.add_column(
        "candidates",
        sa.Column("age", sa.Integer, nullable=True),
    )

    # Index fuer schnelle Filterung beim Matching
    op.create_index("ix_candidates_age", "candidates", ["age"])

    # Sofort befuellen aus birth_date
    op.execute("""
        UPDATE candidates
        SET age = EXTRACT(YEAR FROM AGE(CURRENT_DATE, birth_date))::integer
        WHERE birth_date IS NOT NULL
    """)


def downgrade() -> None:
    op.drop_index("ix_candidates_age", table_name="candidates")
    op.drop_column("candidates", "age")
