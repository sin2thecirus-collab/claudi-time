"""Recalculate content_hash to include job_url for better duplicate detection.

Bisheriger Hash: SHA-256(unternehmen|position|stadt|plz)
Neuer Hash:      SHA-256(unternehmen|position|stadt|plz|url)

Problem: Gleiche Firma + gleiche Position + gleiche Stadt wurde als Duplikat
erkannt, obwohl es verschiedene Stellenanzeigen (mit verschiedenen URLs) waren.

Diese Migration berechnet ALLE bestehenden content_hashes neu, damit sie mit
dem neuen 5-Feld-Algorithmus uebereinstimmen.

Revision ID: 014
Revises: 013
Create Date: 2026-02-11
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "014"
down_revision = "013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Recalculate all content_hashes to include job_url."""
    conn = op.get_bind()

    # pgcrypto Extension aktivieren (fuer digest/SHA-256)
    conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pgcrypto;"))

    # Temporaer den UNIQUE-Constraint entfernen, damit es keine Konflikte gibt
    # waehrend der Neuberechnung
    try:
        op.drop_constraint("jobs_content_hash_key", "jobs", type_="unique")
    except Exception:
        # Constraint-Name koennte anders sein
        try:
            op.drop_index("ix_jobs_content_hash", table_name="jobs")
        except Exception:
            pass

    # Alle Hashes neu berechnen mit SHA-256(unternehmen|position|stadt|plz|url)
    # PostgreSQL: encode(digest(text, 'sha256'), 'hex') erzeugt den gleichen
    # 64-Zeichen Hex-String wie Python hashlib.sha256().hexdigest()
    conn.execute(
        sa.text("""
            UPDATE jobs
            SET content_hash = encode(
                digest(
                    LOWER(COALESCE(TRIM(company_name), ''))
                    || '|' || LOWER(COALESCE(TRIM(position), ''))
                    || '|' || LOWER(COALESCE(TRIM(COALESCE(city, work_location_city)), ''))
                    || '|' || COALESCE(TRIM(postal_code), '')
                    || '|' || LOWER(COALESCE(TRIM(job_url), '')),
                    'sha256'
                ),
                'hex'
            )
        """)
    )

    # UNIQUE-Constraint wieder hinzufuegen
    op.create_unique_constraint("jobs_content_hash_key", "jobs", ["content_hash"])


def downgrade() -> None:
    """Revert to old 4-field content_hash (without URL)."""
    conn = op.get_bind()

    # pgcrypto sicherstellen
    conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pgcrypto;"))

    try:
        op.drop_constraint("jobs_content_hash_key", "jobs", type_="unique")
    except Exception:
        pass

    # Alte Hashes berechnen: SHA-256(unternehmen|position|stadt|plz)
    conn.execute(
        sa.text("""
            UPDATE jobs
            SET content_hash = encode(
                digest(
                    LOWER(COALESCE(TRIM(company_name), ''))
                    || '|' || LOWER(COALESCE(TRIM(position), ''))
                    || '|' || LOWER(COALESCE(TRIM(COALESCE(city, work_location_city)), ''))
                    || '|' || COALESCE(TRIM(postal_code), ''),
                    'sha256'
                ),
                'hex'
            )
        """)
    )

    op.create_unique_constraint("jobs_content_hash_key", "jobs", ["content_hash"])
