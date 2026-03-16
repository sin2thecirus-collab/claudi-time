"""EmailBlocklist Model — Domain-Blocklist fuer Vorstellungs-E-Mails.

Wenn ein Unternehmen die Loeschung seiner Daten verlangt, wird die Domain
hier eingetragen. Alle zukuenftigen E-Mails an diese Domain werden blockiert.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class EmailBlocklist(Base):
    """Blockierte E-Mail-Domains — keine Vorstellungen mehr an diese Domains."""

    __tablename__ = "email_blocklist"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # Die blockierte Domain (z.B. "example.de")
    domain: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False, index=True
    )

    # Grund der Blockierung
    reason: Mapped[str | None] = mapped_column(Text)

    # Referenz-Daten (nach Loeschung des Unternehmens)
    company_name_before_deletion: Mapped[str | None] = mapped_column(String(500))
    contact_email: Mapped[str | None] = mapped_column(String(255))

    # Wann und wie blockiert
    blocked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    blocked_by: Mapped[str] = mapped_column(
        String(50), default="manual", server_default="manual"
    )  # "auto_reply_monitor" oder "manual"

    __table_args__ = (
        Index("ix_email_blocklist_blocked_at", "blocked_at"),
    )
