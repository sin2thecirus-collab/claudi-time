"""Statistics Models - Tagesstatistiken und Filter-Nutzung."""

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class DailyStatistics(Base):
    """Model für tägliche Statistiken."""

    __tablename__ = "daily_statistics"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # Datum (einzigartig pro Tag)
    date: Mapped[date] = mapped_column(Date, unique=True, nullable=False)

    # Zählungen
    jobs_total: Mapped[int] = mapped_column(Integer, default=0)
    jobs_active: Mapped[int] = mapped_column(Integer, default=0)
    candidates_total: Mapped[int] = mapped_column(Integer, default=0)
    candidates_active: Mapped[int] = mapped_column(Integer, default=0)
    matches_total: Mapped[int] = mapped_column(Integer, default=0)

    # KI-Nutzung
    ai_checks_count: Mapped[int] = mapped_column(Integer, default=0)
    ai_checks_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)

    # Vermittlungen
    matches_presented: Mapped[int] = mapped_column(Integer, default=0)
    matches_placed: Mapped[int] = mapped_column(Integer, default=0)

    # Durchschnittswerte
    avg_ai_score: Mapped[float | None] = mapped_column(Float)
    avg_distance_km: Mapped[float | None] = mapped_column(Float)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    __table_args__ = (Index("ix_daily_statistics_date", "date"),)


class FilterUsage(Base):
    """Model für Filter-Nutzungsstatistiken."""

    __tablename__ = "filter_usage"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    filter_type: Mapped[str] = mapped_column(String(50), nullable=False)
    filter_value: Mapped[str] = mapped_column(String(255), nullable=False)

    used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    __table_args__ = (
        Index("ix_filter_usage_filter_type", "filter_type"),
        Index("ix_filter_usage_used_at", "used_at"),
    )
