"""Add email_body_html to client_presentations

Revision ID: 040
Revises: 039
"""
from alembic import op
import sqlalchemy as sa

revision = '040'
down_revision = '039'

def upgrade():
    op.add_column('client_presentations', sa.Column('email_body_html', sa.Text(), nullable=True))

def downgrade():
    op.drop_column('client_presentations', 'email_body_html')
