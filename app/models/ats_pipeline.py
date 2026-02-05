"""ATSPipelineEntry Model - Kanban-Pipeline-Eintraege (Kandidat in Stelle)."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class PipelineStage(str, enum.Enum):
    """Stages der ATS-Pipeline."""

    MATCHED = "matched"
    SENT = "sent"
    FEEDBACK = "feedback"
    INTERVIEW_1 = "interview_1"
    INTERVIEW_2 = "interview_2"
    INTERVIEW_3 = "interview_3"
    OFFER = "offer"
    PLACED = "placed"
    REJECTED = "rejected"


# Reihenfolge fuer UI-Anzeige (ohne REJECTED, das ist ein Sonderstatus)
PIPELINE_STAGE_ORDER = [
    PipelineStage.MATCHED,
    PipelineStage.SENT,
    PipelineStage.FEEDBACK,
    PipelineStage.INTERVIEW_1,
    PipelineStage.INTERVIEW_2,
    PipelineStage.INTERVIEW_3,
    PipelineStage.OFFER,
    PipelineStage.PLACED,
]

# Deutsche Labels fuer UI
PIPELINE_STAGE_LABELS = {
    PipelineStage.MATCHED: "Gematcht",
    PipelineStage.SENT: "Vorgestellt",
    PipelineStage.FEEDBACK: "Feedback",
    PipelineStage.INTERVIEW_1: "Interview 1",
    PipelineStage.INTERVIEW_2: "Interview 2",
    PipelineStage.INTERVIEW_3: "Interview 3",
    PipelineStage.OFFER: "Angebot",
    PipelineStage.PLACED: "Besetzt",
    PipelineStage.REJECTED: "Abgelehnt",
}

# Farben fuer UI-Badges
PIPELINE_STAGE_COLORS = {
    PipelineStage.MATCHED: "blue",
    PipelineStage.SENT: "indigo",
    PipelineStage.FEEDBACK: "amber",
    PipelineStage.INTERVIEW_1: "purple",
    PipelineStage.INTERVIEW_2: "purple",
    PipelineStage.INTERVIEW_3: "purple",
    PipelineStage.OFFER: "emerald",
    PipelineStage.PLACED: "green",
    PipelineStage.REJECTED: "red",
}


class ATSPipelineEntry(Base):
    """Model fuer einen Kandidaten in einer ATS-Pipeline."""

    __tablename__ = "ats_pipeline_entries"

    # Primaerschluessel
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # Foreign Keys
    ats_job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ats_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("candidates.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Pipeline-Status
    stage: Mapped[PipelineStage] = mapped_column(
        Enum(PipelineStage, values_callable=lambda x: [e.value for e in x]),
        default=PipelineStage.MATCHED,
    )
    stage_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    # Details
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

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
    ats_job: Mapped["ATSJob"] = relationship(
        "ATSJob", back_populates="pipeline_entries",
    )
    candidate: Mapped["Candidate | None"] = relationship("Candidate")
    activities: Mapped[list["ATSActivity"]] = relationship(
        "ATSActivity",
        back_populates="pipeline_entry",
        cascade="all, delete-orphan",
    )

    # Constraints und Indizes
    __table_args__ = (
        UniqueConstraint("ats_job_id", "candidate_id", name="uq_ats_pipeline_job_candidate"),
        Index("ix_ats_pipeline_ats_job_id", "ats_job_id"),
        Index("ix_ats_pipeline_candidate_id", "candidate_id"),
        Index("ix_ats_pipeline_stage", "stage"),
        Index("ix_ats_pipeline_created_at", "created_at"),
    )

    @property
    def stage_label(self) -> str:
        """Gibt das deutsche Label des aktuellen Stages zurueck."""
        return PIPELINE_STAGE_LABELS.get(self.stage, self.stage.value)

    @property
    def stage_color(self) -> str:
        """Gibt die Farbe des aktuellen Stages zurueck."""
        return PIPELINE_STAGE_COLORS.get(self.stage, "gray")
