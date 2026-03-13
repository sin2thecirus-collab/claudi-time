"""Add direct presentation fields + presentation_batches table.

Erweitert client_presentations um Felder fuer direkte Kandidaten-Vorstellung
(ohne Match) und fuegt die Batch-Tracking-Tabelle fuer CSV-Bulk-Upload hinzu.

Neue Felder auf client_presentations:
- source: match_center / candidate_direct / csv_bulk
- job_posting_text: Roh-Stellentext
- extracted_job_data: GPT-extrahierte strukturierte Daten
- skills_comparison: Qualitativer Vergleich
- batch_id: FK zu presentation_batches
- reply_to_email: Reply-To Adresse
- response_type: bounce / auto_reply / genuine_reply

Neue Tabelle: presentation_batches (CSV-Bulk-Tracking mit Row-Level-Recovery)

Revision ID: 039
Revises: 038
Create Date: 2026-03-13
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = "039"
down_revision = "038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Neue Felder auf client_presentations ──
    op.add_column(
        "client_presentations",
        sa.Column("source", sa.String(20), server_default="match_center", nullable=False),
    )
    op.add_column(
        "client_presentations",
        sa.Column("job_posting_text", sa.Text(), nullable=True),
    )
    op.add_column(
        "client_presentations",
        sa.Column("extracted_job_data", JSONB(), nullable=True),
    )
    op.add_column(
        "client_presentations",
        sa.Column("skills_comparison", JSONB(), nullable=True),
    )
    op.add_column(
        "client_presentations",
        sa.Column("batch_id", UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "client_presentations",
        sa.Column("reply_to_email", sa.String(500), server_default="hamdard@sincirus.com", nullable=True),
    )
    op.add_column(
        "client_presentations",
        sa.Column("response_type", sa.String(20), nullable=True),
    )

    # ── Neue Tabelle: presentation_batches ──
    op.create_table(
        "presentation_batches",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("candidate_id", UUID(as_uuid=True), sa.ForeignKey("candidates.id", ondelete="SET NULL"), nullable=True),
        sa.Column("csv_filename", sa.String(255), nullable=True),
        sa.Column("total_rows", sa.Integer(), server_default="0", nullable=False),
        sa.Column("processed", sa.Integer(), server_default="0", nullable=False),
        sa.Column("skipped", sa.Integer(), server_default="0", nullable=False),
        sa.Column("errors", sa.Integer(), server_default="0", nullable=False),
        sa.Column("status", sa.String(20), server_default="processing", nullable=False),
        sa.Column("mailbox_distribution", JSONB(), nullable=True),
        sa.Column("error_details", JSONB(), nullable=True),
        sa.Column("processed_rows", JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # ── FK: client_presentations.batch_id → presentation_batches.id ──
    op.create_foreign_key(
        "fk_presentations_batch_id",
        "client_presentations",
        "presentation_batches",
        ["batch_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # ── Indizes ──
    op.create_index(
        "idx_presentations_company_role",
        "client_presentations",
        ["company_id", "source"],
        postgresql_where=sa.text("status != 'cancelled'"),
    )
    op.create_index(
        "idx_presentations_batch",
        "client_presentations",
        ["batch_id"],
        postgresql_where=sa.text("batch_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("idx_presentations_batch", table_name="client_presentations")
    op.drop_index("idx_presentations_company_role", table_name="client_presentations")
    op.drop_constraint("fk_presentations_batch_id", "client_presentations", type_="foreignkey")
    op.drop_table("presentation_batches")
    op.drop_column("client_presentations", "response_type")
    op.drop_column("client_presentations", "reply_to_email")
    op.drop_column("client_presentations", "batch_id")
    op.drop_column("client_presentations", "skills_comparison")
    op.drop_column("client_presentations", "extracted_job_data")
    op.drop_column("client_presentations", "job_posting_text")
    op.drop_column("client_presentations", "source")
