"""CandidateTask Model — Aufgaben die aus E-Mail-Antworten entstehen."""

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class CandidateTask(Base):
    """Aufgaben fuer Kandidaten — erstellt durch GPT-Antwort-Analyse oder manuell.

    Task-Typen:
    - follow_up: Kandidat hat Interesse, auf Termin warten
    - call_back: Kandidat in X Monaten erneut kontaktieren
    - review: Antwort manuell pruefen
    - termin: Telefonat-Termin
    - manual: Manuell erstellt
    """

    __tablename__ = "candidate_tasks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("candidates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Aufgaben-Details
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    task_type: Mapped[str] = mapped_column(
        String(50), nullable=False, default="manual"
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="open", index=True
    )  # "open" / "done" / "cancelled"
    priority: Mapped[str] = mapped_column(
        String(20), nullable=False, default="normal"
    )  # "low" / "normal" / "high" / "urgent"

    # Faelligkeit
    due_date: Mapped[date | None] = mapped_column(Date, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Herkunft
    source: Mapped[str | None] = mapped_column(String(50), default="system")  # "system" / "gpt" / "manual"
    source_email_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("candidate_emails.id", ondelete="SET NULL"),
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
    candidate = relationship("Candidate", foreign_keys=[candidate_id], lazy="selectin")
    source_email = relationship("CandidateEmail", foreign_keys=[source_email_id], lazy="selectin")
