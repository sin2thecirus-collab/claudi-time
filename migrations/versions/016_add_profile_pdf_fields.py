"""Add profile PDF fields to candidates table.

Felder fuer die automatische Profil-PDF-Generierung nach Qualifizierungsgespraech:
- profile_pdf_r2_key: R2 Object Key (z.B. "profiles/a3f2b1c4_Mueller_Thomas_profil.pdf")
- profile_pdf_generated_at: Wann das PDF zuletzt generiert wurde

Revision ID: 016
Revises: 015
Create Date: 2026-02-15
"""

from alembic import op
import sqlalchemy as sa

revision = "016"
down_revision = "015"
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


COLUMNS = [
    ("profile_pdf_r2_key", sa.String(500)),
    ("profile_pdf_generated_at", sa.DateTime(timezone=True)),
]


def upgrade() -> None:
    for col_name, col_type in COLUMNS:
        if not _column_exists("candidates", col_name):
            op.add_column(
                "candidates",
                sa.Column(col_name, col_type, nullable=True),
            )


def downgrade() -> None:
    for col_name, _ in reversed(COLUMNS):
        op.drop_column("candidates", col_name)
