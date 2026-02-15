"""Add email_drafts table for automated email sending.

Speichert automatisch generierte E-Mail-Entwuerfe nach Kandidatengespraechen.
Typ kontaktdaten + stellenausschreibung werden sofort gesendet.
Typ individuell wartet auf Recruiter-Pruefung.

Revision ID: 018
Revises: 017
Create Date: 2026-02-15
"""

from alembic import op
import sqlalchemy as sa

revision = "018"
down_revision = "017"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    """Prueft ob eine Tabelle existiert."""
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = :table"
        ),
        {"table": table_name},
    )
    return result.fetchone() is not None


def upgrade() -> None:
    if _table_exists("email_drafts"):
        return

    op.create_table(
        "email_drafts",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("candidate_id", sa.dialects.postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("candidates.id", ondelete="SET NULL"), nullable=True),
        sa.Column("ats_job_id", sa.dialects.postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("ats_jobs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("call_note_id", sa.dialects.postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("ats_call_notes.id", ondelete="SET NULL"), nullable=True),
        sa.Column("email_type", sa.String(50), nullable=False, server_default="individuell"),
        sa.Column("to_email", sa.String(500), nullable=False),
        sa.Column("subject", sa.String(500), nullable=False),
        sa.Column("body_html", sa.Text, nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("gpt_context", sa.Text, nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("send_error", sa.Text, nullable=True),
        sa.Column("microsoft_message_id", sa.String(255), nullable=True),
        sa.Column("auto_send", sa.Boolean, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_index("ix_email_drafts_candidate_id", "email_drafts", ["candidate_id"])
    op.create_index("ix_email_drafts_status", "email_drafts", ["status"])
    op.create_index("ix_email_drafts_created_at", "email_drafts", ["created_at"])


def downgrade() -> None:
    op.drop_table("email_drafts")
