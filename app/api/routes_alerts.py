"""Alerts API Routes - Endpoints für System-Benachrichtigungen."""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.exception_handlers import NotFoundException
from app.api.rate_limiter import RateLimitTier, rate_limit
from app.database import get_db
from app.models.alert import AlertPriority, AlertType
from app.services.alert_service import AlertService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/alerts", tags=["Alerts"])


# ==================== Response Schemas ====================


class AlertResponse(BaseModel):
    """Schema für Alert-Response."""

    id: str
    alert_type: str
    priority: str
    title: str
    message: str
    job_id: str | None = None
    candidate_id: str | None = None
    match_id: str | None = None
    is_read: bool
    is_dismissed: bool
    created_at: str

    model_config = {"from_attributes": True}


class AlertListResponse(BaseModel):
    """Schema für paginierte Alert-Listen."""

    items: list[AlertResponse]
    total: int
    page: int
    per_page: int


class AlertCheckResult(BaseModel):
    """Schema für Alert-Check-Ergebnis."""

    excellent_matches: int = Field(description="Erstellte Alerts für exzellente Matches")
    expiring_jobs: int = Field(description="Erstellte Alerts für ablaufende Jobs")
    cleaned_up: int = Field(description="Gelöschte alte Alerts")


# ==================== Endpoints ====================


@router.get(
    "",
    response_model=AlertListResponse,
    summary="Alle Alerts",
    description="Gibt alle Alerts zurück (paginiert)",
)
@rate_limit(RateLimitTier.STANDARD)
async def get_alerts(
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=20, ge=1, le=100),
    include_dismissed: bool = Query(default=False),
    db: AsyncSession = Depends(get_db),
) -> AlertListResponse:
    """
    Gibt alle Alerts zurück.

    - **page**: Seitennummer (Standard: 1)
    - **per_page**: Einträge pro Seite (Standard: 20)
    - **include_dismissed**: Auch verworfene Alerts anzeigen (Standard: False)
    """
    alert_service = AlertService(db)
    alerts, total = await alert_service.get_all_alerts(
        include_dismissed=include_dismissed,
        limit=per_page,
        offset=(page - 1) * per_page,
    )

    return AlertListResponse(
        items=[_alert_to_response(alert) for alert in alerts],
        total=total,
        page=page,
        per_page=per_page,
    )


@router.get(
    "/active",
    response_model=list[AlertResponse],
    summary="Aktive Alerts",
    description="Gibt nur aktive (ungelesene, nicht verworfene) Alerts zurück",
)
@rate_limit(RateLimitTier.STANDARD)
async def get_active_alerts(
    limit: int = Query(default=10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
) -> list[AlertResponse]:
    """
    Gibt nur aktive Alerts zurück.

    Aktive Alerts sind:
    - Nicht als gelesen markiert
    - Nicht verworfen

    Sortiert nach Priorität (HIGH > MEDIUM > LOW) und Datum.
    """
    alert_service = AlertService(db)
    alerts = await alert_service.get_active_alerts(limit=limit)

    return [_alert_to_response(alert) for alert in alerts]


@router.get(
    "/{alert_id}",
    response_model=AlertResponse,
    summary="Alert-Details",
)
@rate_limit(RateLimitTier.STANDARD)
async def get_alert(
    alert_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> AlertResponse:
    """Gibt die Details eines Alerts zurück."""
    alert_service = AlertService(db)
    alert = await alert_service.get_alert(alert_id)

    if not alert:
        raise NotFoundException(message="Alert nicht gefunden")

    return _alert_to_response(alert)


@router.put(
    "/{alert_id}/read",
    response_model=AlertResponse,
    summary="Als gelesen markieren",
)
@rate_limit(RateLimitTier.WRITE)
async def mark_alert_as_read(
    alert_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> AlertResponse:
    """Markiert einen Alert als gelesen."""
    alert_service = AlertService(db)
    alert = await alert_service.mark_as_read(alert_id)

    if not alert:
        raise NotFoundException(message="Alert nicht gefunden")

    return _alert_to_response(alert)


@router.put(
    "/{alert_id}/dismiss",
    response_model=AlertResponse,
    summary="Alert verwerfen",
)
@rate_limit(RateLimitTier.WRITE)
async def dismiss_alert(
    alert_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> AlertResponse:
    """
    Verwirft einen Alert.

    Verworfene Alerts werden nicht mehr in der aktiven Liste angezeigt.
    """
    alert_service = AlertService(db)
    alert = await alert_service.dismiss(alert_id)

    if not alert:
        raise NotFoundException(message="Alert nicht gefunden")

    return _alert_to_response(alert)


@router.put(
    "/dismiss-all",
    summary="Alle Alerts verwerfen",
)
@rate_limit(RateLimitTier.WRITE)
async def dismiss_all_alerts(
    db: AsyncSession = Depends(get_db),
) -> dict[str, int]:
    """Verwirft alle aktiven Alerts."""
    alert_service = AlertService(db)
    count = await alert_service.dismiss_all()

    return {"dismissed_count": count}


@router.put(
    "/read-all",
    summary="Alle als gelesen markieren",
)
@rate_limit(RateLimitTier.WRITE)
async def mark_all_as_read(
    db: AsyncSession = Depends(get_db),
) -> dict[str, int]:
    """Markiert alle aktiven Alerts als gelesen."""
    alert_service = AlertService(db)
    count = await alert_service.mark_all_as_read()

    return {"marked_count": count}


# ==================== Admin-Endpunkte ====================


@router.post(
    "/check",
    response_model=AlertCheckResult,
    summary="Alert-Checks ausführen",
    description="Führt alle automatischen Alert-Checks aus",
)
@rate_limit(RateLimitTier.ADMIN)
async def run_alert_checks(
    db: AsyncSession = Depends(get_db),
) -> AlertCheckResult:
    """
    Führt alle automatischen Alert-Checks aus.

    Wird normalerweise vom nächtlichen Cron-Job aufgerufen.

    Prüft auf:
    - Exzellente Matches (≤5km, ≥3 Keywords)
    - Ablaufende Jobs (in den nächsten 7 Tagen)

    Bereinigt außerdem alte, verworfene Alerts (>30 Tage).
    """
    alert_service = AlertService(db)
    result = await alert_service.run_all_checks()

    return AlertCheckResult(**result)


@router.post(
    "/cleanup",
    summary="Alte Alerts bereinigen",
)
@rate_limit(RateLimitTier.ADMIN)
async def cleanup_old_alerts(
    days: int = Query(default=30, ge=7, le=365),
    db: AsyncSession = Depends(get_db),
) -> dict[str, int]:
    """
    Löscht alte, verworfene Alerts.

    - **days**: Alter in Tagen (Standard: 30)
    """
    alert_service = AlertService(db)
    count = await alert_service.cleanup_old_alerts(days=days)

    return {"deleted_count": count}


# ==================== Hilfsfunktionen ====================


def _alert_to_response(alert) -> AlertResponse:
    """Konvertiert ein Alert-Model zu einem Response-Schema."""
    return AlertResponse(
        id=str(alert.id),
        alert_type=alert.alert_type.value,
        priority=alert.priority.value,
        title=alert.title,
        message=alert.message,
        job_id=str(alert.job_id) if alert.job_id else None,
        candidate_id=str(alert.candidate_id) if alert.candidate_id else None,
        match_id=str(alert.match_id) if alert.match_id else None,
        is_read=alert.is_read,
        is_dismissed=alert.is_dismissed,
        created_at=alert.created_at.isoformat() if alert.created_at else None,
    )
