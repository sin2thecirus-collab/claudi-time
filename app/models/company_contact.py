"""CompanyContact Model - Ansprechpartner bei Unternehmen."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class CompanyContact(Base):
    """Model fuer Ansprechpartner bei Unternehmen."""

    __tablename__ = "company_contacts"

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

    # Kontaktdaten
    salutation: Mapped[str | None] = mapped_column(String(20))
    first_name: Mapped[str | None] = mapped_column(String(100))
    last_name: Mapped[str | None] = mapped_column(String(100))
    position: Mapped[str | None] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(500))
    phone: Mapped[str | None] = mapped_column(String(100))
    mobile: Mapped[str | None] = mapped_column(String(100))
    city: Mapped[str | None] = mapped_column(String(255))

    # Notizen
    notes: Mapped[str | None] = mapped_column(Text)

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

    # Relationship
    company: Mapped["Company"] = relationship("Company", back_populates="contacts")

    __table_args__ = (
        Index("ix_company_contacts_company_id", "company_id"),
        Index("ix_company_contacts_last_name", "last_name"),
    )

    @property
    def full_name(self) -> str:
        """Gibt den vollstaendigen Namen zurueck."""
        parts = [self.first_name, self.last_name]
        return " ".join(p for p in parts if p) or "Unbekannt"
