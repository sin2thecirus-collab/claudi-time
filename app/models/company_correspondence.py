"""CompanyCorrespondence Model - E-Mail-Korrespondenz mit Unternehmen."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class CorrespondenceDirection(str, enum.Enum):
    """Richtung der Korrespondenz."""

    INBOUND = "inbound"
    OUTBOUND = "outbound"


class CompanyCorrespondence(Base):
    """Model fuer E-Mail-Korrespondenz mit Unternehmen."""

    __tablename__ = "company_correspondence"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
    )

    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("company_contacts.id", ondelete="SET NULL"),
    )

    # E-Mail-Daten
    direction: Mapped[CorrespondenceDirection] = mapped_column(
        Enum(CorrespondenceDirection, values_callable=lambda x: [e.value for e in x]),
        default=CorrespondenceDirection.OUTBOUND,
    )
    subject: Mapped[str] = mapped_column(String(500), nullable=False)
    body: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    # Relationships
    company: Mapped["Company"] = relationship("Company", back_populates="correspondence")
    contact: Mapped["CompanyContact | None"] = relationship("CompanyContact")

    __table_args__ = (
        Index("ix_company_correspondence_company_id", "company_id"),
        Index("ix_company_correspondence_sent_at", "sent_at"),
    )
