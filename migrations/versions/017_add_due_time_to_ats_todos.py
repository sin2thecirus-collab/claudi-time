"""Add due_time field to ats_todos table.

Uhrzeit-Feld fuer Aufgaben, z.B. "14:00" wenn ein Kandidat
zu einer bestimmten Uhrzeit zurueckgerufen werden soll.

Revision ID: 017
Revises: 016
Create Date: 2026-02-15
"""

from alembic import op
import sqlalchemy as sa

revision = "017"
down_revision = "016"
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


def upgrade() -> None:
    if not _column_exists("ats_todos", "due_time"):
        op.add_column(
            "ats_todos",
            sa.Column("due_time", sa.String(5), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("ats_todos", "due_time")
