"""Add conversation_id and indexes for email reply tracking.

Enables reliable reply detection for the 'Nicht erreicht' email sequence:
- conversation_id links outbound emails to their Outlook conversation thread
- Indexes speed up reply matching (conversation_id, message_id, direction, composite)

Revision ID: 030
Revises: 029
Create Date: 2026-02-26
"""

from alembic import op
import sqlalchemy as sa

revision = "030"
down_revision = "029"
branch_labels = None
depends_on = None


def upgrade():
    # Neue Spalte: conversation_id fuer Outlook-Threading
    op.add_column(
        "candidate_emails",
        sa.Column("conversation_id", sa.String(500), nullable=True),
    )

    # Indexes fuer schnelles Reply-Matching
    op.create_index(
        "ix_candidate_emails_message_id",
        "candidate_emails",
        ["message_id"],
    )
    op.create_index(
        "ix_candidate_emails_conversation_id",
        "candidate_emails",
        ["conversation_id"],
    )
    op.create_index(
        "ix_candidate_emails_direction",
        "candidate_emails",
        ["direction"],
    )
    op.create_index(
        "ix_candidate_emails_sequence_lookup",
        "candidate_emails",
        ["candidate_id", "direction", "sequence_type"],
    )


def downgrade():
    op.drop_index("ix_candidate_emails_sequence_lookup", table_name="candidate_emails")
    op.drop_index("ix_candidate_emails_direction", table_name="candidate_emails")
    op.drop_index("ix_candidate_emails_conversation_id", table_name="candidate_emails")
    op.drop_index("ix_candidate_emails_message_id", table_name="candidate_emails")
    op.drop_column("candidate_emails", "conversation_id")
