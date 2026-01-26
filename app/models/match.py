"""Match Model - Verknüpfung zwischen Jobs und Kandidaten."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    ARRAY,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class MatchStatus(str, enum.Enum):
    """Status eines Matches."""

    NEW = "new"
    AI_CHECKED = "ai_checked"
    PRESENTED = "presented"
    REJECTED = "rejected"
    PLACED = "placed"


class Match(Base):
    """Model für Job-Kandidaten-Matches."""

    __tablename__ = "matches"

    # Primärschlüssel
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # Foreign Keys
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("candidates.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Basis-Matching-Daten
    distance_km: Mapped[float | None] = mapped_column(Float)
    keyword_score: Mapped[float | None] = mapped_column(Float)
    matched_keywords: Mapped[list[str] | None] = mapped_column(ARRAY(String))

    # KI-Bewertung
    ai_score: Mapped[float | None] = mapped_column(Float)
    ai_explanation: Mapped[str | None] = mapped_column(Text)
    ai_strengths: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    ai_weaknesses: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    ai_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Status
    status: Mapped[MatchStatus] = mapped_column(
        Enum(MatchStatus, values_callable=lambda x: [e.value for e in x]),
        default=MatchStatus.NEW,
    )

    # Vermittlung
    placed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    placed_notes: Mapped[str | None] = mapped_column(Text)

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
    job: Mapped["Job"] = relationship("Job", back_populates="matches")
    candidate: Mapped["Candidate"] = relationship("Candidate", back_populates="matches")

    # Constraints und Indizes
    __table_args__ = (
        UniqueConstraint("job_id", "candidate_id", name="uq_match_job_candidate"),
        Index("ix_matches_job_id", "job_id"),
        Index("ix_matches_candidate_id", "candidate_id"),
        Index("ix_matches_status", "status"),
        Index("ix_matches_ai_score", "ai_score"),
        Index("ix_matches_distance_km", "distance_km"),
        Index("ix_matches_created_at", "created_at"),
    )

    @property
    def is_ai_checked(self) -> bool:
        """Prüft, ob eine KI-Bewertung vorliegt."""
        return self.ai_checked_at is not None

    @property
    def is_excellent(self) -> bool:
        """Prüft, ob es ein exzellentes Match ist (≤5km und ≥3 Keywords)."""
        distance_ok = self.distance_km is not None and self.distance_km <= 5
        keywords_ok = self.matched_keywords is not None and len(self.matched_keywords) >= 3
        return distance_ok and keywords_ok
