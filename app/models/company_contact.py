"""CompanyContact Model - Ansprechpartner bei Unternehmen."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func, text
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
    contact_number: Mapped[int | None] = mapped_column(
        Integer, server_default=text("nextval('company_contacts_contact_number_seq')")
    )
    city: Mapped[str | None] = mapped_column(String(255))

    # ── Akquise (Migration 032) ──
    source: Mapped[str | None] = mapped_column(String(20))  # "advertsdata" / "manual"
    contact_role: Mapped[str | None] = mapped_column(String(20))  # "firma" / "anzeige"
    phone_normalized: Mapped[str | None] = mapped_column(String(20))  # E.164 (+491701234567)

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

    # Akquise-Relationships
    acquisition_calls: Mapped[list["AcquisitionCall"]] = relationship(
        "AcquisitionCall", back_populates="contact",
    )
    acquisition_emails: Mapped[list["AcquisitionEmail"]] = relationship(
        "AcquisitionEmail", back_populates="contact",
    )

    __table_args__ = (
        Index("ix_company_contacts_company_id", "company_id"),
        Index("ix_company_contacts_last_name", "last_name"),
        Index("idx_contacts_phone_norm", "phone_normalized", postgresql_where=text("phone_normalized IS NOT NULL")),
    )

    @property
    def contact_number_display(self) -> str | None:
        """Gibt die formatierte Kontakt-Nummer zurueck (z.B. '001234')."""
        if self.contact_number is not None:
            return f"00{self.contact_number}"
        return None

    @property
    def full_name(self) -> str:
        """Gibt den vollstaendigen Namen zurueck."""
        parts = [self.first_name, self.last_name]
        return " ".join(p for p in parts if p) or "Unbekannt"
