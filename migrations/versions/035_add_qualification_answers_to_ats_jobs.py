"""Add qualification_answers JSONB to ats_jobs for Akquise data transfer.

Revision ID: 035
Revises: 034
Create Date: 2026-03-02
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "035"
down_revision = "034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ats_jobs", sa.Column("qualification_answers", JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("ats_jobs", "qualification_answers")
