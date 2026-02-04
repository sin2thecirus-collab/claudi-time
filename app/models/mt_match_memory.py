"""MT Match Memory - Gedaechtnis fuer Match-Entscheidungen."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class MTMatchMemory(Base):
    """Speichert Match-Entscheidungen als Gedaechtnis.

    Damit MT sich erinnert:
    - Welche Kandidaten wurden schon zu welchen Jobs/Firmen vorgeschlagen?
    - Was war das Ergebnis? (gematcht, abgelehnt, platziert)
    - Soll der Kandidat nie wieder fuer diese Firma vorgeschlagen werden?
    """

    __tablename__ = "mt_match_memory"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # Wer wurde wem vorgeschlagen?
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("candidates.id", ondelete="SET NULL"),
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="SET NULL"),
    )
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="SET NULL"),
    )

    # Was ist passiert?
    action: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # "matched", "rejected", "placed", "presented"

    # Warum abgelehnt?
    rejection_reason: Mapped[str | None] = mapped_column(Text)

    # Nie wieder fuer diese Firma?
    never_again_company: Mapped[bool] = mapped_column(Boolean, default=False)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
