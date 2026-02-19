"""OutreachItem Model — Einzelne Kandidaten pro Outreach-Batch."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class OutreachItem(Base):
    """Einzelner Kandidat in einem Outreach-Batch.

    Kampagnen-Typen:
    - "bekannt": Quelle = Bestand → Hallo Herr/Frau (Kampagne 1)
    - "erstkontakt": Quelle != Bestand → Sehr geehrte/r (Kampagne 2)
    """

    __tablename__ = "outreach_items"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("outreach_batches.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("candidates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Kampagne
    campaign_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="erstkontakt"
    )  # "bekannt" / "erstkontakt"
    source_override: Mapped[str | None] = mapped_column(String(50))  # Milad setzt Quelle manuell

    # Status
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="prepared", index=True
    )  # "prepared" / "approved" / "sent" / "skipped" / "error"

    # Instantly-Tracking
    instantly_lead_id: Mapped[str | None] = mapped_column(String(255))

    # Versand
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    send_error: Mapped[str | None] = mapped_column(Text)

    # Timestamp
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    # Relationships
    batch = relationship("OutreachBatch", back_populates="items")
    candidate = relationship("Candidate", foreign_keys=[candidate_id], lazy="selectin")
