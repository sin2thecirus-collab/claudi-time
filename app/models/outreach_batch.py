"""OutreachBatch Model — Tages-Batches fuer Rundmail."""

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class OutreachBatch(Base):
    """Tages-Batch fuer die Rundmail-Aktion.

    Jeden Morgen um 6:00 erstellt n8n einen neuen Batch mit den naechsten
    X Finance-Kandidaten. Milad prueft die Liste auf /emails → Tab "Rundmail"
    und klickt "Ausgewaehlte senden".
    """

    __tablename__ = "outreach_batches"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    batch_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    total_candidates: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    approved_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sent_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="prepared", index=True
    )  # "prepared" / "partial" / "sent" / "cancelled"

    max_per_mailbox: Mapped[int] = mapped_column(Integer, nullable=False, default=30)

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
    items: Mapped[list["OutreachItem"]] = relationship(
        "OutreachItem",
        back_populates="batch",
        cascade="all, delete-orphan",
    )
