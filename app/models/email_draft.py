"""EmailDraft Model - E-Mail-Entwuerfe und gesendete Emails."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class EmailDraftStatus(str, enum.Enum):
    """Status eines E-Mail-Entwurfs."""
    DRAFT = "draft"           # Wartet auf Pruefung
    APPROVED = "approved"     # Freigegeben, wird gesendet
    SENT = "sent"             # Erfolgreich gesendet
    CANCELLED = "cancelled"   # Verworfen
    FAILED = "failed"         # Senden fehlgeschlagen


class EmailType(str, enum.Enum):
    """Typ der automatischen Email."""
    KONTAKTDATEN = "kontaktdaten"               # Recruiter-Kontaktdaten nach Quali
    STELLENAUSSCHREIBUNG = "stellenausschreibung" # Job-Details zusenden
    INDIVIDUELL = "individuell"                   # GPT-generierter Inhalt


class EmailDraft(Base):
    """Model fuer automatische E-Mail-Entwuerfe und gesendete Emails.

    Wird automatisch erstellt wenn GPT email_actions aus einem Call extrahiert.
    Typ kontaktdaten + stellenausschreibung = sofort gesendet (auto).
    Typ individuell = Draft → Recruiter prueft → sendet.
    """

    __tablename__ = "email_drafts"

    # Primaerschluessel
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # Verknuepfungen
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("candidates.id", ondelete="SET NULL"),
        index=True,
    )
    ats_job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ats_jobs.id", ondelete="SET NULL"),
    )
    call_note_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ats_call_notes.id", ondelete="SET NULL"),
    )

    # Email-Details
    email_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default=EmailType.INDIVIDUELL.value,
    )
    to_email: Mapped[str] = mapped_column(String(500), nullable=False)
    subject: Mapped[str] = mapped_column(String(500), nullable=False)
    body_html: Mapped[str] = mapped_column(Text, nullable=False)

    # Status
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=EmailDraftStatus.DRAFT.value,
        index=True,
    )

    # GPT-Kontext (warum diese Email erstellt wurde)
    gpt_context: Mapped[str | None] = mapped_column(Text)

    # Versand-Details
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    send_error: Mapped[str | None] = mapped_column(Text)
    microsoft_message_id: Mapped[str | None] = mapped_column(String(255))

    # Auto-Send Flag: True = ohne Pruefung sofort senden
    auto_send: Mapped[bool] = mapped_column(default=False, server_default="false")

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
    candidate = relationship("Candidate", foreign_keys=[candidate_id], lazy="selectin")

    @property
    def is_pending(self) -> bool:
        """Draft wartet auf Pruefung."""
        return self.status == EmailDraftStatus.DRAFT.value

    @property
    def is_sent(self) -> bool:
        """Email wurde gesendet."""
        return self.status == EmailDraftStatus.SENT.value
