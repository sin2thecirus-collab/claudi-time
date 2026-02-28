"""AcquisitionEmail Model - Gesendete Akquise-E-Mails (Initial, Follow-up, Break-up)."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class AcquisitionEmail(Base):
    """Speichert alle gesendeten Akquise-E-Mails (zugeordnet zu Job + Contact + Company)."""

    __tablename__ = "acquisition_emails"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )

    # Foreign Keys
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="SET NULL"),
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("company_contacts.id", ondelete="SET NULL"),
    )
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="SET NULL"),
    )
    parent_email_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("acquisition_emails.id", ondelete="SET NULL"),
    )  # Thread-Linking (Follow-up/Break-up bezieht sich auf Initial)

    # E-Mail-Daten
    from_email: Mapped[str | None] = mapped_column(String(500))  # Absender-Postfach
    to_email: Mapped[str | None] = mapped_column(String(500))
    subject: Mapped[str | None] = mapped_column(String(500))
    body_html: Mapped[str | None] = mapped_column(Text)  # NUR fuer Signatur
    body_plain: Mapped[str | None] = mapped_column(Text)  # Wird als Content gesendet

    # Fiktiver Kandidat (GPT-generiert)
    candidate_fiction: Mapped[dict | None] = mapped_column(JSONB)

    # Sequenz-Tracking
    email_type: Mapped[str | None] = mapped_column(String(20))  # initial/follow_up/break_up
    sequence_position: Mapped[int] = mapped_column(
        Integer, server_default=text("1"), nullable=False,
    )  # 1=Initial, 2=Follow-up, 3=Break-up

    # Status
    status: Mapped[str] = mapped_column(
        String(20), server_default=text("'draft'"), nullable=False,
    )  # draft/scheduled/sent/failed/bounced/replied

    # Versand-Details
    scheduled_send_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # Wann soll gesendet werden (2h Delay)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    graph_message_id: Mapped[str | None] = mapped_column(String(255))  # Microsoft Graph Message-ID
    unsubscribe_token: Mapped[str | None] = mapped_column(String(64), unique=True)  # Abmelde-Link

    # Timestamp
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # Relationships
    job: Mapped["Job"] = relationship("Job", back_populates="acquisition_emails")
    contact: Mapped["CompanyContact"] = relationship("CompanyContact", back_populates="acquisition_emails")
    company: Mapped["Company"] = relationship("Company")
    parent_email: Mapped["AcquisitionEmail | None"] = relationship(
        "AcquisitionEmail", remote_side=[id], foreign_keys=[parent_email_id],
    )

    __table_args__ = (
        Index("idx_acq_emails_job", "job_id", text("created_at DESC")),
        Index("idx_acq_emails_parent", "parent_email_id", postgresql_where=text("parent_email_id IS NOT NULL")),
        Index("idx_acq_emails_unsub", "unsubscribe_token"),
    )
