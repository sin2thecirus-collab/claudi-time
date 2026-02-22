"""Add job_tasks field to jobs table.

Stores extracted tasks/activities from job_text for efficient LLM matching.
Populated during job classification (GPT extracts tasks from job_text).

Revision ID: 024
Revises: 023
Create Date: 2026-02-22
"""

from alembic import op
import sqlalchemy as sa

revision = "024"
down_revision = "023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("job_tasks", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "job_tasks")
