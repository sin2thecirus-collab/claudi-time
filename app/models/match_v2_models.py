"""Matching Engine v2 — Neue Models für strukturiertes Matching + Learning.

Tabellen:
- match_v2_training_data: Feedback-Daten für ML-Training
- match_v2_learned_rules: Entdeckte Muster (Association Rules, Decision Trees)
- match_v2_scoring_weights: Lernbare Gewichte für Score-Komponenten
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class MatchV2TrainingData(Base):
    """Training-Daten aus Recruiter-Feedback für ML-Modelle."""

    __tablename__ = "match_v2_training_data"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # Referenzen
    match_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("matches.id", ondelete="SET NULL"), nullable=True
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    # Feature-Snapshot zum Zeitpunkt des Feedbacks
    # {skill_overlap, seniority_gap, career_traj, embedding_sim, distance_km, software_match, ...}
    features: Mapped[dict | None] = mapped_column(JSONB)

    # Ergebnis
    outcome: Mapped[str | None] = mapped_column(String(20))  # "good" / "bad" / "neutral"
    outcome_source: Mapped[str | None] = mapped_column(String(20))  # "user_feedback" / "placed" / "rejected"

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index("ix_v2_training_match_id", "match_id"),
        Index("ix_v2_training_outcome", "outcome"),
        Index("ix_v2_training_created_at", "created_at"),
    )


class MatchV2LearnedRule(Base):
    """Automatisch entdeckte Matching-Regeln (Association Rules, Decision Trees, etc.)."""

    __tablename__ = "match_v2_learned_rules"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # Regel-Typ: "association" / "decision_tree" / "weight_override" / "exclusion"
    rule_type: Mapped[str] = mapped_column(String(30), nullable=False)

    # Regel als JSON
    # Beispiel: {"if": {"has_HGB": true, "seniority_gap": "<=1"}, "then": "good_match", "boost": 10}
    rule_json: Mapped[dict] = mapped_column(JSONB, nullable=False)

    # Qualität
    confidence: Mapped[float | None] = mapped_column(Float)  # 0.0-1.0
    support_count: Mapped[int | None] = mapped_column(Integer)  # Anzahl stützender Beispiele

    # Status
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_v2_rules_type", "rule_type"),
        Index("ix_v2_rules_active", "active"),
        Index("ix_v2_rules_confidence", "confidence"),
    )


class MatchV2ScoringWeight(Base):
    """Lernbare Gewichte für die Score-Komponenten der Matching Engine v2."""

    __tablename__ = "match_v2_scoring_weights"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # Welche Komponente: "skill_overlap", "seniority_fit", "embedding_sim", etc.
    component: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)

    # Gewichte
    weight: Mapped[float] = mapped_column(Float, nullable=False)  # Aktuelles Gewicht
    default_weight: Mapped[float] = mapped_column(Float, nullable=False)  # Ursprungswert

    # Tracking
    adjustment_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_adjusted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_v2_weights_component", "component"),
    )
