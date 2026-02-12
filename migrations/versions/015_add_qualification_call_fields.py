"""Add qualification call fields to candidates table.

Qualifizierungsgespräch-Felder fuer KI-Transkription-Pipeline:
- desired_positions: Gewünschte Positionen (Freitext)
- key_activities: Tätigkeiten (Freitext)
- home_office_days: Home-Office Tage (z.B. "2 bis 3 Tage")
- commute_max: Pendelbereitschaft (z.B. "30 min")
- commute_transport: Verkehrsmittel (Auto/ÖPNV/Beides)
- erp_main: ERP-Steckenpferd (z.B. "DATEV")
- employment_type: Vollzeit/Teilzeit
- part_time_hours: Teilzeit-Stunden
- preferred_industries: Bevorzugte Branchen (Freitext)
- avoided_industries: Branchen vermeiden (Freitext)
- open_office_ok: Großraumbüro OK (ja/nein/egal)
- whatsapp_ok: WhatsApp-Kontakt erlaubt
- other_recruiters: Andere Recruiter aktiv
- exclusivity_agreed: Exklusivität vereinbart
- applied_at_companies_text: Wo bereits beworben (Freitext aus Transkription)
- call_transcript: Volle Transkription
- call_summary: KI-Zusammenfassung
- call_date: Datum des Gesprächs
- call_type: Gesprächstyp (qualifizierung/kurz/kunde/sonstig)

Revision ID: 015
Revises: 014
Create Date: 2026-02-12
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "015"
down_revision = "014"
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
    ("desired_positions", sa.Text()),
    ("key_activities", sa.Text()),
    ("home_office_days", sa.String(50)),
    ("commute_max", sa.String(100)),
    ("commute_transport", sa.String(50)),
    ("erp_main", sa.String(100)),
    ("employment_type", sa.String(50)),
    ("part_time_hours", sa.String(50)),
    ("preferred_industries", sa.Text()),
    ("avoided_industries", sa.Text()),
    ("open_office_ok", sa.String(20)),
    ("whatsapp_ok", sa.Boolean()),
    ("other_recruiters", sa.Text()),
    ("exclusivity_agreed", sa.Boolean()),
    ("applied_at_companies_text", sa.Text()),
    ("call_transcript", sa.Text()),
    ("call_summary", sa.Text()),
    ("call_date", sa.DateTime(timezone=True)),
    ("call_type", sa.String(50)),
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
