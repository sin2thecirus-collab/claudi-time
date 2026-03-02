"""CandidateDocument Model - Dateien zu Kandidaten (Lebenslauf, Zeugnisse, etc.)."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class CandidateDocument(Base):
    """Model fuer Dateien die zu Kandidaten gehoeren (CV, Zeugnisse, Zertifikate)."""

    __tablename__ = "candidate_documents"

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

    # Datei-Informationen
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    file_path: Mapped[str] = mapped_column(Text, nullable=False)  # R2 Object Key
    file_size: Mapped[int | None] = mapped_column(Integer)  # Bytes
    mime_type: Mapped[str | None] = mapped_column(String(100))

    # Kategorie (lebenslauf, zeugnis, sonstiges)
    category: Mapped[str | None] = mapped_column(String(50))

    # Notizen
    notes: Mapped[str | None] = mapped_column(Text)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    # Relationship
    candidate: Mapped["Candidate"] = relationship("Candidate", back_populates="documents")

    __table_args__ = (
        Index("ix_candidate_documents_candidate_id", "candidate_id"),
    )
