"""ATSActivity Model - Aktivitaets-Timeline fuer ATS."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ActivityType(str, enum.Enum):
    """Typ der ATS-Aktivitaet."""

    STAGE_CHANGED = "stage_changed"
    NOTE_ADDED = "note_added"
    TODO_CREATED = "todo_created"
    TODO_COMPLETED = "todo_completed"
    EMAIL_SENT = "email_sent"
    EMAIL_RECEIVED = "email_received"
    CALL_LOGGED = "call_logged"
    CANDIDATE_ADDED = "candidate_added"
    CANDIDATE_REMOVED = "candidate_removed"
    JOB_CREATED = "job_created"
    JOB_STATUS_CHANGED = "job_status_changed"
    CANDIDATE_RESPONSE = "candidate_response"
    TODO_AUTO_COMPLETED = "todo_auto_completed"
    TODO_CANCELLED = "todo_cancelled"


# Deutsche Labels fuer UI
ACTIVITY_TYPE_LABELS = {
    ActivityType.STAGE_CHANGED: "Stage geaendert",
    ActivityType.NOTE_ADDED: "Notiz hinzugefuegt",
    ActivityType.TODO_CREATED: "Aufgabe erstellt",
    ActivityType.TODO_COMPLETED: "Aufgabe erledigt",
    ActivityType.EMAIL_SENT: "E-Mail gesendet",
    ActivityType.EMAIL_RECEIVED: "E-Mail empfangen",
    ActivityType.CALL_LOGGED: "Anruf protokolliert",
    ActivityType.CANDIDATE_ADDED: "Kandidat hinzugefuegt",
    ActivityType.CANDIDATE_REMOVED: "Kandidat entfernt",
    ActivityType.JOB_CREATED: "Stelle erstellt",
    ActivityType.JOB_STATUS_CHANGED: "Status geaendert",
    ActivityType.CANDIDATE_RESPONSE: "Kandidaten-Antwort",
    ActivityType.TODO_AUTO_COMPLETED: "Aufgabe auto-erledigt",
    ActivityType.TODO_CANCELLED: "Aufgabe abgebrochen",
}

# Icons fuer UI (Heroicons Name)
ACTIVITY_TYPE_ICONS = {
    ActivityType.STAGE_CHANGED: "arrows-right-left",
    ActivityType.NOTE_ADDED: "document-text",
    ActivityType.TODO_CREATED: "clipboard-document-check",
    ActivityType.TODO_COMPLETED: "check-circle",
    ActivityType.EMAIL_SENT: "paper-airplane",
    ActivityType.EMAIL_RECEIVED: "inbox-arrow-down",
    ActivityType.CALL_LOGGED: "phone",
    ActivityType.CANDIDATE_ADDED: "user-plus",
    ActivityType.CANDIDATE_REMOVED: "user-minus",
    ActivityType.JOB_CREATED: "briefcase",
    ActivityType.JOB_STATUS_CHANGED: "arrow-path",
    ActivityType.CANDIDATE_RESPONSE: "chat-bubble-left-right",
    ActivityType.TODO_AUTO_COMPLETED: "sparkles",
    ActivityType.TODO_CANCELLED: "x-circle",
}


class ATSActivity(Base):
    """Model fuer die ATS-Aktivitaets-Timeline."""

    __tablename__ = "ats_activities"

    # Primaerschluessel
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # Aktivitaets-Details
    activity_type: Mapped[ActivityType] = mapped_column(
        Enum(ActivityType, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    metadata_json: Mapped[dict | None] = mapped_column(
        JSONB,
        name="metadata",
    )

    # Foreign Keys (alle optional)
    ats_job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ats_jobs.id", ondelete="SET NULL"),
        nullable=True,
    )
    pipeline_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ats_pipeline_entries.id", ondelete="SET NULL"),
        nullable=True,
    )
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="SET NULL"),
        nullable=True,
    )
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("candidates.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    # Relationships
    ats_job: Mapped["ATSJob | None"] = relationship(
        "ATSJob", back_populates="activities",
    )
    pipeline_entry: Mapped["ATSPipelineEntry | None"] = relationship(
        "ATSPipelineEntry", back_populates="activities",
    )

    # Indizes
    __table_args__ = (
        Index("ix_ats_activities_ats_job_id", "ats_job_id"),
        Index("ix_ats_activities_pipeline_entry_id", "pipeline_entry_id"),
        Index("ix_ats_activities_company_id", "company_id"),
        Index("ix_ats_activities_created_at", "created_at"),
    )

    @property
    def type_label(self) -> str:
        """Gibt das deutsche Label des Aktivitaets-Typs zurueck."""
        return ACTIVITY_TYPE_LABELS.get(self.activity_type, self.activity_type.value)

    @property
    def type_icon(self) -> str:
        """Gibt den Icon-Namen des Aktivitaets-Typs zurueck."""
        return ACTIVITY_TYPE_ICONS.get(self.activity_type, "information-circle")
