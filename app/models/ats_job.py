"""ATSJob Model - Qualifizierte Stellen aus Kundengespraechen."""

import enum
import uuid
from datetime import datetime

from geoalchemy2 import Geography
from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ATSJobPriority(str, enum.Enum):
    """Prioritaet einer ATS-Stelle."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


class ATSJobStatus(str, enum.Enum):
    """Status einer ATS-Stelle."""

    OPEN = "open"
    PAUSED = "paused"
    FILLED = "filled"
    CANCELLED = "cancelled"


class ATSJob(Base):
    """Model fuer qualifizierte Stellen aus Kundengespraechen."""

    __tablename__ = "ats_jobs"

    # Primaerschluessel
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # Foreign Keys
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="SET NULL"),
        nullable=True,
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("company_contacts.id", ondelete="SET NULL"),
        nullable=True,
    )
    # HINWEIS: source_job_id wird erst nach Migration 011 verfuegbar sein
    # Bis dahin wird Cascading Delete im Code via try/except gehandhabt

    # Stellen-Informationen
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    requirements: Mapped[str | None] = mapped_column(Text)

    # Standort
    location_city: Mapped[str | None] = mapped_column(String(100))
    location_coords: Mapped[str | None] = mapped_column(
        Geography(geometry_type="POINT", srid=4326),
    )

    # Gehalt
    salary_min: Mapped[int | None] = mapped_column(Integer)
    salary_max: Mapped[int | None] = mapped_column(Integer)

    # Details
    employment_type: Mapped[str | None] = mapped_column(String(50))
    priority: Mapped[ATSJobPriority] = mapped_column(
        Enum(ATSJobPriority, values_callable=lambda x: [e.value for e in x]),
        default=ATSJobPriority.MEDIUM,
    )
    status: Mapped[ATSJobStatus] = mapped_column(
        Enum(ATSJobStatus, values_callable=lambda x: [e.value for e in x]),
        default=ATSJobStatus.OPEN,
    )
    source: Mapped[str | None] = mapped_column(String(100))
    notes: Mapped[str | None] = mapped_column(Text)

    # Pipeline-Uebersicht Flag
    # Jobs erscheinen erst nach Klick auf "To Interview" in der Pipeline-Uebersicht
    in_pipeline: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Besetzt-Datum
    filled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

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
        "Company", back_populates="ats_jobs",
    )
    contact: Mapped["CompanyContact | None"] = relationship(
        "CompanyContact",
    )
    pipeline_entries: Mapped[list["ATSPipelineEntry"]] = relationship(
        "ATSPipelineEntry", back_populates="ats_job", cascade="all, delete-orphan",
    )
    call_notes: Mapped[list["ATSCallNote"]] = relationship(
        "ATSCallNote", back_populates="ats_job", cascade="all, delete-orphan",
    )
    activities: Mapped[list["ATSActivity"]] = relationship(
        "ATSActivity",
        back_populates="ats_job",
        cascade="all, delete-orphan",
        order_by="ATSActivity.created_at.desc()",
    )

    # Indizes
    __table_args__ = (
        Index("ix_ats_jobs_company_id", "company_id"),
        Index("ix_ats_jobs_status", "status"),
        Index("ix_ats_jobs_priority", "priority"),
        Index("ix_ats_jobs_created_at", "created_at"),
    )

    @property
    def is_open(self) -> bool:
        """Prueft, ob die Stelle offen ist."""
        return self.status == ATSJobStatus.OPEN

    @property
    def salary_display(self) -> str:
        """Gibt die formatierte Gehaltsspanne zurueck."""
        if self.salary_min and self.salary_max:
            return f"{self.salary_min:,}€ - {self.salary_max:,}€"
        elif self.salary_min:
            return f"ab {self.salary_min:,}€"
        elif self.salary_max:
            return f"bis {self.salary_max:,}€"
        return ""
