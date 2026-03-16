"""Add email_blocklist table and presentation reply tracking fields

Revision ID: 041
Revises: 040
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = '041'
down_revision = '040'


def upgrade():
    # 1. Neue Tabelle: email_blocklist
    op.create_table(
        'email_blocklist',
        sa.Column('id', UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('domain', sa.String(255), unique=True, nullable=False),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('company_name_before_deletion', sa.String(500), nullable=True),
        sa.Column('contact_email', sa.String(255), nullable=True),
        sa.Column('blocked_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('blocked_by', sa.String(50), server_default='manual'),
    )
    op.create_index('ix_email_blocklist_domain', 'email_blocklist', ['domain'], unique=True)
    op.create_index('ix_email_blocklist_blocked_at', 'email_blocklist', ['blocked_at'])

    # 2. Neue Felder auf client_presentations fuer Reply-Tracking
    op.add_column('client_presentations', sa.Column('reply_status', sa.String(30), nullable=True))
    op.add_column('client_presentations', sa.Column('reply_received_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('client_presentations', sa.Column('reply_body_preview', sa.Text(), nullable=True))


def downgrade():
    # Reply-Tracking Felder entfernen
    op.drop_column('client_presentations', 'reply_body_preview')
    op.drop_column('client_presentations', 'reply_received_at')
    op.drop_column('client_presentations', 'reply_status')

    # Blocklist-Tabelle entfernen
    op.drop_index('ix_email_blocklist_blocked_at', table_name='email_blocklist')
    op.drop_index('ix_email_blocklist_domain', table_name='email_blocklist')
    op.drop_table('email_blocklist')
