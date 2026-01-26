"""Statistics API Routes - Endpoints für Statistiken und Auswertungen."""

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.rate_limiter import RateLimitTier, rate_limit
from app.database import get_db
from app.services.statistics_service import StatisticsService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/statistics", tags=["Statistics"])


# ==================== Response Schemas ====================


class TopFilterResponse(BaseModel):
    """Schema für Top-Filter."""

    filter_type: str
    filter_value: str
    usage_count: int


class DashboardStatsResponse(BaseModel):
    """Schema für Dashboard-Statistiken."""

    # Aktuelle Zählungen
    jobs_active: int = Field(description="Aktive Jobs (nicht gelöscht, nicht abgelaufen)")
    candidates_active: int = Field(description="Aktive Kandidaten (letzte 30 Tage)")
    candidates_total: int = Field(description="Kandidaten gesamt")
    matches_total: int = Field(description="Matches gesamt")

    # KI-Nutzung (Zeitraum)
    ai_checks_count: int = Field(description="KI-Checks im Zeitraum")
    ai_checks_cost_usd: float = Field(description="KI-Kosten in USD im Zeitraum")

    # Vermittlungen (Zeitraum)
    matches_presented: int = Field(description="Vorgestellte Kandidaten im Zeitraum")
    matches_placed: int = Field(description="Vermittlungen im Zeitraum")

    # Durchschnittswerte
    avg_ai_score: float | None = Field(description="Durchschnittlicher KI-Score")
    avg_distance_km: float | None = Field(description="Durchschnittliche Distanz in km")

    # Top-Filter
    top_filters: list[TopFilterResponse] = Field(description="Meistgenutzte Filter")

    # Probleme
    jobs_without_matches: int = Field(description="Jobs ohne Matches")
    candidates_without_address: int = Field(description="Kandidaten ohne gültige Adresse")


class JobWithoutMatchesResponse(BaseModel):
    """Schema für Jobs ohne Matches."""

    id: str
    position: str
    company_name: str | None
    city: str | None
    created_at: str


class CandidateWithoutAddressResponse(BaseModel):
    """Schema für Kandidaten ohne Adresse."""

    id: str
    full_name: str
    city: str | None
    created_at: str


# ==================== Endpoints ====================


@router.get("/jobs-count", summary="Anzahl aktiver Jobs")
async def get_jobs_count(db: AsyncSession = Depends(get_db)) -> dict:
    """Gibt die Anzahl aktiver Jobs zurück."""
    stats_service = StatisticsService(db)
    stats = await stats_service.get_dashboard_stats(days=30)
    return {"count": stats.jobs_active}


@router.get("/candidates-active-count", summary="Anzahl aktiver Kandidaten")
async def get_candidates_active_count(db: AsyncSession = Depends(get_db)) -> dict:
    """Gibt die Anzahl aktiver Kandidaten zurück."""
    stats_service = StatisticsService(db)
    stats = await stats_service.get_dashboard_stats(days=30)
    return {"count": stats.candidates_active}


@router.get("/ai-checks-count", summary="Anzahl KI-Checks")
async def get_ai_checks_count(db: AsyncSession = Depends(get_db)) -> dict:
    """Gibt die Anzahl der KI-Checks der letzten 30 Tage zurück."""
    stats_service = StatisticsService(db)
    stats = await stats_service.get_dashboard_stats(days=30)
    return {"count": stats.ai_checks_count}


@router.get("/placed-count", summary="Anzahl Vermittlungen")
async def get_placed_count(db: AsyncSession = Depends(get_db)) -> dict:
    """Gibt die Anzahl der Vermittlungen der letzten 30 Tage zurück."""
    stats_service = StatisticsService(db)
    stats = await stats_service.get_dashboard_stats(days=30)
    return {"count": stats.matches_placed}


