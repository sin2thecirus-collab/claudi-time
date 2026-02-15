"""Add drive time fields to matches table.

Phase 10 der Matching-Verbesserung:
- drive_time_car_min (INTEGER): Fahrzeit mit dem Auto in Minuten (Google Maps)
- drive_time_transit_min (INTEGER): Fahrzeit mit ÖPNV in Minuten (Google Maps)

Revision ID: 020
Revises: 019
Create Date: 2026-02-16
"""

from alembic import op
import sqlalchemy as sa

revision = "020"
down_revision = "019"
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
    ("drive_time_car_min", sa.Integer),
    ("drive_time_transit_min", sa.Integer),
]


def upgrade() -> None:
    for col_name, col_type in COLUMNS:
        if not _column_exists("matches", col_name):
            op.add_column(
                "matches",
                sa.Column(col_name, col_type(), nullable=True),
            )

    # Index auf drive_time_car_min fuer Fahrzeit-Filter
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = 'matches' AND indexname = 'ix_matches_drive_time_car_min'"
        )
    )
    if result.fetchone() is None:
        op.create_index("ix_matches_drive_time_car_min", "matches", ["drive_time_car_min"])


def downgrade() -> None:
    op.drop_index("ix_matches_drive_time_car_min", table_name="matches")
    for col_name, _ in reversed(COLUMNS):
        op.drop_column("matches", col_name)
