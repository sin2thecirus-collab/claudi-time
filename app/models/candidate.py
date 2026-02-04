"""Candidate Model - Kandidaten aus CRM-Sync."""

import uuid
from datetime import date, datetime

from geoalchemy2 import Geography
from sqlalchemy import ARRAY, Boolean, Column, Date, DateTime, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Candidate(Base):
    """Model für Kandidaten aus dem CRM."""

    __tablename__ = "candidates"

    # Primärschlüssel
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # CRM-Referenz
    crm_id: Mapped[str | None] = mapped_column(String(255), unique=True)

    # Persönliche Daten
    first_name: Mapped[str | None] = mapped_column(String(100))
    last_name: Mapped[str | None] = mapped_column(String(100))
    email: Mapped[str | None] = mapped_column(String(500))
    phone: Mapped[str | None] = mapped_column(String(100))
    birth_date: Mapped[date | None] = mapped_column(Date)

    # Berufliche Daten
    current_position: Mapped[str | None] = mapped_column(Text)
    current_company: Mapped[str | None] = mapped_column(Text)
    skills: Mapped[list[str] | None] = mapped_column(ARRAY(String))

    # Berufserfahrung und Ausbildung (aus CV-Parsing)
    work_history: Mapped[dict | None] = mapped_column(JSONB)
    education: Mapped[dict | None] = mapped_column(JSONB)

    # Weiterbildungen (aus CV-Parsing, separat von education)
    further_education: Mapped[dict | None] = mapped_column(JSONB)

    # Sprachen und IT-Kenntnisse (aus CV-Parsing, separat von skills)
    languages: Mapped[dict | None] = mapped_column(JSONB)
    it_skills: Mapped[list[str] | None] = mapped_column(ARRAY(String))

    # Adresse
    street_address: Mapped[str | None] = mapped_column(Text)
    postal_code: Mapped[str | None] = mapped_column(String(50))
    city: Mapped[str | None] = mapped_column(String(255))

    # Koordinaten (PostGIS Geography)
    address_coords: Mapped[str | None] = mapped_column(
        Geography(geometry_type="POINT", srid=4326),
    )

    # CV-Daten
    cv_text: Mapped[str | None] = mapped_column(Text)
    cv_url: Mapped[str | None] = mapped_column(Text)
    cv_stored_path: Mapped[str | None] = mapped_column(Text)  # R2 Object Key (z.B. 'cvs/{uuid}.pdf')
    cv_parsed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cv_parse_failed: Mapped[bool] = mapped_column(Boolean, default=False)  # True wenn PDF nicht lesbar (z.B. Bild-PDF)

    # Manuelle Overrides (Felder die manuell bearbeitet wurden und nicht per Sync/Parsing ueberschrieben werden)
    manual_overrides: Mapped[dict | None] = mapped_column(JSONB)

    # Manuelle Jobtitel-Zuweisung (wird NIE automatisch ueberschrieben)
    manual_job_titles: Mapped[list[str] | None] = mapped_column(ARRAY(String))  # z.B. ["Finanzbuchhalter/in", "Kreditorenbuchhalter/in"]
    manual_job_titles_set_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Hotlist-Kategorisierung
    hotlist_category: Mapped[str | None] = mapped_column(String(50))
    hotlist_city: Mapped[str | None] = mapped_column(String(255))
    hotlist_job_title: Mapped[str | None] = mapped_column(String(255))  # Primary Role
    hotlist_job_titles: Mapped[list[str] | None] = mapped_column(ARRAY(String))  # Alle Rollen
    categorized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Finance-Klassifizierung (OpenAI-Trainingsdaten)
    classification_data: Mapped[dict | None] = mapped_column(JSONB)  # {"source", "roles", "reasoning", "classified_at"}

    # Embedding (pgvector - text-embedding-3-small, 1536 Dimensionen)
    # Embedding (OpenAI text-embedding-3-small, 1536 Dimensionen, als JSONB-Array gespeichert)
    embedding = Column(JSONB)

    # Status
    hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Sync-Tracking
    crm_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

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
        back_populates="candidate",
        cascade="all, delete-orphan",
    )

    # Indizes
    __table_args__ = (
        Index("ix_candidates_crm_id", "crm_id"),
        Index("ix_candidates_city", "city"),
        Index("ix_candidates_hidden", "hidden"),
        Index("ix_candidates_deleted_at", "deleted_at"),
        Index("ix_candidates_created_at", "created_at"),
        Index("ix_candidates_current_position", "current_position"),
        Index("ix_candidates_skills", "skills", postgresql_using="gin"),
        Index("ix_candidates_hotlist_category", "hotlist_category"),
    )

    @property
    def full_name(self) -> str:
        """Generiert den vollständigen Namen."""
        parts = [self.first_name, self.last_name]
        return " ".join(p for p in parts if p) or "Unbekannt"

    @property
    def age(self) -> int | None:
        """Berechnet das Alter aus dem Geburtsdatum."""
        if not self.birth_date:
            return None
        today = date.today()
        age = today.year - self.birth_date.year
        if (today.month, today.day) < (self.birth_date.month, self.birth_date.day):
            age -= 1
        return age

    @property
    def is_active(self) -> bool:
        """Prüft, ob der Kandidat als aktiv gilt (≤30 Tage alt)."""
        if not self.created_at:
            return False
        days_since_creation = (datetime.now(self.created_at.tzinfo) - self.created_at).days
        return days_since_creation <= 30 and not self.hidden
