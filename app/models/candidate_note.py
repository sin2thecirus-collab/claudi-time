"""CandidateNote Model - Notizen-Verlauf fuer Kandidaten.

Jede Notiz ist ein eigener Eintrag mit Titel, Inhalt, Datum und Quelle.
Ersetzt das alte Freitext-Feld candidate_notes auf dem Kandidaten-Model.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class CandidateNote(Base):
    """Model fuer einzelne Notizen zu einem Kandidaten."""

    __tablename__ = "candidate_notes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("candidates.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Inhalt
    title: Mapped[str | None] = mapped_column(String(500))
    content: Mapped[str] = mapped_column(Text, nullable=False)

    # Quelle: "manual" (User), "ki_transkription", "n8n", "system"
    source: Mapped[str | None] = mapped_column(String(50))

    # Notiz-Datum (waehlbar vom User, Default = jetzt)
    note_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    # Erstellungszeitpunkt (immer automatisch)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    # Relationships
    candidate: Mapped["Candidate"] = relationship("Candidate", back_populates="notes_list")

    __table_args__ = (
        Index("ix_candidate_notes_candidate_id", "candidate_id"),
        Index("ix_candidate_notes_note_date", "note_date"),
        Index("ix_candidate_notes_created_at", "created_at"),
    )
