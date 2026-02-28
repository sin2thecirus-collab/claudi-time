"""Add scheduled_send_at to acquisition_emails for delayed sending.

Revision ID: 033
Revises: 032
Create Date: 2026-02-28
"""

from alembic import op
import sqlalchemy as sa

revision = "033"
down_revision = "032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "acquisition_emails",
        sa.Column("scheduled_send_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Partieller Index fuer n8n Cron-Job: nur geplante E-Mails
    op.execute(sa.text(
        "CREATE INDEX idx_acq_emails_scheduled "
        "ON acquisition_emails (scheduled_send_at) "
        "WHERE status = 'scheduled' AND scheduled_send_at IS NOT NULL"
    ))


def downgrade() -> None:
    op.execute(sa.text("DROP INDEX IF EXISTS idx_acq_emails_scheduled"))
    op.drop_column("acquisition_emails", "scheduled_send_at")
