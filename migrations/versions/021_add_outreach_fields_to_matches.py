"""Add outreach tracking fields to matches table.

Phase 11-12: End-to-End Recruiting Pipeline:
- outreach_status (VARCHAR): pending / sent / responded / interested / declined / no_response
- outreach_sent_at (TIMESTAMP): Wann wurde die E-Mail geschickt?
- outreach_responded_at (TIMESTAMP): Wann hat der Kandidat geantwortet?
- candidate_feedback (TEXT): Freitext — Was hat der Kandidat gesagt?
- presentation_status (VARCHAR): not_sent / presented / interview / rejected / hired
- presentation_sent_at (TIMESTAMP): Wann wurde der Kandidat beim Kunden vorgestellt?

Revision ID: 021
Revises: 020
Create Date: 2026-02-16
"""

from alembic import op
import sqlalchemy as sa

revision = "021"
down_revision = "020"
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


COLUMNS = [
    ("outreach_status", sa.String(50)),
    ("outreach_sent_at", sa.DateTime(timezone=True)),
    ("outreach_responded_at", sa.DateTime(timezone=True)),
    ("candidate_feedback", sa.Text),
    ("presentation_status", sa.String(50)),
    ("presentation_sent_at", sa.DateTime(timezone=True)),
]


def upgrade() -> None:
    for col_name, col_type in COLUMNS:
        if not _column_exists("matches", col_name):
            op.add_column(
                "matches",
                sa.Column(col_name, col_type, nullable=True),
            )

    # Index auf outreach_status fuer Outreach-Pipeline-Queries
    conn = op.get_bind()
    for idx_name, idx_col in [
        ("ix_matches_outreach_status", "outreach_status"),
        ("ix_matches_presentation_status", "presentation_status"),
    ]:
        result = conn.execute(
            sa.text(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename = 'matches' AND indexname = :idx"
            ),
            {"idx": idx_name},
        )
        if result.fetchone() is None:
            op.create_index(idx_name, "matches", [idx_col])


def downgrade() -> None:
    op.drop_index("ix_matches_presentation_status", table_name="matches")
    op.drop_index("ix_matches_outreach_status", table_name="matches")
    for col_name, _ in reversed(COLUMNS):
        op.drop_column("matches", col_name)
