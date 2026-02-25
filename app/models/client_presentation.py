"""ClientPresentation Model — Kandidaten-Vorstellung an Unternehmen.

Trackt den gesamten Vorstellungsprozess:
- Initiale Vorstellungs-E-Mail (KI-generiert oder manuell)
- Follow-Up-Sequenz (2 Tage + 3 Tage Erinnerungen)
- Kunden-Antwort-Klassifizierung (KI-basiert)
- Fallback-E-Mail-Kaskade (bewerber@, karriere@, hr@ etc.)
- Triple-Dokumentation (Kandidat, Job, Unternehmen)
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class PresentationStatus(str, enum.Enum):
    """Status einer Kundenvorstellung."""

    DRAFT = "draft"                    # Entwurf (noch nicht gesendet)
    SENT = "sent"                      # Initiale E-Mail gesendet
    FOLLOWUP_1 = "followup_1"         # 1. Erinnerung gesendet (nach 2 Tagen)
    FOLLOWUP_2 = "followup_2"         # 2. Erinnerung gesendet (nach 3 weiteren Tagen)
    RESPONDED = "responded"            # Kunde hat geantwortet
    NO_RESPONSE = "no_response"        # Keine Antwort nach kompletter Sequenz
    CANCELLED = "cancelled"            # Sequenz manuell abgebrochen


class PresentationMode(str, enum.Enum):
    """Wie wurde die Vorstellungs-E-Mail erstellt."""

    AI_GENERATED = "ai_generated"      # Claude hat die E-Mail generiert
    CUSTOM = "custom"                  # Manuell vom Berater geschrieben


class ClientResponseCategory(str, enum.Enum):
    """KI-klassifizierte Kunden-Antwort-Kategorie."""

    INTERESSE_JA = "interesse_ja"              # Interesse, moechte Profil sehen
    TERMIN_VORSCHLAG = "termin_vorschlag"      # Moechte Gespraech/Termin vereinbaren
    SPAETER_MELDEN = "spaeter_melden"          # Spaeter wieder melden (in X Wochen)
    KEIN_INTERESSE = "kein_interesse"           # Kein Interesse
    BEREITS_BESETZT = "bereits_besetzt"        # Stelle bereits besetzt
    SONSTIGES = "sonstiges"                    # Sonstige Antwort


class ClientPresentation(Base):
    """Model fuer Kandidaten-Vorstellungen an Unternehmen.

    Eine Vorstellung verknuepft einen Match mit einem Unternehmen
    und trackt den gesamten E-Mail-Prozess inkl. Follow-Ups.
    """

    __tablename__ = "client_presentations"

    # Primaerschluessel
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # Verknuepfungen
    match_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("matches.id", ondelete="SET NULL"),
        nullable=True,
    )
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
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("company_contacts.id", ondelete="SET NULL"),
    )

    # E-Mail-Inhalte
    email_to: Mapped[str] = mapped_column(String(500), nullable=False)
    email_from: Mapped[str] = mapped_column(String(500), nullable=False)
    email_subject: Mapped[str] = mapped_column(String(500), nullable=False)
    email_body_text: Mapped[str | None] = mapped_column(Text)
    email_signature_html: Mapped[str | None] = mapped_column(Text)
    mailbox_used: Mapped[str | None] = mapped_column(String(100))  # z.B. "hamdard@sincirus-karriere.de"

    # Vorstellungsmodus
    presentation_mode: Mapped[str] = mapped_column(
        String(20), default="ai_generated"
    )  # ai_generated / custom

    # PDF-Anhang
    pdf_attached: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    pdf_r2_key: Mapped[str | None] = mapped_column(String(500))  # Cloudflare R2 Pfad

    # Sequenz-Tracking
    status: Mapped[str] = mapped_column(String(30), default="sent", server_default="sent")
    sequence_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    sequence_step: Mapped[int] = mapped_column(Integer, default=1, server_default="1")

    # Zeitstempel fuer jede Phase
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    followup1_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    followup2_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Kunden-Antwort
    client_response_category: Mapped[str | None] = mapped_column(String(30))
    client_response_text: Mapped[str | None] = mapped_column(Text)
    client_response_raw: Mapped[str | None] = mapped_column(Text)  # Original-E-Mail-Text
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Spaeter-melden: Wiedervorlage-Datum
    callback_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Auto-Reply an Kunden (nach KI-Klassifizierung)
    auto_reply_sent: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    auto_reply_text: Mapped[str | None] = mapped_column(Text)

    # Fallback-E-Mail-Kaskade (wenn kein Ansprechpartner vorhanden)
    is_fallback: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    fallback_domain: Mapped[str | None] = mapped_column(String(255))
    fallback_attempts: Mapped[dict | None] = mapped_column(JSONB)  # [{"email": "bewerber@...", "status": "bounced", "tried_at": "..."}]
    fallback_successful_email: Mapped[str | None] = mapped_column(String(500))  # E-Mail die nicht gebounced ist

    # n8n-Integration
    n8n_execution_id: Mapped[str | None] = mapped_column(String(255))

    # Dokumentation-Links (fuer Triple-Dokumentation)
    correspondence_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("company_correspondence.id", ondelete="SET NULL"),
    )  # Link zur company_correspondence Tabelle
    pipeline_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ats_pipeline_entries.id", ondelete="SET NULL"),
    )  # Link zum ATS Pipeline Entry

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
    match: Mapped["Match | None"] = relationship("Match", foreign_keys=[match_id])
    candidate: Mapped["Candidate | None"] = relationship("Candidate", foreign_keys=[candidate_id])
    job: Mapped["Job | None"] = relationship("Job", foreign_keys=[job_id])
    company: Mapped["Company | None"] = relationship("Company", foreign_keys=[company_id])
    contact: Mapped["CompanyContact | None"] = relationship("CompanyContact", foreign_keys=[contact_id])
    correspondence: Mapped["CompanyCorrespondence | None"] = relationship(
        "CompanyCorrespondence", foreign_keys=[correspondence_id]
    )
    pipeline_entry: Mapped["ATSPipelineEntry | None"] = relationship(
        "ATSPipelineEntry", foreign_keys=[pipeline_entry_id]
    )

    # Indizes
    __table_args__ = (
        Index("ix_client_presentations_match_id", "match_id"),
        Index("ix_client_presentations_candidate_id", "candidate_id"),
        Index("ix_client_presentations_job_id", "job_id"),
        Index("ix_client_presentations_company_id", "company_id"),
        Index("ix_client_presentations_contact_id", "contact_id"),
        Index("ix_client_presentations_status", "status"),
        Index("ix_client_presentations_created_at", "created_at"),
        Index("ix_client_presentations_is_fallback", "is_fallback"),
    )

    @property
    def is_sequence_complete(self) -> bool:
        """Prüft ob die Sequenz abgeschlossen ist (alle Follow-Ups gesendet oder Antwort erhalten)."""
        return self.status in ("responded", "no_response", "cancelled")

    @property
    def awaiting_response(self) -> bool:
        """Prüft ob auf eine Antwort gewartet wird."""
        return self.status in ("sent", "followup_1", "followup_2") and self.sequence_active
