"""Job Model - Stellenanzeigen aus CSV-Import."""

import uuid
from datetime import datetime

from geoalchemy2 import Geography
from sqlalchemy import ARRAY, Boolean, Column, DateTime, Float, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB as JobJSONB
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Job(Base):
    """Model für Stellenanzeigen."""

    __tablename__ = "jobs"

    # Primärschlüssel
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # Basis-Informationen
    company_name: Mapped[str] = mapped_column(String(255), nullable=False)
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="SET NULL"),
        nullable=True,
    )
    position: Mapped[str] = mapped_column(String(255), nullable=False)

    # Adresse
    street_address: Mapped[str | None] = mapped_column(String(255))
    postal_code: Mapped[str | None] = mapped_column(String(10))
    city: Mapped[str | None] = mapped_column(String(100))
    work_location_city: Mapped[str | None] = mapped_column(String(100))

    # Koordinaten (PostGIS Geography für Distanzberechnungen)
    location_coords: Mapped[str | None] = mapped_column(
        Geography(geometry_type="POINT", srid=4326),
    )

    # Job-Details
    job_url: Mapped[str | None] = mapped_column(String(500))
    job_text: Mapped[str | None] = mapped_column(Text)
    employment_type: Mapped[str | None] = mapped_column(String(100))
    industry: Mapped[str | None] = mapped_column(String(100))
    company_size: Mapped[str | None] = mapped_column(String(50))
    work_arrangement: Mapped[str | None] = mapped_column(String(20))  # "vor_ort" / "hybrid" / "remote"

    # Hotlist-Kategorisierung
    hotlist_category: Mapped[str | None] = mapped_column(String(50))
    hotlist_city: Mapped[str | None] = mapped_column(String(255))
    hotlist_job_title: Mapped[str | None] = mapped_column(String(255))  # Primary Role
    hotlist_job_titles: Mapped[list[str] | None] = mapped_column(ARRAY(String))  # Alle Rollen
    categorized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Manuelle Jobtitel-Zuweisung (wird NIE automatisch ueberschrieben)
    manual_job_title: Mapped[str | None] = mapped_column(String(255))
    manual_job_title_set_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Embedding (OpenAI text-embedding-3-small, 1536 Dimensionen, als JSONB-Array gespeichert)
    embedding = Column(JobJSONB)

    # ── Matching Engine v2: Strukturiertes Profil ──
    v2_seniority_level: Mapped[int | None] = mapped_column(Integer)  # 1-6 (benötigtes Level)
    v2_required_skills: Mapped[dict | None] = mapped_column(JobJSONB)  # [{skill, importance, category}]
    v2_role_summary: Mapped[str | None] = mapped_column(Text)  # Kern-Tätigkeiten normalisiert
    v2_profile_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    v2_embedding: Mapped[dict | None] = mapped_column(JobJSONB)  # 384-dim Embedding

    # ── Deep Classification (Phase 1) ──
    classification_data = Column(JobJSONB, nullable=True)  # GPT-Klassifizierung: primary_role, roles, sub_level, quality etc.
    quality_score: Mapped[str | None] = mapped_column(String(20))  # high / medium / low — Quality Gate
    job_tasks: Mapped[str | None] = mapped_column(Text)  # Extrahierte Taetigkeiten aus job_text (fuer Stufe-0 LLM-Vorfilter)

    # ── Akquise-Felder (Migration 032) ──
    acquisition_source: Mapped[str | None] = mapped_column(String(20))  # "advertsdata" oder NULL
    position_id: Mapped[str | None] = mapped_column(String(50))  # Externe Position-ID
    anzeigen_id: Mapped[str | None] = mapped_column(String(50))  # Externe Anzeigen-ID (Dedup)
    akquise_status: Mapped[str | None] = mapped_column(String(30))  # neu/angerufen/kontaktiert/...
    akquise_status_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    akquise_priority: Mapped[int | None] = mapped_column(Integer)  # 0 (hoechste) bis 10
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    import_batch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    # Qualifizierung (aus Webex-Transkripten extrahiert)
    qualification_answers: Mapped[dict | None] = mapped_column(JobJSONB)
    qualification_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Duplikaterkennung
    content_hash: Mapped[str | None] = mapped_column(String(64), unique=True)

    # Ablauf und Löschung
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    excluded_from_deletion: Mapped[bool] = mapped_column(Boolean, default=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Import-Tracking
    imported_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    last_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    # Relationships
    company: Mapped["Company | None"] = relationship(
        "Company", back_populates="jobs",
    )
    matches: Mapped[list["Match"]] = relationship(
        "Match",
        back_populates="job",
        cascade="all, delete-orphan",
    )
    acquisition_calls: Mapped[list["AcquisitionCall"]] = relationship(
        "AcquisitionCall", back_populates="job",
    )
    acquisition_emails: Mapped[list["AcquisitionEmail"]] = relationship(
        "AcquisitionEmail", back_populates="job",
    )

    # Indizes
    __table_args__ = (
        Index("ix_jobs_city", "city"),
        Index("ix_jobs_work_location_city", "work_location_city"),
        Index("ix_jobs_company_name", "company_name"),
        Index("ix_jobs_position", "position"),
        Index("ix_jobs_industry", "industry"),
        Index("ix_jobs_created_at", "created_at"),
        Index("ix_jobs_expires_at", "expires_at"),
        Index("ix_jobs_deleted_at", "deleted_at"),
        Index("ix_jobs_content_hash", "content_hash"),
        Index("ix_jobs_hotlist_category", "hotlist_category"),
        Index("ix_jobs_company_id", "company_id"),
        Index("ix_jobs_imported_at", "imported_at"),
        Index("ix_jobs_last_updated_at", "last_updated_at"),
        Index("ix_jobs_quality_score", "quality_score"),
        # Akquise-Indizes (Migration 032)
        Index("idx_jobs_anzeigen_id", "anzeigen_id", postgresql_where=text("anzeigen_id IS NOT NULL")),
        Index("idx_jobs_akquise_status", "acquisition_source", "akquise_status", "akquise_priority"),
        Index("idx_jobs_batch_id", "import_batch_id", postgresql_where=text("import_batch_id IS NOT NULL")),
    )

    @property
    def is_deleted(self) -> bool:
        """Prüft, ob der Job soft-deleted ist."""
        return self.deleted_at is not None

    @property
    def is_expired(self) -> bool:
        """Prüft, ob der Job abgelaufen ist."""
        if self.expires_at is None:
            return False
        return self.expires_at < datetime.now(self.expires_at.tzinfo)

    @property
    def display_city(self) -> str:
        """Gibt die anzuzeigende Stadt zurück (work_location_city oder city)."""
        return self.work_location_city or self.city or "Unbekannt"
