"""Add hotlist categorization fields to candidates/jobs and deepmatch fields to matches.

Revision ID: 005
Revises: 004
Create Date: 2026-01-30
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def _add_column_if_not_exists(table: str, column_name: str, column_type: str) -> None:
    """Fügt eine Spalte hinzu, wenn sie noch nicht existiert."""
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = :table AND column_name = :col"
        ),
        {"table": table, "col": column_name},
    )
    if not result.fetchone():
        op.add_column(
            table,
            sa.Column(column_name, _parse_type(column_type), nullable=True),
        )


def _parse_type(type_str: str):
    """Konvertiert Type-String zu SQLAlchemy-Typ."""
    mapping = {
        "VARCHAR(50)": sa.String(50),
        "VARCHAR(255)": sa.String(255),
        "TIMESTAMPTZ": sa.DateTime(timezone=True),
        "FLOAT": sa.Float(),
        "TEXT": sa.Text(),
    }
    return mapping[type_str]


def upgrade() -> None:
    # Candidates: Hotlist-Felder
    _add_column_if_not_exists("candidates", "hotlist_category", "VARCHAR(50)")
    _add_column_if_not_exists("candidates", "hotlist_city", "VARCHAR(255)")
    _add_column_if_not_exists("candidates", "hotlist_job_title", "VARCHAR(255)")
    _add_column_if_not_exists("candidates", "categorized_at", "TIMESTAMPTZ")

    # Jobs: Hotlist-Felder
    _add_column_if_not_exists("jobs", "hotlist_category", "VARCHAR(50)")
    _add_column_if_not_exists("jobs", "hotlist_city", "VARCHAR(255)")
    _add_column_if_not_exists("jobs", "hotlist_job_title", "VARCHAR(255)")
    _add_column_if_not_exists("jobs", "categorized_at", "TIMESTAMPTZ")

    # Matches: DeepMatch-Felder
    _add_column_if_not_exists("matches", "pre_score", "FLOAT")
    _add_column_if_not_exists("matches", "user_feedback", "VARCHAR(50)")
    _add_column_if_not_exists("matches", "feedback_note", "TEXT")
    _add_column_if_not_exists("matches", "feedback_at", "TIMESTAMPTZ")

    # Indizes erstellen (idempotent mit IF NOT EXISTS)
    conn = op.get_bind()
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_candidates_hotlist_category "
        "ON candidates (hotlist_category)"
    ))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_jobs_hotlist_category "
        "ON jobs (hotlist_category)"
    ))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_matches_pre_score "
        "ON matches (pre_score)"
    ))


def downgrade() -> None:
    # Matches
    op.drop_column("matches", "feedback_at")
    op.drop_column("matches", "feedback_note")
    op.drop_column("matches", "user_feedback")
    op.drop_column("matches", "pre_score")

    # Jobs
    op.drop_column("jobs", "categorized_at")
    op.drop_column("jobs", "hotlist_job_title")
    op.drop_column("jobs", "hotlist_city")
    op.drop_column("jobs", "hotlist_category")

    # Candidates
    op.drop_column("candidates", "categorized_at")
    op.drop_column("candidates", "hotlist_job_title")
    op.drop_column("candidates", "hotlist_city")
    op.drop_column("candidates", "hotlist_category")
