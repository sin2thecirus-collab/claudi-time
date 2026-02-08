"""Match Model - Verknüpfung zwischen Jobs und Kandidaten."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    ARRAY,
    Boolean,
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
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class MatchStatus(str, enum.Enum):
    """Status eines Matches."""

    NEW = "new"
    AI_CHECKED = "ai_checked"
    PRESENTED = "presented"
    REJECTED = "rejected"
    PLACED = "placed"


class MatchingMethod(str, enum.Enum):
    """Welches System hat diesen Match erstellt."""

    PRE_MATCH = "pre_match"
    DEEP_MATCH = "deep_match"
    SMART_MATCH = "smart_match"
    MANUAL = "manual"


class Match(Base):
    """Model für Job-Kandidaten-Matches."""

    __tablename__ = "matches"

    # Primärschlüssel
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # Foreign Keys (SET NULL: Matches bleiben erhalten wenn Jobs/Kandidaten geloescht werden)
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="SET NULL"),
        nullable=True,
    )
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("candidates.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Matching-Methode (welches System hat diesen Match erstellt)
    matching_method: Mapped[str | None] = mapped_column(String(50))

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

    # Pre-Scoring & DeepMatch
    pre_score: Mapped[float | None] = mapped_column(Float)
    user_feedback: Mapped[str | None] = mapped_column(String(50))
    feedback_note: Mapped[str | None] = mapped_column(Text)
    feedback_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Quick-AI Score (Phase C — guenstige KI-Schnellbewertung)
    quick_score: Mapped[int | None] = mapped_column(Float)  # 0-100
    quick_reason: Mapped[str | None] = mapped_column(String(200))  # 1 Satz
    quick_scored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Status
    status: Mapped[MatchStatus] = mapped_column(
        Enum(MatchStatus, values_callable=lambda x: [e.value for e in x]),
        default=MatchStatus.NEW,
    )

    # ── Matching Engine v2: Score ──
    v2_score: Mapped[float | None] = mapped_column(Float)  # Neuer Matching-Score (0-100)
    v2_score_breakdown: Mapped[dict | None] = mapped_column(JSONB)  # {skill_overlap, seniority_fit, embedding_sim, ...}
    v2_matched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Stale-Tracking (Match ist veraltet weil sich Kandidaten-Daten geaendert haben)
    stale: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    stale_reason: Mapped[str | None] = mapped_column(String(255))
    stale_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

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

    # Relationships (Optional weil SET NULL bei Loeschung)
    job: Mapped["Job | None"] = relationship("Job", back_populates="matches")
    candidate: Mapped["Candidate | None"] = relationship("Candidate", back_populates="matches")

    # Constraints und Indizes
    __table_args__ = (
        UniqueConstraint("job_id", "candidate_id", name="uq_match_job_candidate"),
        Index("ix_matches_job_id", "job_id"),
        Index("ix_matches_candidate_id", "candidate_id"),
        Index("ix_matches_status", "status"),
        Index("ix_matches_ai_score", "ai_score"),
        Index("ix_matches_distance_km", "distance_km"),
        Index("ix_matches_created_at", "created_at"),
        Index("ix_matches_pre_score", "pre_score"),
        Index("ix_matches_stale", "stale"),
        Index("ix_matches_matching_method", "matching_method"),
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
