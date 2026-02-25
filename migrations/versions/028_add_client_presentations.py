"""Add client_presentations table for candidate-to-company presentations.

Tracks the full presentation lifecycle:
- Initial presentation email (AI-generated or custom)
- Follow-up sequence (2 days + 3 days reminders)
- Client response classification (AI-powered)
- Fallback email cascade (bewerber@, karriere@, hr@ etc.)

Revision ID: 028
Revises: 027
Create Date: 2026-02-25
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "028"
down_revision = "027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "client_presentations",
        # Primaerschluessel
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        # Verknuepfungen
        sa.Column("match_id", sa.dialects.postgresql.UUID(as_uuid=True), sa.ForeignKey("matches.id", ondelete="SET NULL")),
        sa.Column("candidate_id", sa.dialects.postgresql.UUID(as_uuid=True), sa.ForeignKey("candidates.id", ondelete="SET NULL")),
        sa.Column("job_id", sa.dialects.postgresql.UUID(as_uuid=True), sa.ForeignKey("jobs.id", ondelete="SET NULL")),
        sa.Column("company_id", sa.dialects.postgresql.UUID(as_uuid=True), sa.ForeignKey("companies.id", ondelete="SET NULL")),
        sa.Column("contact_id", sa.dialects.postgresql.UUID(as_uuid=True), sa.ForeignKey("company_contacts.id", ondelete="SET NULL")),
        # E-Mail-Inhalte
        sa.Column("email_to", sa.String(500), nullable=False),
        sa.Column("email_from", sa.String(500), nullable=False),
        sa.Column("email_subject", sa.String(500), nullable=False),
        sa.Column("email_body_text", sa.Text),
        sa.Column("email_signature_html", sa.Text),
        sa.Column("mailbox_used", sa.String(100)),
        # Vorstellungsmodus
        sa.Column("presentation_mode", sa.String(20), server_default="ai_generated"),
        # PDF-Anhang
        sa.Column("pdf_attached", sa.Boolean, server_default="true"),
        sa.Column("pdf_r2_key", sa.String(500)),
        # Sequenz-Tracking
        sa.Column("status", sa.String(30), server_default="sent"),
        sa.Column("sequence_active", sa.Boolean, server_default="true"),
        sa.Column("sequence_step", sa.Integer, server_default="1"),
        # Zeitstempel
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("followup1_sent_at", sa.DateTime(timezone=True)),
        sa.Column("followup2_sent_at", sa.DateTime(timezone=True)),
        # Kunden-Antwort
        sa.Column("client_response_category", sa.String(30)),
        sa.Column("client_response_text", sa.Text),
        sa.Column("client_response_raw", sa.Text),
        sa.Column("responded_at", sa.DateTime(timezone=True)),
        # Wiedervorlage
        sa.Column("callback_date", sa.DateTime(timezone=True)),
        # Auto-Reply
        sa.Column("auto_reply_sent", sa.Boolean, server_default="false"),
        sa.Column("auto_reply_text", sa.Text),
        # Fallback-Kaskade
        sa.Column("is_fallback", sa.Boolean, server_default="false"),
        sa.Column("fallback_domain", sa.String(255)),
        sa.Column("fallback_attempts", JSONB),
        sa.Column("fallback_successful_email", sa.String(500)),
        # n8n-Integration
        sa.Column("n8n_execution_id", sa.String(255)),
        # Dokumentation-Links
        sa.Column("correspondence_id", sa.dialects.postgresql.UUID(as_uuid=True), sa.ForeignKey("company_correspondence.id", ondelete="SET NULL")),
        sa.Column("pipeline_entry_id", sa.dialects.postgresql.UUID(as_uuid=True), sa.ForeignKey("ats_pipeline_entries.id", ondelete="SET NULL")),
        # Timestamps
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )

    # Indizes
    op.create_index("ix_client_presentations_match_id", "client_presentations", ["match_id"])
    op.create_index("ix_client_presentations_candidate_id", "client_presentations", ["candidate_id"])
    op.create_index("ix_client_presentations_job_id", "client_presentations", ["job_id"])
    op.create_index("ix_client_presentations_company_id", "client_presentations", ["company_id"])
    op.create_index("ix_client_presentations_contact_id", "client_presentations", ["contact_id"])
    op.create_index("ix_client_presentations_status", "client_presentations", ["status"])
    op.create_index("ix_client_presentations_created_at", "client_presentations", ["created_at"])
    op.create_index("ix_client_presentations_is_fallback", "client_presentations", ["is_fallback"])


def downgrade() -> None:
    op.drop_index("ix_client_presentations_is_fallback", table_name="client_presentations")
    op.drop_index("ix_client_presentations_created_at", table_name="client_presentations")
    op.drop_index("ix_client_presentations_status", table_name="client_presentations")
    op.drop_index("ix_client_presentations_contact_id", table_name="client_presentations")
    op.drop_index("ix_client_presentations_company_id", table_name="client_presentations")
    op.drop_index("ix_client_presentations_job_id", table_name="client_presentations")
    op.drop_index("ix_client_presentations_candidate_id", table_name="client_presentations")
    op.drop_index("ix_client_presentations_match_id", table_name="client_presentations")
    op.drop_table("client_presentations")
