"""CompanyDocument Model - Dokumente zu Unternehmen (Vertraege, etc.)."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class CompanyDocument(Base):
    """Model fuer Dokumente die zu Unternehmen gehoeren."""

    __tablename__ = "company_documents"

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

    # Datei-Informationen
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    file_path: Mapped[str] = mapped_column(Text, nullable=False)  # R2 Key
    file_size: Mapped[int | None] = mapped_column(Integer)  # Bytes
    mime_type: Mapped[str | None] = mapped_column(String(100))

    # Notizen
    notes: Mapped[str | None] = mapped_column(Text)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    # Relationship
    company: Mapped["Company"] = relationship("Company", back_populates="documents")

    __table_args__ = (
        Index("ix_company_documents_company_id", "company_id"),
    )
