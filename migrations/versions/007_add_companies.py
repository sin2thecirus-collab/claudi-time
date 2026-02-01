"""Add companies, company_contacts, company_correspondence tables + jobs.company_id FK.

Erstellt die Unternehmensverwaltung mit Kontakten und Korrespondenz.
Migriert bestehende Jobs zu Company-Records.

Revision ID: 007
Revises: 006
Create Date: 2026-02-01
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers
revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    """Prueft ob eine Tabelle existiert."""
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = :table"
        ),
        {"table": table_name},
    )
    return result.fetchone() is not None


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


def _index_exists(index_name: str) -> bool:
    """Prueft ob ein Index existiert."""
    conn = op.get_bind()
    result = conn.execute(
        sa.text("SELECT indexname FROM pg_indexes WHERE indexname = :idx"),
        {"idx": index_name},
    )
    return result.fetchone() is not None


def upgrade() -> None:
    conn = op.get_bind()

    # --- 1. Enum-Typen erstellen ---
    conn.execute(sa.text(
        "DO $$ BEGIN "
        "CREATE TYPE companystatus AS ENUM ('active', 'blacklist', 'laufende_prozesse'); "
        "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
    ))
    conn.execute(sa.text(
        "DO $$ BEGIN "
        "CREATE TYPE correspondencedirection AS ENUM ('inbound', 'outbound'); "
        "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
    ))

    # --- 2. companies Tabelle ---
    if not _table_exists("companies"):
        op.create_table(
            "companies",
            sa.Column("id", UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text("gen_random_uuid()")),
            sa.Column("name", sa.String(255), nullable=False, unique=True),
            sa.Column("domain", sa.String(255)),
            sa.Column("street", sa.String(255)),
            sa.Column("house_number", sa.String(20)),
            sa.Column("postal_code", sa.String(10)),
            sa.Column("city", sa.String(100)),
            sa.Column("employee_count", sa.String(50)),
            sa.Column("status", sa.Enum("active", "blacklist", "laufende_prozesse",
                                        name="companystatus", create_type=False),
                      server_default="active"),
            sa.Column("notes", sa.Text),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True),
                      server_default=sa.func.now()),
        )
        # PostGIS Koordinaten-Spalte separat hinzufuegen
        conn.execute(sa.text(
            "SELECT AddGeographyColumn('public', 'companies', 'location_coords', "
            "4326, 'POINT', 2)"
        ))
        # Indizes
        op.create_index("ix_companies_name", "companies", ["name"])
        op.create_index("ix_companies_city", "companies", ["city"])
        op.create_index("ix_companies_status", "companies", ["status"])
        op.create_index("ix_companies_created_at", "companies", ["created_at"])

    # --- 3. company_contacts Tabelle ---
    if not _table_exists("company_contacts"):
        op.create_table(
            "company_contacts",
            sa.Column("id", UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text("gen_random_uuid()")),
            sa.Column("company_id", UUID(as_uuid=True),
                      sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
            sa.Column("salutation", sa.String(20)),
            sa.Column("first_name", sa.String(100)),
            sa.Column("last_name", sa.String(100)),
            sa.Column("position", sa.String(255)),
            sa.Column("email", sa.String(500)),
            sa.Column("phone", sa.String(100)),
            sa.Column("notes", sa.Text),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True),
                      server_default=sa.func.now()),
        )
        op.create_index("ix_company_contacts_company_id", "company_contacts", ["company_id"])
        op.create_index("ix_company_contacts_last_name", "company_contacts", ["last_name"])

    # --- 4. company_correspondence Tabelle ---
    if not _table_exists("company_correspondence"):
        op.create_table(
            "company_correspondence",
            sa.Column("id", UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text("gen_random_uuid()")),
            sa.Column("company_id", UUID(as_uuid=True),
                      sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
            sa.Column("contact_id", UUID(as_uuid=True),
                      sa.ForeignKey("company_contacts.id", ondelete="SET NULL")),
            sa.Column("direction", sa.Enum("inbound", "outbound",
                                           name="correspondencedirection", create_type=False),
                      server_default="outbound"),
            sa.Column("subject", sa.String(500), nullable=False),
            sa.Column("body", sa.Text),
            sa.Column("sent_at", sa.DateTime(timezone=True),
                      server_default=sa.func.now()),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      server_default=sa.func.now()),
        )
        op.create_index("ix_company_correspondence_company_id",
                        "company_correspondence", ["company_id"])
        op.create_index("ix_company_correspondence_sent_at",
                        "company_correspondence", ["sent_at"])

    # --- 5. jobs.company_id FK ---
    if not _column_exists("jobs", "company_id"):
        op.add_column("jobs", sa.Column("company_id", UUID(as_uuid=True)))
        op.create_foreign_key(
            "fk_jobs_company_id",
            "jobs", "companies",
            ["company_id"], ["id"],
            ondelete="SET NULL",
        )
        if not _index_exists("ix_jobs_company_id"):
            op.create_index("ix_jobs_company_id", "jobs", ["company_id"])

    # --- 6. Datenmigration: bestehende company_name → Company Records ---
    # Fuer jeden eindeutigen company_name eine Company erstellen
    conn.execute(sa.text("""
        INSERT INTO companies (id, name, city, postal_code, street, created_at, updated_at)
        SELECT
            gen_random_uuid(),
            sub.company_name,
            sub.city,
            sub.postal_code,
            sub.street_address,
            NOW(),
            NOW()
        FROM (
            SELECT DISTINCT ON (TRIM(company_name))
                TRIM(company_name) AS company_name,
                city,
                postal_code,
                street_address
            FROM jobs
            WHERE company_name IS NOT NULL
                AND TRIM(company_name) != ''
                AND deleted_at IS NULL
            ORDER BY TRIM(company_name), created_at DESC
        ) sub
        WHERE NOT EXISTS (
            SELECT 1 FROM companies c
            WHERE LOWER(c.name) = LOWER(sub.company_name)
        )
    """))

    # Jobs mit company_id verknuepfen
    conn.execute(sa.text("""
        UPDATE jobs j
        SET company_id = c.id
        FROM companies c
        WHERE LOWER(TRIM(j.company_name)) = LOWER(c.name)
            AND j.company_id IS NULL
    """))


def downgrade() -> None:
    # FK und Spalte von jobs entfernen
    op.drop_constraint("fk_jobs_company_id", "jobs", type_="foreignkey")
    if _index_exists("ix_jobs_company_id"):
        op.drop_index("ix_jobs_company_id", table_name="jobs")
    op.drop_column("jobs", "company_id")

    # Tabellen loeschen (umgekehrte Reihenfolge wegen FKs)
    op.drop_table("company_correspondence")
    op.drop_table("company_contacts")
    op.drop_table("companies")

    # Enum-Typen loeschen
    conn = op.get_bind()
    conn.execute(sa.text("DROP TYPE IF EXISTS correspondencedirection"))
    conn.execute(sa.text("DROP TYPE IF EXISTS companystatus"))
