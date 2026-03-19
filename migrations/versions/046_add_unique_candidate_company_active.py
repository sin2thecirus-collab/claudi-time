"""Add partial unique index on client_presentations (candidate_id, company_id).

Prevents duplicate active presentations of the same candidate to the same company.
Cancelled and no_response presentations are excluded — they may have duplicates.

Revision ID: 046
Revises: 045
Create Date: 2026-03-19
"""

from alembic import op

revision = "046"
down_revision = "045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_presentations_candidate_company_active
        ON client_presentations (candidate_id, company_id)
        WHERE status NOT IN ('cancelled', 'no_response')
          AND candidate_id IS NOT NULL
          AND company_id IS NOT NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_presentations_candidate_company_active")
