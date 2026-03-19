"""Add interview_scheduled and interview_cancelled to activitytype enum.

Fix: Migration 044 wurde ohne ALTER TYPE deployed, Alembic hat sie als
erledigt markiert. Die Enum-Werte muessen separat nachgetragen werden.

Revision ID: 045
Revises: 044
Create Date: 2026-03-19
"""

from alembic import op

revision = "045"
down_revision = "044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # WICHTIG: ALTER TYPE ... ADD VALUE kann NICHT in einer Transaktion laufen
    # Deshalb execution_options mit autocommit
    op.execute("COMMIT")
    op.execute("ALTER TYPE activitytype ADD VALUE IF NOT EXISTS 'interview_scheduled'")
    op.execute("ALTER TYPE activitytype ADD VALUE IF NOT EXISTS 'interview_cancelled'")


def downgrade() -> None:
    # PostgreSQL erlaubt kein DROP VALUE aus einem Enum
    # Die Werte bleiben bestehen, werden aber nicht mehr verwendet
    pass
