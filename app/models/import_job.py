"""ImportJob Model - CSV-Import-Tracking."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ImportStatus(str, enum.Enum):
    """Status eines Import-Jobs."""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ImportJob(Base):
    """Model für CSV-Import-Tracking."""

    __tablename__ = "import_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # Datei-Info
    filename: Mapped[str] = mapped_column(String(255), nullable=False)

    # Fortschritt
    total_rows: Mapped[int] = mapped_column(Integer, default=0)
    processed_rows: Mapped[int] = mapped_column(Integer, default=0)
    successful_rows: Mapped[int] = mapped_column(Integer, default=0)
    failed_rows: Mapped[int] = mapped_column(Integer, default=0)

    # Status
    status: Mapped[ImportStatus] = mapped_column(
        Enum(ImportStatus),
        default=ImportStatus.PENDING,
    )

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
        Index("ix_import_jobs_status", "status"),
        Index("ix_import_jobs_created_at", "created_at"),
    )

    @property
    def progress_percent(self) -> float:
        """Berechnet den Fortschritt in Prozent."""
        if self.total_rows == 0:
            return 0.0
        return (self.processed_rows / self.total_rows) * 100

    @property
    def is_complete(self) -> bool:
        """Prüft, ob der Import abgeschlossen ist."""
        return self.status in (
            ImportStatus.COMPLETED,
            ImportStatus.FAILED,
            ImportStatus.CANCELLED,
        )
