"""Add system_settings table for configurable parameters.

Stores key-value pairs like drive_time_score_threshold.

Revision ID: 022
Revises: 021
Create Date: 2026-02-16
"""

from alembic import op
import sqlalchemy as sa

revision = "022"
down_revision = "021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Tabelle nur erstellen wenn sie nicht existiert
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT tablename FROM pg_tables "
            "WHERE schemaname = 'public' AND tablename = 'system_settings'"
        )
    )
    if result.fetchone() is None:
        op.create_table(
            "system_settings",
            sa.Column("key", sa.String(100), primary_key=True),
            sa.Column("value", sa.String(500), nullable=False),
            sa.Column("description", sa.String(500), nullable=True),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
            ),
        )

    # Default-Wert: drive_time_score_threshold = 70
    conn.execute(
        sa.text(
            "INSERT INTO system_settings (key, value, description) "
            "VALUES (:key, :value, :desc) "
            "ON CONFLICT (key) DO NOTHING"
        ),
        {
            "key": "drive_time_score_threshold",
            "value": "70",
            "desc": "Minimum Match-Score fuer Google Maps Fahrzeit-Berechnung (0-100)",
        },
    )


def downgrade() -> None:
    op.drop_table("system_settings")
