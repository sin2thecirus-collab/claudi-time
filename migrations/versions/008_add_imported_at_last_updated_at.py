"""Add imported_at and last_updated_at to jobs table.

Trackt wann ein Job erstmals importiert wurde (imported_at) und wann ein
Duplikat-Import den Datensatz zuletzt beruehrt hat (last_updated_at).

Revision ID: 008
Revises: 007
Create Date: 2026-02-01
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "008"
down_revision = "007"
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
    conn = op.get_bind()

    # imported_at — Wann der Job erstmals importiert wurde
    if not _column_exists("jobs", "imported_at"):
        op.add_column(
            "jobs",
            sa.Column("imported_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        # Backfill: bestehende Jobs bekommen created_at als imported_at
        conn.execute(sa.text("UPDATE jobs SET imported_at = created_at WHERE imported_at IS NULL"))

    # last_updated_at — Wann ein Duplikat-Import den Job zuletzt beruehrt hat
    if not _column_exists("jobs", "last_updated_at"):
        op.add_column(
            "jobs",
            sa.Column("last_updated_at", sa.DateTime(timezone=True), nullable=True),
        )

    # Indexes
    if not _index_exists("ix_jobs_imported_at"):
        op.create_index("ix_jobs_imported_at", "jobs", ["imported_at"])
    if not _index_exists("ix_jobs_last_updated_at"):
        op.create_index("ix_jobs_last_updated_at", "jobs", ["last_updated_at"])


def downgrade() -> None:
    op.drop_index("ix_jobs_last_updated_at", table_name="jobs")
    op.drop_index("ix_jobs_imported_at", table_name="jobs")
    op.drop_column("jobs", "last_updated_at")
    op.drop_column("jobs", "imported_at")
