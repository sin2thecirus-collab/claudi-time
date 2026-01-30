"""Job Model - Stellenanzeigen aus CSV-Import."""

import uuid
from datetime import datetime

from geoalchemy2 import Geography
from sqlalchemy import Boolean, DateTime, Float, Index, String, Text, func
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

    # Hotlist-Kategorisierung
    hotlist_category: Mapped[str | None] = mapped_column(String(50))
    hotlist_city: Mapped[str | None] = mapped_column(String(255))
    hotlist_job_title: Mapped[str | None] = mapped_column(String(255))
    categorized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Duplikaterkennung
    content_hash: Mapped[str | None] = mapped_column(String(64), unique=True)

    # Ablauf und Löschung
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    excluded_from_deletion: Mapped[bool] = mapped_column(Boolean, default=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

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
    matches: Mapped[list["Match"]] = relationship(
        "Match",
        back_populates="job",
        cascade="all, delete-orphan",
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
