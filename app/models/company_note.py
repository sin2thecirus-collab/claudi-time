"""CompanyNote Model - Notizen-Verlauf fuer Unternehmen."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class CompanyNote(Base):
    """Model fuer einzelne Notizen zu einem Unternehmen."""

    __tablename__ = "company_notes"

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

    # Inhalt
    title: Mapped[str | None] = mapped_column(String(500))
    content: Mapped[str] = mapped_column(Text, nullable=False)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    # Relationships
    company: Mapped["Company"] = relationship("Company", back_populates="notes_list")
    contact: Mapped["CompanyContact | None"] = relationship("CompanyContact")

    __table_args__ = (
        Index("ix_company_notes_company_id", "company_id"),
        Index("ix_company_notes_created_at", "created_at"),
    )
