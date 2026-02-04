"""Add rating fields to candidates table.

Sterne-Bewertung (1-5) fuer Kandidaten-Qualitaet.
- rating: Integer 1-5
- rating_set_at: Zeitstempel der Bewertung

Revision ID: 010
Revises: 009
Create Date: 2026-02-04
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "010"
down_revision = "009"
branch_labels = None
depends_on = None


def _column_exists(table_name: str, column_name: str) -> bool:
    """Prueft ob eine Spalte existiert."""
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = :table AND column_name = :col"
        ),
        {"table": table_name, "col": column_name},
    )
    return result.fetchone() is not None


def _index_exists(index_name: str) -> bool:
    """Prueft ob ein Index existiert."""
    conn = op.get_bind()
    result = conn.execute(
        sa.text("SELECT indexname FROM pg_indexes WHERE indexname = :idx"),
        {"idx": index_name},
    )
    return result.fetchone() is not None


def upgrade() -> None:
    # rating (1-5 Integer)
    if not _column_exists("candidates", "rating"):
        op.add_column(
            "candidates",
            sa.Column("rating", sa.Integer(), nullable=True),
        )

    # rating_set_at — Zeitstempel
    if not _column_exists("candidates", "rating_set_at"):
        op.add_column(
            "candidates",
            sa.Column("rating_set_at", sa.DateTime(timezone=True), nullable=True),
        )

    # Index fuer Filter/Sortierung
    if not _index_exists("ix_candidates_rating"):
        op.create_index("ix_candidates_rating", "candidates", ["rating"])


def downgrade() -> None:
    op.drop_index("ix_candidates_rating", table_name="candidates")
    op.drop_column("candidates", "rating_set_at")
    op.drop_column("candidates", "rating")
