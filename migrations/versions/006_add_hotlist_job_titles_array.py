"""Add hotlist_job_titles array + classification_data to candidates/jobs.

Erlaubt mehrere Jobtitel pro Kandidat/Job (z.B. Finanzbuchhalter + Kreditorenbuchhalter).
classification_data speichert OpenAI-Trainingsdaten für den lokalen Algorithmus.

Revision ID: 006
Revises: 005
Create Date: 2026-01-30
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

# revision identifiers
revision = "006"
down_revision = "005"
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
            sa.Column(column_name, sa.Text() if "TEXT" in column_type.upper() else
                      ARRAY(sa.String) if "[]" in column_type else
                      JSONB if "JSONB" in column_type.upper() else
                      sa.String(255)),
        )


def _create_index_if_not_exists(index_name: str, table: str, column: str, using: str = None) -> None:
    """Erstellt einen Index, wenn er noch nicht existiert."""
    conn = op.get_bind()
    result = conn.execute(
        sa.text("SELECT indexname FROM pg_indexes WHERE indexname = :idx"),
        {"idx": index_name},
    )
    if not result.fetchone():
        if using:
            conn.execute(
                sa.text(f"CREATE INDEX {index_name} ON {table} USING {using}({column})")
            )
        else:
            op.create_index(index_name, table, [column])


def upgrade() -> None:
    # Candidates: hotlist_job_titles Array
    _add_column_if_not_exists("candidates", "hotlist_job_titles", "VARCHAR[]")
    _create_index_if_not_exists(
        "ix_candidates_hotlist_job_titles", "candidates", "hotlist_job_titles", using="GIN"
    )

    # Candidates: classification_data JSONB (OpenAI-Trainingsdaten)
    _add_column_if_not_exists("candidates", "classification_data", "JSONB")

    # Jobs: hotlist_job_titles Array
    _add_column_if_not_exists("jobs", "hotlist_job_titles", "VARCHAR[]")
    _create_index_if_not_exists(
        "ix_jobs_hotlist_job_titles", "jobs", "hotlist_job_titles", using="GIN"
    )

    # Bestehende Daten migrieren: hotlist_job_title → hotlist_job_titles Array
    conn = op.get_bind()
    conn.execute(sa.text(
        "UPDATE candidates SET hotlist_job_titles = ARRAY[hotlist_job_title] "
        "WHERE hotlist_job_title IS NOT NULL AND hotlist_job_titles IS NULL"
    ))
    conn.execute(sa.text(
        "UPDATE jobs SET hotlist_job_titles = ARRAY[hotlist_job_title] "
        "WHERE hotlist_job_title IS NOT NULL AND hotlist_job_titles IS NULL"
    ))


def downgrade() -> None:
    op.drop_index("ix_jobs_hotlist_job_titles", table_name="jobs")
    op.drop_column("jobs", "hotlist_job_titles")
    op.drop_index("ix_candidates_hotlist_job_titles", table_name="candidates")
    op.drop_column("candidates", "hotlist_job_titles")
    op.drop_column("candidates", "classification_data")
