"""Add salary, notice_period and erp fields to candidates table.

Pipeline-relevante Felder fuer Kandidaten:
- salary: Gehaltswunsch (String, z.B. "55.000 €")
- notice_period: Kuendigungsfrist (String, z.B. "3 Monate")
- erp: ERP-Kenntnisse (Array, z.B. ["SAP", "DATEV"])

Revision ID: 012
Revises: 011
Create Date: 2026-02-07
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers
revision = "012"
down_revision = "011"
branch_labels = None
depends_on = None


def _column_exists(table_name: str, column_name: str) -> bool:
    """Prueft ob eine Spalte existiert."""
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = :table AND column_name = :col"
        ),
        {"table": table_name, "col": column_name},
    )
    return result.fetchone() is not None


def upgrade() -> None:
    # salary — Gehaltswunsch (Freitext)
    if not _column_exists("candidates", "salary"):
        op.add_column(
            "candidates",
            sa.Column("salary", sa.String(100), nullable=True),
        )

    # notice_period — Kuendigungsfrist (Freitext)
    if not _column_exists("candidates", "notice_period"):
        op.add_column(
            "candidates",
            sa.Column("notice_period", sa.String(100), nullable=True),
        )

    # erp — ERP-Kenntnisse (Array of Strings)
    if not _column_exists("candidates", "erp"):
        op.add_column(
            "candidates",
            sa.Column("erp", postgresql.ARRAY(sa.String()), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("candidates", "erp")
    op.drop_column("candidates", "notice_period")
    op.drop_column("candidates", "salary")
