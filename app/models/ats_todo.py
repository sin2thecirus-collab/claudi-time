"""ATSTodo Model - Aufgaben und To-Dos."""

import enum
import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Enum, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class TodoStatus(str, enum.Enum):
    """Status einer Aufgabe."""

    OPEN = "open"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    CANCELLED = "cancelled"


class TodoPriority(str, enum.Enum):
    """Prioritaet einer Aufgabe."""

    UNWICHTIG = "unwichtig"
    MITTELMAESSIG = "mittelmaessig"
    WICHTIG = "wichtig"
    DRINGEND = "dringend"
    SEHR_DRINGEND = "sehr_dringend"


# Deutsche Labels fuer UI
TODO_STATUS_LABELS = {
    TodoStatus.OPEN: "Offen",
    TodoStatus.IN_PROGRESS: "In Bearbeitung",
    TodoStatus.DONE: "Erledigt",
    TodoStatus.CANCELLED: "Abgebrochen",
}

TODO_PRIORITY_LABELS = {
    TodoPriority.UNWICHTIG: "Unwichtig",
    TodoPriority.MITTELMAESSIG: "Mittelmaessig",
    TodoPriority.WICHTIG: "Wichtig",
    TodoPriority.DRINGEND: "Dringend",
    TodoPriority.SEHR_DRINGEND: "Sehr dringend",
}

TODO_PRIORITY_COLORS = {
    TodoPriority.UNWICHTIG: "gray",
    TodoPriority.MITTELMAESSIG: "blue",
    TodoPriority.WICHTIG: "amber",
    TodoPriority.DRINGEND: "orange",
    TodoPriority.SEHR_DRINGEND: "red",
}


class ATSTodo(Base):
    """Model fuer Aufgaben und To-Dos."""

    __tablename__ = "ats_todos"

    # Primaerschluessel
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # Aufgaben-Details
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    # Status und Prioritaet
    status: Mapped[TodoStatus] = mapped_column(
        Enum(TodoStatus, values_callable=lambda x: [e.value for e in x]),
        default=TodoStatus.OPEN,
    )
    priority: Mapped[TodoPriority] = mapped_column(
        Enum(TodoPriority, values_callable=lambda x: [e.value for e in x]),
        default=TodoPriority.WICHTIG,
    )

    # Faelligkeit
    due_date: Mapped[date | None] = mapped_column(Date)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Foreign Keys (alle optional — Todo kann zu verschiedenen Entitaeten gehoeren)
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
    ats_job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ats_jobs.id", ondelete="SET NULL"),
        nullable=True,
    )
    call_note_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ats_call_notes.id", ondelete="SET NULL"),
        nullable=True,
    )
    pipeline_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ats_pipeline_entries.id", ondelete="SET NULL"),
        nullable=True,
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("company_contacts.id", ondelete="SET NULL"),
        nullable=True,
    )

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
    company: Mapped["Company | None"] = relationship("Company")
    candidate: Mapped["Candidate | None"] = relationship("Candidate")
    ats_job: Mapped["ATSJob | None"] = relationship("ATSJob")
    call_note: Mapped["ATSCallNote | None"] = relationship(
        "ATSCallNote", back_populates="todos",
    )
    contact: Mapped["CompanyContact | None"] = relationship("CompanyContact")

    # Indizes
    __table_args__ = (
        Index("ix_ats_todos_status", "status"),
        Index("ix_ats_todos_priority", "priority"),
        Index("ix_ats_todos_due_date", "due_date"),
        Index("ix_ats_todos_company_id", "company_id"),
        Index("ix_ats_todos_candidate_id", "candidate_id"),
        Index("ix_ats_todos_ats_job_id", "ats_job_id"),
        Index("ix_ats_todos_contact_id", "contact_id"),
    )

    @property
    def is_overdue(self) -> bool:
        """Prueft, ob die Aufgabe ueberfaellig ist."""
        if self.due_date and self.status in (TodoStatus.OPEN, TodoStatus.IN_PROGRESS):
            return self.due_date < date.today()
        return False

    @property
    def status_label(self) -> str:
        """Gibt das deutsche Label des Status zurueck."""
        return TODO_STATUS_LABELS.get(self.status, self.status.value)

    @property
    def priority_label(self) -> str:
        """Gibt das deutsche Label der Prioritaet zurueck."""
        return TODO_PRIORITY_LABELS.get(self.priority, self.priority.value)

    @property
    def priority_color(self) -> str:
        """Gibt die Farbe der Prioritaet zurueck."""
        return TODO_PRIORITY_COLORS.get(self.priority, "gray")
