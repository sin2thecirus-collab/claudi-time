"""Phase 5: Company address consolidation, contact fields, todo contact FK, documents table.

Aenderungen:
- companies: ADD address (Text), befuellt aus street+house_number+postal_code+city, DROP street/house_number/postal_code
- company_contacts: ADD city (String 255), ADD mobile (String 100)
- ats_todos: ADD contact_id (UUID FK -> company_contacts.id)
- CREATE TABLE company_documents

Revision ID: 013
Revises: 012
Create Date: 2026-02-07
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers
revision = "013"
down_revision = "012"
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


def _table_exists(table_name: str) -> bool:
    """Prueft ob eine Tabelle existiert."""
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_name = :table AND table_schema = 'public'"
        ),
        {"table": table_name},
    )
    return result.fetchone() is not None


def upgrade() -> None:
    # === 1. Companies: Address consolidation ===

    # Add address column
    if not _column_exists("companies", "address"):
        op.add_column(
            "companies",
            sa.Column("address", sa.Text(), nullable=True),
        )

        # Populate address from existing fields
        op.execute(
            sa.text("""
                UPDATE companies
                SET address = NULLIF(TRIM(
                    CONCAT_WS(', ',
                        NULLIF(TRIM(CONCAT_WS(' ', street, house_number)), ''),
                        NULLIF(TRIM(CONCAT_WS(' ', postal_code, city)), '')
                    )
                ), '')
            """)
        )

    # Drop old address columns (keep city!)
    if _column_exists("companies", "street"):
        op.drop_column("companies", "street")
    if _column_exists("companies", "house_number"):
        op.drop_column("companies", "house_number")
    if _column_exists("companies", "postal_code"):
        op.drop_column("companies", "postal_code")

    # === 2. CompanyContacts: Add city + mobile ===

    if not _column_exists("company_contacts", "city"):
        op.add_column(
            "company_contacts",
            sa.Column("city", sa.String(255), nullable=True),
        )

    if not _column_exists("company_contacts", "mobile"):
        op.add_column(
            "company_contacts",
            sa.Column("mobile", sa.String(100), nullable=True),
        )

    # === 3. ATSTodos: Add contact_id FK ===

    if not _column_exists("ats_todos", "contact_id"):
        op.add_column(
            "ats_todos",
            sa.Column(
                "contact_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("company_contacts.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )
        op.create_index(
            "ix_ats_todos_contact_id",
            "ats_todos",
            ["contact_id"],
        )

    # === 4. CompanyDocuments: New table ===

    if not _table_exists("company_documents"):
        op.create_table(
            "company_documents",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                       server_default=sa.text("gen_random_uuid()")),
            sa.Column("company_id", postgresql.UUID(as_uuid=True),
                       sa.ForeignKey("companies.id", ondelete="CASCADE"),
                       nullable=False),
            sa.Column("filename", sa.String(500), nullable=False),
            sa.Column("file_path", sa.Text(), nullable=False),
            sa.Column("file_size", sa.Integer(), nullable=True),
            sa.Column("mime_type", sa.String(100), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True),
                       server_default=sa.func.now()),
        )
        op.create_index(
            "ix_company_documents_company_id",
            "company_documents",
            ["company_id"],
        )


def downgrade() -> None:
    # Drop company_documents table
    op.drop_index("ix_company_documents_company_id", table_name="company_documents")
    op.drop_table("company_documents")

    # Remove contact_id from ats_todos
    op.drop_index("ix_ats_todos_contact_id", table_name="ats_todos")
    op.drop_column("ats_todos", "contact_id")

    # Remove mobile and city from company_contacts
    op.drop_column("company_contacts", "mobile")
    op.drop_column("company_contacts", "city")

    # Restore old address columns (data is lost)
    op.add_column("companies", sa.Column("postal_code", sa.String(10), nullable=True))
    op.add_column("companies", sa.Column("house_number", sa.String(20), nullable=True))
    op.add_column("companies", sa.Column("street", sa.String(255), nullable=True))
    op.drop_column("companies", "address")
