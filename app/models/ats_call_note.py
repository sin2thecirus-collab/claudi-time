"""ATSCallNote Model - Telefonat-Notizen und Gespraechsprotokolle."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class CallType(str, enum.Enum):
    """Typ des Telefonats."""

    ACQUISITION = "acquisition"
    QUALIFICATION = "qualification"
    FOLLOWUP = "followup"
    CANDIDATE_CALL = "candidate_call"


class CallDirection(str, enum.Enum):
    """Richtung des Anrufs."""

    OUTBOUND = "outbound"
    INBOUND = "inbound"


# Deutsche Labels fuer UI
CALL_TYPE_LABELS = {
    CallType.ACQUISITION: "Akquise",
    CallType.QUALIFICATION: "Qualifizierung",
    CallType.FOLLOWUP: "Nachfassen",
    CallType.CANDIDATE_CALL: "Kandidatengespraech",
}


class ATSCallNote(Base):
    """Model fuer Telefonat-Notizen."""

    __tablename__ = "ats_call_notes"

    # Primaerschluessel
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # Foreign Keys (alle optional — Call-Note kann zu verschiedenen Entitaeten gehoeren)
    ats_job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ats_jobs.id", ondelete="SET NULL"),
        nullable=True,
    )
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="SET NULL"),
        nullable=True,
    )
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("candidates.id", ondelete="SET NULL"),
        nullable=True,
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("company_contacts.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Gespraechs-Details
    call_type: Mapped[CallType] = mapped_column(
        Enum(CallType, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    raw_notes: Mapped[str | None] = mapped_column(Text)
    action_items: Mapped[dict | None] = mapped_column(JSONB)
    duration_minutes: Mapped[int | None] = mapped_column(Integer)

    # Richtung (outbound/inbound)
    direction: Mapped[CallDirection | None] = mapped_column(
        Enum(CallDirection, values_callable=lambda x: [e.value for e in x]),
        nullable=True,
    )

    # Zeitpunkt des Anrufs
    called_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    # Relationships
    ats_job: Mapped["ATSJob | None"] = relationship(
        "ATSJob", back_populates="call_notes",
        foreign_keys=[ats_job_id],
    )
    company: Mapped["Company | None"] = relationship("Company")
    candidate: Mapped["Candidate | None"] = relationship("Candidate")
    contact: Mapped["CompanyContact | None"] = relationship("CompanyContact")
    todos: Mapped[list["ATSTodo"]] = relationship(
        "ATSTodo", back_populates="call_note", cascade="all, delete-orphan",
    )

    # Indizes
    __table_args__ = (
        Index("ix_ats_call_notes_company_id", "company_id"),
        Index("ix_ats_call_notes_candidate_id", "candidate_id"),
        Index("ix_ats_call_notes_ats_job_id", "ats_job_id"),
        Index("ix_ats_call_notes_call_type", "call_type"),
        Index("ix_ats_call_notes_called_at", "called_at"),
    )

    @property
    def call_type_label(self) -> str:
        """Gibt das deutsche Label des Call-Types zurueck."""
        return CALL_TYPE_LABELS.get(self.call_type, self.call_type.value)
