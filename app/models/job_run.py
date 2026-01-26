"""JobRun Model - Tracking von Background-Jobs."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class JobType(str, enum.Enum):
    """Typen von Background-Jobs."""

    GEOCODING = "geocoding"
    CRM_SYNC = "crm_sync"
    MATCHING = "matching"
    CLEANUP = "cleanup"
    CV_PARSING = "cv_parsing"


class JobRunStatus(str, enum.Enum):
    """Status eines Job-Runs."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobSource(str, enum.Enum):
    """Quelle, die den Job ausgelöst hat."""

    MANUAL = "manual"
    CRON = "cron"
    SYSTEM = "system"


class JobRun(Base):
    """Model für Background-Job-Tracking."""

    __tablename__ = "job_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # Job-Typ und Quelle
    job_type: Mapped[JobType] = mapped_column(Enum(JobType), nullable=False)
    source: Mapped[JobSource] = mapped_column(
        Enum(JobSource),
        default=JobSource.MANUAL,
    )

    # Status
    status: Mapped[JobRunStatus] = mapped_column(
        Enum(JobRunStatus),
        default=JobRunStatus.PENDING,
    )

    # Fortschritt
    items_total: Mapped[int] = mapped_column(Integer, default=0)
    items_processed: Mapped[int] = mapped_column(Integer, default=0)
    items_successful: Mapped[int] = mapped_column(Integer, default=0)
    items_failed: Mapped[int] = mapped_column(Integer, default=0)

    # Fehler-Details
    error_message: Mapped[str | None] = mapped_column(Text)
    errors_detail: Mapped[dict | None] = mapped_column(JSONB)

    # Timestamps
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    __table_args__ = (
        Index("ix_job_runs_job_type", "job_type"),
        Index("ix_job_runs_status", "status"),
        Index("ix_job_runs_source", "source"),
        Index("ix_job_runs_created_at", "created_at"),
    )

    @property
    def progress_percent(self) -> float:
        """Berechnet den Fortschritt in Prozent."""
        if self.items_total == 0:
            return 0.0
        return (self.items_processed / self.items_total) * 100

    @property
    def is_running(self) -> bool:
        """Prüft, ob der Job noch läuft."""
        return self.status in (JobRunStatus.PENDING, JobRunStatus.RUNNING)

    @property
    def duration_seconds(self) -> float | None:
        """Berechnet die Laufzeit in Sekunden."""
        if not self.started_at:
            return None
        end_time = self.completed_at or datetime.now(self.started_at.tzinfo)
        return (end_time - self.started_at).total_seconds()
