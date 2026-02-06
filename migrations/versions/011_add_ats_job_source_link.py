"""Add source_job_id and deleted_at to ats_jobs table.

Verknuepft ATSJob mit dem Quell-Job fuer:
- Cascading Soft-Delete: Wenn Job geloescht wird, wird ATSJob auch geloescht
- Dynamische Synchronisation: Aenderungen am Job werden reflektiert

- source_job_id: FK zu jobs.id (nullable, ON DELETE SET NULL)
- deleted_at: Soft-Delete Timestamp

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


def _fk_exists(constraint_name: str) -> bool:
    """Prueft ob ein Foreign Key existiert."""
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT constraint_name FROM information_schema.table_constraints "
            "WHERE constraint_name = :name AND constraint_type = 'FOREIGN KEY'"
        ),
        {"name": constraint_name},
    )
    return result.fetchone() is not None


def upgrade() -> None:
    # source_job_id - Verknuepfung zum Quell-Job
    if not _column_exists("ats_jobs", "source_job_id"):
        op.add_column(
            "ats_jobs",
            sa.Column(
                "source_job_id",
                postgresql.UUID(as_uuid=True),
                nullable=True,
            ),
        )

    # Foreign Key zu jobs.id
    if not _fk_exists("fk_ats_jobs_source_job_id"):
        op.create_foreign_key(
            "fk_ats_jobs_source_job_id",
            "ats_jobs",
            "jobs",
            ["source_job_id"],
            ["id"],
            ondelete="SET NULL",
        )

    # Index fuer schnelle Lookups
    if not _index_exists("ix_ats_jobs_source_job_id"):
        op.create_index(
            "ix_ats_jobs_source_job_id",
            "ats_jobs",
            ["source_job_id"],
        )

    # deleted_at - Soft-Delete Timestamp
    if not _column_exists("ats_jobs", "deleted_at"):
        op.add_column(
            "ats_jobs",
            sa.Column(
                "deleted_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
        )

    # Index fuer deleted_at (wichtig fuer Filter-Performance)
    if not _index_exists("ix_ats_jobs_deleted_at"):
        op.create_index(
            "ix_ats_jobs_deleted_at",
            "ats_jobs",
            ["deleted_at"],
        )


def downgrade() -> None:
    # Indizes entfernen
    op.drop_index("ix_ats_jobs_deleted_at", table_name="ats_jobs")
    op.drop_index("ix_ats_jobs_source_job_id", table_name="ats_jobs")

    # Foreign Key entfernen
    op.drop_constraint("fk_ats_jobs_source_job_id", "ats_jobs", type_="foreignkey")

    # Spalten entfernen
    op.drop_column("ats_jobs", "deleted_at")
    op.drop_column("ats_jobs", "source_job_id")
