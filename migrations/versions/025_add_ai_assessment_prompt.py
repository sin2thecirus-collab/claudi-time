"""Add ai_assessment_prompt to system_settings.

Default prompt for optional AI assessment in V5 Matching.

Revision ID: 025
Revises: 024
Create Date: 2026-02-22
"""

from alembic import op
import sqlalchemy as sa

revision = "025"
down_revision = "024"
branch_labels = None
depends_on = None

_DEFAULT_PROMPT = (
    "Du bist ein extrem erfahrener Personalberater mit 20 Jahre Berufserfahrung "
    "im Bereich Finance und Accounting.\n\n"
    "Bewerte bitte nach deiner Meinung und nach deinem Ermessen ob dieser "
    "Kandidat fuer diese Stelle geeignet ist.\n\n"
    "Achte besonders auf:\n"
    "- Uebereinstimmung der Taetigkeiten\n"
    "- Qualifikations-Level\n"
    "- Branchenerfahrung\n"
    "- Software-Kenntnisse (DATEV, SAP, etc.)\n"
    "- Soft Skills und Entwicklungspotenzial\n\n"
    'Antworte NUR als JSON:\n'
    '{"score": 0-100, "staerken": ["...", "..."], "luecken": ["...", "..."]}'
)


def upgrade() -> None:
    conn = op.get_bind()

    # value-Spalte vergroessern fuer langen Prompt (500 -> 5000)
    op.alter_column(
        "system_settings",
        "value",
        type_=sa.String(5000),
        existing_type=sa.String(500),
        existing_nullable=False,
    )

    conn.execute(
        sa.text(
            "INSERT INTO system_settings (key, value, description) "
            "VALUES (:key, :value, :desc) "
            "ON CONFLICT (key) DO NOTHING"
        ),
        {
            "key": "ai_assessment_prompt",
            "value": _DEFAULT_PROMPT,
            "desc": "KI-Bewertungs-Prompt fuer optionale manuelle Bewertung (V5 Matching)",
        },
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text("DELETE FROM system_settings WHERE key = 'ai_assessment_prompt'")
    )
    op.alter_column(
        "system_settings",
        "value",
        type_=sa.String(500),
        existing_type=sa.String(5000),
        existing_nullable=False,
    )
