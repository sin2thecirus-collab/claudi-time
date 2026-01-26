"""Alert Model - Benachrichtigungen für Recruiter."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AlertType(str, enum.Enum):
    """Typen von Alerts."""

    EXCELLENT_MATCH = "excellent_match"
    EXPIRING_JOB = "expiring_job"
    SYNC_ERROR = "sync_error"
    IMPORT_COMPLETE = "import_complete"
    SYSTEM = "system"


class AlertPriority(str, enum.Enum):
    """Prioritätsstufen für Alerts."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Alert(Base):
    """Model für System-Benachrichtigungen."""

    __tablename__ = "alerts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # Alert-Daten
    alert_type: Mapped[AlertType] = mapped_column(Enum(AlertType), nullable=False)
    priority: Mapped[AlertPriority] = mapped_column(
        Enum(AlertPriority),
        default=AlertPriority.MEDIUM,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)

    # Optionale Referenzen
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="SET NULL"),
    )
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("candidates.id", ondelete="SET NULL"),
    )
    match_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("matches.id", ondelete="SET NULL"),
    )

    # Status
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    is_dismissed: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    __table_args__ = (
        Index("ix_alerts_alert_type", "alert_type"),
        Index("ix_alerts_priority", "priority"),
        Index("ix_alerts_is_read", "is_read"),
        Index("ix_alerts_is_dismissed", "is_dismissed"),
        Index("ix_alerts_created_at", "created_at"),
    )

    @property
    def is_active(self) -> bool:
        """Prüft, ob der Alert noch aktiv ist (ungelesen und nicht abgewiesen)."""
        return not self.is_read and not self.is_dismissed