@router.get(
    "/dashboard",
    response_model=DashboardStatsResponse,
    summary="Dashboard-Statistiken",
    description="Gibt aggregierte Statistiken für das Dashboard zurück",
)
@rate_limit(RateLimitTier.STANDARD)
async def get_dashboard_stats(
    days: int = Query(default=30, ge=1, le=365, description="Zeitraum in Tagen"),
    db: AsyncSession = Depends(get_db),
) -> DashboardStatsResponse:
    """
    Gibt aggregierte Statistiken für das Dashboard zurück.

    - **days**: Zeitraum für zeitraumbezogene Statistiken (Standard: 30 Tage)

    Enthält:
    - Aktive Jobs und Kandidaten
    - KI-Check-Statistiken
    - Vermittlungs-Statistiken
    - Meistgenutzte Filter
    - Erkannte Probleme
    """
    stats_service = StatisticsService(db)
    stats = await stats_service.get_dashboard_stats(days=days)

    return DashboardStatsResponse(
        jobs_active=stats.jobs_active,
        candidates_active=stats.candidates_active,
        candidates_total=stats.candidates_total,
        matches_total=stats.matches_total,
        ai_checks_count=stats.ai_checks_count,
        ai_checks_cost_usd=stats.ai_checks_cost_usd,
        matches_presented=stats.matches_presented,
        matches_placed=stats.matches_placed,
        avg_ai_score=stats.avg_ai_score,
        avg_distance_km=stats.avg_distance_km,
        top_filters=[
            TopFilterResponse(
                filter_type=f.filter_type,
                filter_value=f.filter_value,
                usage_count=f.usage_count,
            )
            for f in stats.top_filters
        ],
        jobs_without_matches=stats.jobs_without_matches,
        candidates_without_address=stats.candidates_without_address,
    )


@router.get(
    "/jobs-without-matches",
    summary="Jobs ohne Matches",
    description="Gibt Jobs zurück, die keine passenden Kandidaten haben",
)
@rate_limit(RateLimitTier.STANDARD)
async def get_jobs_without_matches(
    limit: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Gibt Jobs ohne Matches zurück.

    Diese Jobs haben keine Kandidaten im 25km-Radius oder haben
    noch keine Matches berechnet.
    """
    stats_service = StatisticsService(db)
    jobs = await stats_service.get_jobs_without_matches(limit=limit)

    return {
        "items": [
            {
                "id": str(job.id),
                "position": job.position,
                "company_name": job.company_name,
                "city": job.work_location_city or job.city,
                "created_at": job.created_at.isoformat() if job.created_at else None,
            }
            for job in jobs
        ],
        "total": len(jobs),
    }


@router.get(
    "/candidates-without-address",
    summary="Kandidaten ohne Adresse",
    description="Gibt Kandidaten zurück, die keine gültige Adresse haben",
)
@rate_limit(RateLimitTier.STANDARD)
async def get_candidates_without_address(
    limit: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Gibt Kandidaten ohne gültige Adresse zurück.

    Diese Kandidaten können nicht für Matches berücksichtigt werden,
    da ihre Koordinaten nicht bekannt sind.
    """
    stats_service = StatisticsService(db)
    candidates = await stats_service.get_candidates_without_address(limit=limit)

    return {
        "items": [
            {
                "id": str(candidate.id),
                "full_name": candidate.full_name,
                "city": candidate.city,
                "created_at": candidate.created_at.isoformat() if candidate.created_at else None,
            }
            for candidate in candidates
        ],
        "total": len(candidates),
    }


@router.post(
    "/aggregate",
    summary="Tägliche Aggregation starten",
    description="Startet die tägliche Statistik-Aggregation manuell",
)
@rate_limit(RateLimitTier.ADMIN)
async def trigger_daily_aggregation(
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """
    Startet die tägliche Statistik-Aggregation manuell.

    Wird normalerweise automatisch vom nächtlichen Cron-Job aufgerufen.
    """
    stats_service = StatisticsService(db)
    await stats_service.aggregate_daily_stats()

    return {"message": "Tägliche Statistiken wurden aggregiert"}
