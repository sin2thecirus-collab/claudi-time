"""CandidateEmail Model — E-Mail-Logging fuer Kandidaten-Korrespondenz."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class CandidateEmail(Base):
    """Jede E-Mail (gesendet + empfangen) wird hier geloggt.

    Wird von n8n befuellt:
    - Nicht-erreicht-Sequenz (IONOS SMTP)
    - Rundmail (Instantly)
    - Antworten (IMAP / Instantly Webhook)
    - Auto-Replies (GPT-generiert)
    """

    __tablename__ = "candidate_emails"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("candidates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # E-Mail-Inhalt
    subject: Mapped[str] = mapped_column(String(500), nullable=False)
    body_text: Mapped[str | None] = mapped_column(Text)
    body_html: Mapped[str | None] = mapped_column(Text)

    # Richtung + Kanal
    direction: Mapped[str] = mapped_column(
        String(20), nullable=False, default="outbound"
    )  # "outbound" / "inbound"
    channel: Mapped[str] = mapped_column(
        String(20), nullable=False, default="ionos"
    )  # "ionos" / "instantly" / "microsoft_graph"

    # Sequenz-Tracking
    sequence_type: Mapped[str | None] = mapped_column(String(50))  # "nicht_erreicht" / "rundmail_bekannt" / "rundmail_erstkontakt"
    sequence_step: Mapped[int | None] = mapped_column(Integer)  # 1, 2, 3

    # Adressen
    from_address: Mapped[str | None] = mapped_column(String(500))
    to_address: Mapped[str | None] = mapped_column(String(500))

    # Message-IDs (fuer Threading)
    message_id: Mapped[str | None] = mapped_column(String(500))
    in_reply_to: Mapped[str | None] = mapped_column(String(500))
    conversation_id: Mapped[str | None] = mapped_column(String(500))

    # Instantly-Tracking
    instantly_lead_id: Mapped[str | None] = mapped_column(String(255))
    instantly_campaign_id: Mapped[str | None] = mapped_column(String(255))

    # Fehler
    send_error: Mapped[str | None] = mapped_column(Text)

    # Timestamp
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    # Relationships
    candidate = relationship("Candidate", foreign_keys=[candidate_id], lazy="selectin")
