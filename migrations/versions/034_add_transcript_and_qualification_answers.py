"""Add transcript fields to acquisition_calls and qualification_answers to jobs.

Revision ID: 034
Revises: 033
Create Date: 2026-03-01
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "034"
down_revision = "033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── acquisition_calls: Transkript-Felder ──
    op.add_column("acquisition_calls", sa.Column("transcript", sa.Text(), nullable=True))
    op.add_column("acquisition_calls", sa.Column("call_summary", sa.Text(), nullable=True))
    op.add_column(
        "acquisition_calls",
        sa.Column("transcript_processed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "acquisition_calls",
        sa.Column("webex_recording_id", sa.String(255), nullable=True),
    )

    # ── jobs: Qualifizierungsdaten ──
    op.add_column("jobs", sa.Column("qualification_answers", JSONB, nullable=True))
    op.add_column(
        "jobs",
        sa.Column("qualification_updated_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Index fuer unverarbeitete Calls (Transcript vorhanden, aber noch nicht extrahiert)
    op.execute(sa.text(
        "CREATE INDEX idx_acq_calls_transcript_pending "
        "ON acquisition_calls (created_at DESC) "
        "WHERE transcript IS NOT NULL AND transcript_processed_at IS NULL"
    ))


def downgrade() -> None:
    op.execute(sa.text("DROP INDEX IF EXISTS idx_acq_calls_transcript_pending"))
    op.drop_column("jobs", "qualification_updated_at")
    op.drop_column("jobs", "qualification_answers")
    op.drop_column("acquisition_calls", "webex_recording_id")
    op.drop_column("acquisition_calls", "transcript_processed_at")
    op.drop_column("acquisition_calls", "call_summary")
    op.drop_column("acquisition_calls", "transcript")
