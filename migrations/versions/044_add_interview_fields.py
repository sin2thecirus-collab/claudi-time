"""Add interview scheduling fields to ats_pipeline_entries.

Interview-Termin-Planung: Datum, Uhrzeit, Art (vor_ort/digital),
Teilnehmer, Einladungs-Tracking via Microsoft Graph Calendar.

Revision ID: 044
Revises: 043
Create Date: 2026-03-18
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "044"
down_revision = "043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Neue ActivityType Enum-Werte hinzufuegen
    op.execute("ALTER TYPE activitytype ADD VALUE IF NOT EXISTS 'interview_scheduled'")
    op.execute("ALTER TYPE activitytype ADD VALUE IF NOT EXISTS 'interview_cancelled'")

    op.add_column(
        "ats_pipeline_entries",
        sa.Column("interview_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "ats_pipeline_entries",
        sa.Column("interview_type", sa.String(10), nullable=True),
    )
    op.add_column(
        "ats_pipeline_entries",
        sa.Column("interview_location", sa.Text(), nullable=True),
    )
    op.add_column(
        "ats_pipeline_entries",
        sa.Column("interview_hint", sa.Text(), nullable=True),
    )
    op.add_column(
        "ats_pipeline_entries",
        sa.Column("interview_participants", JSONB, nullable=True),
    )
    op.add_column(
        "ats_pipeline_entries",
        sa.Column("interview_invite_by", sa.String(10), nullable=True),
    )
    op.add_column(
        "ats_pipeline_entries",
        sa.Column(
            "interview_invite_sent",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )
    op.add_column(
        "ats_pipeline_entries",
        sa.Column("interview_event_id", sa.String(500), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ats_pipeline_entries", "interview_event_id")
    op.drop_column("ats_pipeline_entries", "interview_invite_sent")
    op.drop_column("ats_pipeline_entries", "interview_invite_by")
    op.drop_column("ats_pipeline_entries", "interview_participants")
    op.drop_column("ats_pipeline_entries", "interview_hint")
    op.drop_column("ats_pipeline_entries", "interview_location")
    op.drop_column("ats_pipeline_entries", "interview_type")
    op.drop_column("ats_pipeline_entries", "interview_at")
