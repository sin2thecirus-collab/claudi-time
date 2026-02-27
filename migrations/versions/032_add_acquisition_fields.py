"""Add acquisition fields for Akquise-Automatisierung.

Erweitert jobs (+9), companies (+1), company_contacts (+3).
Erstellt acquisition_calls und acquisition_emails Tabellen.
9 partielle Indizes fuer performante Queries.

Revision ID: 032
Revises: 031
Create Date: 2026-02-27
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = "032"
down_revision = "031"
branch_labels = None
depends_on = None


def upgrade():
    # ── Jobs: +9 Akquise-Felder ──
    op.add_column("jobs", sa.Column("acquisition_source", sa.String(20), nullable=True))
    op.add_column("jobs", sa.Column("position_id", sa.String(50), nullable=True))
    op.add_column("jobs", sa.Column("anzeigen_id", sa.String(50), nullable=True))
    op.add_column("jobs", sa.Column("akquise_status", sa.String(30), nullable=True))
    op.add_column("jobs", sa.Column("akquise_status_changed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("jobs", sa.Column("akquise_priority", sa.Integer(), nullable=True))
    op.add_column("jobs", sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("jobs", sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("jobs", sa.Column("import_batch_id", UUID(as_uuid=True), nullable=True))

    # ── Companies: +1 Akquise-Feld ──
    op.add_column("companies", sa.Column(
        "acquisition_status", sa.String(20), nullable=True, server_default="prospect",
    ))

    # ── CompanyContacts: +3 Akquise-Felder ──
    op.add_column("company_contacts", sa.Column("source", sa.String(20), nullable=True))
    op.add_column("company_contacts", sa.Column("contact_role", sa.String(20), nullable=True))
    op.add_column("company_contacts", sa.Column("phone_normalized", sa.String(20), nullable=True))

    # ── Neue Tabelle: acquisition_calls ──
    op.create_table(
        "acquisition_calls",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("job_id", UUID(as_uuid=True), sa.ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("contact_id", UUID(as_uuid=True), sa.ForeignKey("company_contacts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("company_id", UUID(as_uuid=True), sa.ForeignKey("companies.id", ondelete="SET NULL"), nullable=True),
        sa.Column("call_type", sa.String(20), nullable=False),
        sa.Column("disposition", sa.String(30), nullable=False),
        sa.Column("qualification_data", JSONB, nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("recording_consent", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("follow_up_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("follow_up_note", sa.String(500), nullable=True),
        sa.Column("email_sent", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("email_consent", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # ── Neue Tabelle: acquisition_emails ──
    op.create_table(
        "acquisition_emails",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("job_id", UUID(as_uuid=True), sa.ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("contact_id", UUID(as_uuid=True), sa.ForeignKey("company_contacts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("company_id", UUID(as_uuid=True), sa.ForeignKey("companies.id", ondelete="SET NULL"), nullable=True),
        sa.Column("parent_email_id", UUID(as_uuid=True), sa.ForeignKey("acquisition_emails.id", ondelete="SET NULL"), nullable=True),
        sa.Column("from_email", sa.String(500), nullable=True),
        sa.Column("to_email", sa.String(500), nullable=True),
        sa.Column("subject", sa.String(500), nullable=True),
        sa.Column("body_html", sa.Text(), nullable=True),
        sa.Column("body_plain", sa.Text(), nullable=True),
        sa.Column("candidate_fiction", JSONB, nullable=True),
        sa.Column("email_type", sa.String(20), nullable=True),
        sa.Column("sequence_position", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("status", sa.String(20), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("graph_message_id", sa.String(255), nullable=True),
        sa.Column("unsubscribe_token", sa.String(64), nullable=True, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # ── Indizes ──
    # Jobs: Dedup-Lookup auf anzeigen_id
    op.create_index(
        "idx_jobs_anzeigen_id", "jobs", ["anzeigen_id"],
        postgresql_where=sa.text("anzeigen_id IS NOT NULL"),
    )
    # Jobs: Tab-Queries (Akquise-Seite)
    op.create_index(
        "idx_jobs_akquise_status", "jobs",
        ["acquisition_source", "akquise_status", "akquise_priority"],
    )
    # Jobs: Batch-Rollback
    op.create_index(
        "idx_jobs_batch_id", "jobs", ["import_batch_id"],
        postgresql_where=sa.text("import_batch_id IS NOT NULL"),
    )
    # Contacts: Rueckruf-Lookup per Telefon
    op.create_index(
        "idx_contacts_phone_norm", "company_contacts", ["phone_normalized"],
        postgresql_where=sa.text("phone_normalized IS NOT NULL"),
    )
    # Calls: Call-Historie pro Job (job_id + created_at, PostgreSQL scannt DESC automatisch)
    op.create_index(
        "idx_acq_calls_job", "acquisition_calls", ["job_id", "created_at"],
    )
    # Calls: Wiedervorlagen
    op.create_index(
        "idx_acq_calls_followup", "acquisition_calls", ["follow_up_date"],
        postgresql_where=sa.text("follow_up_date IS NOT NULL"),
    )
    # Emails: E-Mail-Historie pro Lead (job_id + created_at, PostgreSQL scannt DESC automatisch)
    op.create_index(
        "idx_acq_emails_job", "acquisition_emails", ["job_id", "created_at"],
    )
    # Emails: Thread-Verknuepfung
    op.create_index(
        "idx_acq_emails_parent", "acquisition_emails", ["parent_email_id"],
        postgresql_where=sa.text("parent_email_id IS NOT NULL"),
    )
    # Emails: Abmelde-Link Lookup
    op.create_index(
        "idx_acq_emails_unsub", "acquisition_emails", ["unsubscribe_token"],
    )


def downgrade():
    # Indizes entfernen
    op.drop_index("idx_acq_emails_unsub", table_name="acquisition_emails")
    op.drop_index("idx_acq_emails_parent", table_name="acquisition_emails")
    op.drop_index("idx_acq_emails_job", table_name="acquisition_emails")
    op.drop_index("idx_acq_calls_followup", table_name="acquisition_calls")
    op.drop_index("idx_acq_calls_job", table_name="acquisition_calls")
    op.drop_index("idx_contacts_phone_norm", table_name="company_contacts")
    op.drop_index("idx_jobs_batch_id", table_name="jobs")
    op.drop_index("idx_jobs_akquise_status", table_name="jobs")
    op.drop_index("idx_jobs_anzeigen_id", table_name="jobs")

    # Tabellen entfernen
    op.drop_table("acquisition_emails")
    op.drop_table("acquisition_calls")

    # Spalten entfernen
    op.drop_column("company_contacts", "phone_normalized")
    op.drop_column("company_contacts", "contact_role")
    op.drop_column("company_contacts", "source")
    op.drop_column("companies", "acquisition_status")
    op.drop_column("jobs", "import_batch_id")
    op.drop_column("jobs", "last_seen_at")
    op.drop_column("jobs", "first_seen_at")
    op.drop_column("jobs", "akquise_priority")
    op.drop_column("jobs", "akquise_status_changed_at")
    op.drop_column("jobs", "akquise_status")
    op.drop_column("jobs", "anzeigen_id")
    op.drop_column("jobs", "position_id")
    op.drop_column("jobs", "acquisition_source")
