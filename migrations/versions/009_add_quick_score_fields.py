"""Add Quick-AI Score fields to matches table.

Phase C des Lern-Kreislaufs:
- quick_score: Guenstige KI-Schnellbewertung (0-100)
- quick_reason: 1 Satz Begruendung
- quick_scored_at: Zeitstempel

Revision ID: 009
Revises: 008
Create Date: 2026-02-02
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "009"
down_revision = "008"
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
    # quick_score — KI-Schnellbewertung (0-100)
    if not _column_exists("matches", "quick_score"):
        op.add_column(
            "matches",
            sa.Column("quick_score", sa.Float(), nullable=True),
        )

    # quick_reason — 1 Satz Begruendung
    if not _column_exists("matches", "quick_reason"):
        op.add_column(
            "matches",
            sa.Column("quick_reason", sa.String(200), nullable=True),
        )

    # quick_scored_at — Zeitstempel
    if not _column_exists("matches", "quick_scored_at"):
        op.add_column(
            "matches",
            sa.Column("quick_scored_at", sa.DateTime(timezone=True), nullable=True),
        )

    # Index fuer Quick-Score (fuer Sortierung/Filterung)
    if not _index_exists("ix_matches_quick_score"):
        op.create_index("ix_matches_quick_score", "matches", ["quick_score"])


def downgrade() -> None:
    op.drop_index("ix_matches_quick_score", table_name="matches")
    op.drop_column("matches", "quick_scored_at")
    op.drop_column("matches", "quick_reason")
    op.drop_column("matches", "quick_score")
