"""Add source_job_id to ats_jobs table.

Verknuepfung zum Quell-Job fuer Cascading Delete.
Wenn ein importierter Job geloescht wird, werden zugehoerige
ATSJobs automatisch mitgeloescht.

Revision ID: 011
Revises: 010
Create Date: 2026-02-06
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers
revision = "011"
down_revision = "010"
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


def _constraint_exists(constraint_name: str) -> bool:
    """Prueft ob ein Constraint existiert."""
    conn = op.get_bind()
    result = conn.execute(
        sa.text("SELECT conname FROM pg_constraint WHERE conname = :name"),
        {"name": constraint_name},
    )
    return result.fetchone() is not None


def upgrade() -> None:
    # source_job_id Spalte
    if not _column_exists("ats_jobs", "source_job_id"):
        op.add_column(
            "ats_jobs",
            sa.Column(
                "source_job_id",
                postgresql.UUID(as_uuid=True),
                nullable=True,
            ),
        )

    # Foreign Key mit CASCADE DELETE
    if not _constraint_exists("fk_ats_jobs_source_job_id"):
        op.create_foreign_key(
            "fk_ats_jobs_source_job_id",
            "ats_jobs",
            "jobs",
            ["source_job_id"],
            ["id"],
            ondelete="CASCADE",
        )

    # Index fuer schnelle Suche
    if not _index_exists("ix_ats_jobs_source_job_id"):
        op.create_index(
            "ix_ats_jobs_source_job_id",
            "ats_jobs",
            ["source_job_id"],
        )


def downgrade() -> None:
    op.drop_index("ix_ats_jobs_source_job_id", table_name="ats_jobs")
    op.drop_constraint("fk_ats_jobs_source_job_id", "ats_jobs", type_="foreignkey")
    op.drop_column("ats_jobs", "source_job_id")
