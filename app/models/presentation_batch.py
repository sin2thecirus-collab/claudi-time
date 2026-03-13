"""PresentationBatch Model — CSV-Bulk-Upload Tracking.

Trackt den Fortschritt eines CSV-Bulk-Uploads fuer Kandidaten-Vorstellungen.
Unterstuetzt Row-Level-Tracking fuer Absturz-Recovery.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class PresentationBatch(Base):
    """Batch-Tracking fuer CSV-Bulk-Vorstellungen."""

    __tablename__ = "presentation_batches"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("candidates.id", ondelete="SET NULL"),
    )
    csv_filename: Mapped[str | None] = mapped_column(String(255))

    # Fortschritt
    total_rows: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    processed: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    skipped: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    errors: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    # Status: processing / completed / failed
    status: Mapped[str] = mapped_column(
        String(20), default="processing", server_default="processing"
    )

    # Mailbox-Verteilung: {"hamdard@sincirus.com": 5, "m.hamdard@...": 3}
    mailbox_distribution: Mapped[dict | None] = mapped_column(JSONB)

    # Fehler-Details: [{"row_index": 3, "error": "...", "company_name": "..."}]
    error_details: Mapped[list | None] = mapped_column(JSONB)

    # Row-Level-Tracking fuer Absturz-Recovery:
    # [{"row_index": 0, "presentation_id": "uuid", "status": "sent"}, ...]
    processed_rows: Mapped[list | None] = mapped_column(JSONB)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    candidate: Mapped["Candidate | None"] = relationship(
        "Candidate", foreign_keys=[candidate_id]
    )
    presentations: Mapped[list["ClientPresentation"]] = relationship(
        "ClientPresentation", foreign_keys="ClientPresentation.batch_id", back_populates="batch"
    )
