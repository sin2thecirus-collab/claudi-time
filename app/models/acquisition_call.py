"""AcquisitionCall Model - Akquise-Anrufe mit Disposition und Qualifizierung."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class AcquisitionCall(Base):
    """Dokumentiert jeden Akquise-Anruf mit Disposition und Qualifizierungsdaten."""

    __tablename__ = "acquisition_calls"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )

    # Foreign Keys
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="SET NULL"),
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("company_contacts.id", ondelete="SET NULL"),
    )
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="SET NULL"),
    )

    # Anruf-Details
    call_type: Mapped[str] = mapped_column(String(20), nullable=False)  # erstanruf/wiedervorlage/rueckruf
    disposition: Mapped[str] = mapped_column(String(30), nullable=False)  # nicht_erreicht/besetzt/...
    qualification_data: Mapped[dict | None] = mapped_column(JSONB)  # Fragen-Antworten
    notes: Mapped[str | None] = mapped_column(Text)
    duration_seconds: Mapped[int | None] = mapped_column(Integer)

    # Transkript (von Webex Recording via n8n + Whisper)
    transcript: Mapped[str | None] = mapped_column(Text)
    call_summary: Mapped[str | None] = mapped_column(Text)
    transcript_processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    webex_recording_id: Mapped[str | None] = mapped_column(String(255))

    # Aufzeichnung
    recording_consent: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false"), nullable=False,
    )  # Default false (§201 StGB)

    # Wiedervorlage
    follow_up_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    follow_up_note: Mapped[str | None] = mapped_column(String(500))

    # E-Mail nach Call
    email_sent: Mapped[bool] = mapped_column(Boolean, server_default=text("false"), nullable=False)
    email_consent: Mapped[bool] = mapped_column(Boolean, server_default=text("true"), nullable=False)

    # Timestamp
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # Relationships
    job: Mapped["Job"] = relationship("Job", back_populates="acquisition_calls")
    contact: Mapped["CompanyContact"] = relationship("CompanyContact", back_populates="acquisition_calls")
    company: Mapped["Company"] = relationship("Company")

    __table_args__ = (
        Index("idx_acq_calls_job", "job_id", text("created_at DESC")),
        Index("idx_acq_calls_followup", "follow_up_date", postgresql_where=text("follow_up_date IS NOT NULL")),
    )
