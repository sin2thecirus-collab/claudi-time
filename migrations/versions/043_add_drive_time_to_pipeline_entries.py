"""Add drive_time fields to ats_pipeline_entries.

Direkte Fahrzeit-Berechnung (Auto + OEPNV) zwischen Kandidat und Job-Standort.

Revision ID: 043
Revises: 042
Create Date: 2026-03-18
"""

from alembic import op
import sqlalchemy as sa

revision = "043"
down_revision = "042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ats_pipeline_entries",
        sa.Column("drive_time_car_min", sa.Integer(), nullable=True),
    )
    op.add_column(
        "ats_pipeline_entries",
        sa.Column("drive_time_transit_min", sa.Integer(), nullable=True),
    )
    op.add_column(
        "ats_pipeline_entries",
        sa.Column("drive_time_car_km", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ats_pipeline_entries", "drive_time_car_km")
    op.drop_column("ats_pipeline_entries", "drive_time_transit_min")
    op.drop_column("ats_pipeline_entries", "drive_time_car_min")
