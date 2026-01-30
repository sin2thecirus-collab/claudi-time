"""Add deleted_at column to candidates for soft-delete with CRM-Sync protection.

Revision ID: 003
Revises: 002
Create Date: 2026-01-30
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Spalte nur hinzufügen wenn sie nicht existiert (init_db könnte sie schon angelegt haben)
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'candidates' AND column_name = 'deleted_at'"
        )
    )
    if not result.fetchone():
        op.add_column(
            "candidates",
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        )

    # Index nur erstellen wenn er nicht existiert
    result = conn.execute(
        sa.text(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = 'candidates' AND indexname = 'ix_candidates_deleted_at'"
        )
    )
    if not result.fetchone():
        op.create_index("ix_candidates_deleted_at", "candidates", ["deleted_at"])


def downgrade() -> None:
    op.drop_index("ix_candidates_deleted_at", table_name="candidates")
    op.drop_column("candidates", "deleted_at")
