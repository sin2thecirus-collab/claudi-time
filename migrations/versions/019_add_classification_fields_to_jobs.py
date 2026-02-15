"""Add Deep Classification fields to jobs table.

Phase 1 der Matching-Verbesserung:
- classification_data (JSONB): Vollstaendige Klassifizierungsdaten von GPT
  (primary_role, roles, reasoning, sub_level, quality_score, quality_reason, etc.)
- quality_score (VARCHAR): high / medium / low — bestimmt ob Job gematcht wird

Revision ID: 019
Revises: 018
Create Date: 2026-02-15
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "019"
down_revision = "018"
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
    ("classification_data", JSONB),
    ("quality_score", sa.String(20)),
]


def upgrade() -> None:
    for col_name, col_type in COLUMNS:
        if not _column_exists("jobs", col_name):
            op.add_column(
                "jobs",
                sa.Column(col_name, col_type, nullable=True),
            )

    # Index auf quality_score fuer schnelles Filtern (high/medium/low)
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = 'jobs' AND indexname = 'ix_jobs_quality_score'"
        )
    )
    if result.fetchone() is None:
        op.create_index("ix_jobs_quality_score", "jobs", ["quality_score"])


def downgrade() -> None:
    op.drop_index("ix_jobs_quality_score", table_name="jobs")
    for col_name, _ in reversed(COLUMNS):
        op.drop_column("jobs", col_name)
