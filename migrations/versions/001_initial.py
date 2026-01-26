"""Initiale Datenbank-Struktur.

Revision ID: 001_initial
Revises:
Create Date: 2026-01-26

"""

from typing import Sequence, Union

import geoalchemy2
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # PostGIS Extension aktivieren
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis;")

    # Jobs-Tabelle
    op.create_table(
        "jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("company_name", sa.String(255), nullable=False),
        sa.Column("position", sa.String(255), nullable=False),
        sa.Column("street_address", sa.String(255)),
        sa.Column("postal_code", sa.String(10)),
        sa.Column("city", sa.String(100)),
        sa.Column("work_location_city", sa.String(100)),
        sa.Column(
            "location_coords",
            geoalchemy2.Geography(geometry_type="POINT", srid=4326),
        ),
        sa.Column("job_url", sa.String(500)),
        sa.Column("job_text", sa.Text()),
        sa.Column("employment_type", sa.String(100)),
        sa.Column("industry", sa.String(100)),
        sa.Column("company_size", sa.String(50)),
        sa.Column("content_hash", sa.String(64), unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("excluded_from_deletion", sa.Boolean(), default=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
    )
    op.create_index("ix_jobs_city", "jobs", ["city"])
    op.create_index("ix_jobs_work_location_city", "jobs", ["work_location_city"])
    op.create_index("ix_jobs_company_name", "jobs", ["company_name"])
    op.create_index("ix_jobs_position", "jobs", ["position"])
    op.create_index("ix_jobs_industry", "jobs", ["industry"])
    op.create_index("ix_jobs_created_at", "jobs", ["created_at"])
    op.create_index("ix_jobs_expires_at", "jobs", ["expires_at"])
    op.create_index("ix_jobs_deleted_at", "jobs", ["deleted_at"])
    op.create_index("ix_jobs_content_hash", "jobs", ["content_hash"])

    # Candidates-Tabelle
    op.create_table(
        "candidates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("crm_id", sa.String(100), unique=True),
        sa.Column("first_name", sa.String(100)),
        sa.Column("last_name", sa.String(100)),
        sa.Column("email", sa.String(255)),
        sa.Column("phone", sa.String(50)),
        sa.Column("birth_date", sa.Date()),
        sa.Column("current_position", sa.String(255)),
        sa.Column("current_company", sa.String(255)),
        sa.Column("skills", postgresql.ARRAY(sa.String())),
        sa.Column("work_history", postgresql.JSONB()),
        sa.Column("education", postgresql.JSONB()),
        sa.Column("street_address", sa.String(255)),
        sa.Column("postal_code", sa.String(10)),
        sa.Column("city", sa.String(100)),
        sa.Column(
            "address_coords",
            geoalchemy2.Geography(geometry_type="POINT", srid=4326),
        ),
        sa.Column("cv_text", sa.Text()),
        sa.Column("cv_url", sa.String(500)),
        sa.Column("cv_parsed_at", sa.DateTime(timezone=True)),
        sa.Column("hidden", sa.Boolean(), default=False),
        sa.Column("crm_synced_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
    )
    op.create_index("ix_candidates_crm_id", "candidates", ["crm_id"])
    op.create_index("ix_candidates_city", "candidates", ["city"])
    op.create_index("ix_candidates_hidden", "candidates", ["hidden"])
    op.create_index("ix_candidates_created_at", "candidates", ["created_at"])
    op.create_index("ix_candidates_current_position", "candidates", ["current_position"])
    op.create_index(
        "ix_candidates_skills",
        "candidates",
        ["skills"],
        postgresql_using="gin",
    )

    # Matches-Tabelle
    op.create_table(
        "matches",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "candidate_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("candidates.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("distance_km", sa.Float()),
        sa.Column("keyword_score", sa.Float()),
        sa.Column("matched_keywords", postgresql.ARRAY(sa.String())),
        sa.Column("ai_score", sa.Float()),
        sa.Column("ai_explanation", sa.Text()),
        sa.Column("ai_strengths", postgresql.ARRAY(sa.String())),
        sa.Column("ai_weaknesses", postgresql.ARRAY(sa.String())),
        sa.Column("ai_checked_at", sa.DateTime(timezone=True)),
        sa.Column(
            "status",
            sa.Enum(
                "new",
                "ai_checked",
                "presented",
                "rejected",
                "placed",
                name="matchstatus",
            ),
            default="new",
        ),
        sa.Column("placed_at", sa.DateTime(timezone=True)),
        sa.Column("placed_notes", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
        sa.UniqueConstraint("job_id", "candidate_id", name="uq_match_job_candidate"),
    )
    op.create_index("ix_matches_job_id", "matches", ["job_id"])
    op.create_index("ix_matches_candidate_id", "matches", ["candidate_id"])
    op.create_index("ix_matches_status", "matches", ["status"])
    op.create_index("ix_matches_ai_score", "matches", ["ai_score"])
    op.create_index("ix_matches_distance_km", "matches", ["distance_km"])
    op.create_index("ix_matches_created_at", "matches", ["created_at"])

    # Priority Cities
    op.create_table(
        "priority_cities",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("city_name", sa.String(100), unique=True, nullable=False),
        sa.Column("priority_order", sa.Integer(), nullable=False, default=0),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
    )

    # Filter Presets
    op.create_table(
        "filter_presets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(100), unique=True, nullable=False),
        sa.Column("filter_config", postgresql.JSONB(), nullable=False),
        sa.Column("is_default", sa.Boolean(), default=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
    )

    # Daily Statistics
    op.create_table(
        "daily_statistics",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("date", sa.Date(), unique=True, nullable=False),
        sa.Column("jobs_total", sa.Integer(), default=0),
        sa.Column("jobs_active", sa.Integer(), default=0),
        sa.Column("candidates_total", sa.Integer(), default=0),
        sa.Column("candidates_active", sa.Integer(), default=0),
        sa.Column("matches_total", sa.Integer(), default=0),
        sa.Column("ai_checks_count", sa.Integer(), default=0),
        sa.Column("ai_checks_cost_usd", sa.Float(), default=0.0),
        sa.Column("matches_presented", sa.Integer(), default=0),
        sa.Column("matches_placed", sa.Integer(), default=0),
        sa.Column("avg_ai_score", sa.Float()),
        sa.Column("avg_distance_km", sa.Float()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
    )
    op.create_index("ix_daily_statistics_date", "daily_statistics", ["date"])

    # Filter Usage
    op.create_table(
        "filter_usage",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("filter_type", sa.String(50), nullable=False),
        sa.Column("filter_value", sa.String(255), nullable=False),
        sa.Column(
            "used_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
    )
    op.create_index("ix_filter_usage_filter_type", "filter_usage", ["filter_type"])
    op.create_index("ix_filter_usage_used_at", "filter_usage", ["used_at"])

    # Alerts
    op.create_table(
        "alerts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "alert_type",
            sa.Enum(
                "excellent_match",
                "expiring_job",
                "sync_error",
                "import_complete",
                "system",
                name="alerttype",
            ),
            nullable=False,
        ),
        sa.Column(
            "priority",
            sa.Enum("low", "medium", "high", name="alertpriority"),
            default="medium",
        ),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("jobs.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "candidate_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("candidates.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "match_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("matches.id", ondelete="SET NULL"),
        ),
        sa.Column("is_read", sa.Boolean(), default=False),
        sa.Column("is_dismissed", sa.Boolean(), default=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
    )
    op.create_index("ix_alerts_alert_type", "alerts", ["alert_type"])
    op.create_index("ix_alerts_priority", "alerts", ["priority"])
    op.create_index("ix_alerts_is_read", "alerts", ["is_read"])
    op.create_index("ix_alerts_is_dismissed", "alerts", ["is_dismissed"])
    op.create_index("ix_alerts_created_at", "alerts", ["created_at"])

    # Import Jobs
    op.create_table(
        "import_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("total_rows", sa.Integer(), default=0),
        sa.Column("processed_rows", sa.Integer(), default=0),
        sa.Column("successful_rows", sa.Integer(), default=0),
        sa.Column("failed_rows", sa.Integer(), default=0),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "processing",
                "completed",
                "failed",
                "cancelled",
                name="importstatus",
            ),
            default="pending",
        ),
        sa.Column("error_message", sa.Text()),
        sa.Column("errors_detail", postgresql.JSONB()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
    )
    op.create_index("ix_import_jobs_status", "import_jobs", ["status"])
    op.create_index("ix_import_jobs_created_at", "import_jobs", ["created_at"])

    # Job Runs
    op.create_table(
        "job_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "job_type",
            sa.Enum(
                "geocoding",
                "crm_sync",
                "matching",
                "cleanup",
                "cv_parsing",
                name="jobtype",
            ),
            nullable=False,
        ),
        sa.Column(
            "source",
            sa.Enum("manual", "cron", "system", name="jobsource"),
            default="manual",
        ),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "running",
                "completed",
                "failed",
                "cancelled",
                name="jobrunstatus",
            ),
            default="pending",
        ),
        sa.Column("items_total", sa.Integer(), default=0),
        sa.Column("items_processed", sa.Integer(), default=0),
        sa.Column("items_successful", sa.Integer(), default=0),
        sa.Column("items_failed", sa.Integer(), default=0),
        sa.Column("error_message", sa.Text()),
        sa.Column("errors_detail", postgresql.JSONB()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
    )
    op.create_index("ix_job_runs_job_type", "job_runs", ["job_type"])
    op.create_index("ix_job_runs_status", "job_runs", ["status"])
    op.create_index("ix_job_runs_source", "job_runs", ["source"])
    op.create_index("ix_job_runs_created_at", "job_runs", ["created_at"])

    # Initiale Prio-Städte einfügen (Hamburg und München)
    op.execute(
        """
        INSERT INTO priority_cities (id, city_name, priority_order)
        VALUES
            (gen_random_uuid(), 'Hamburg', 1),
            (gen_random_uuid(), 'München', 2)
        """
    )


def downgrade() -> None:
    op.drop_table("job_runs")
    op.drop_table("import_jobs")
    op.drop_table("alerts")
    op.drop_table("filter_usage")
    op.drop_table("daily_statistics")
    op.drop_table("filter_presets")
    op.drop_table("priority_cities")
    op.drop_table("matches")
    op.drop_table("candidates")
    op.drop_table("jobs")

    # Enums löschen
    op.execute("DROP TYPE IF EXISTS matchstatus")
    op.execute("DROP TYPE IF EXISTS alerttype")
    op.execute("DROP TYPE IF EXISTS alertpriority")
    op.execute("DROP TYPE IF EXISTS importstatus")
    op.execute("DROP TYPE IF EXISTS jobtype")
    op.execute("DROP TYPE IF EXISTS jobsource")
    op.execute("DROP TYPE IF EXISTS jobrunstatus")

    # PostGIS Extension entfernen (optional)
    # op.execute("DROP EXTENSION IF EXISTS postgis;")
