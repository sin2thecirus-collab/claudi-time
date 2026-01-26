"""Settings API Routes - Endpoints für Prio-Städte und Einstellungen."""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.exception_handlers import NotFoundException, ConflictException
from app.api.rate_limiter import RateLimitTier, rate_limit
from app.config import Limits
from app.database import get_db
from app.services.filter_service import FilterService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings", tags=["Einstellungen"])


# ==================== Schemas ====================

class PriorityCityResponse(BaseModel):
    """Schema für Prio-Stadt Response."""
    id: UUID
    city_name: str
    priority_order: int

    model_config = {"from_attributes": True}


class PriorityCityCreate(BaseModel):
    """Schema für neue Prio-Stadt."""
    city_name: str = Field(min_length=2, max_length=100)
    priority_order: int | None = Field(default=None, ge=0)


class PriorityCityUpdate(BaseModel):
    """Schema für Prio-Städte Update (Liste)."""
    cities: list[dict] = Field(
        description="Liste von {city_name, priority_order}",
        max_length=Limits.PRIORITY_CITIES_MAX,
    )


# ==================== Prio-Städte ====================

@router.get(
    "/priority-cities",
    response_model=list[PriorityCityResponse],
    summary="Prio-Städte auflisten",
    description="Gibt die priorisierten Städte zurück (werden oben angezeigt)",
)
async def list_priority_cities(
    db: AsyncSession = Depends(get_db),
):
    """
    Listet alle Prio-Städte auf.

    Jobs in diesen Städten werden in Listen immer zuerst angezeigt.
    Standard: Hamburg, München
    """
    filter_service = FilterService(db)
    cities = await filter_service.get_priority_cities()

    return [PriorityCityResponse.model_validate(c) for c in cities]


@router.post(
    "/priority-cities",
    response_model=PriorityCityResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Prio-Stadt hinzufügen",
)
@rate_limit(RateLimitTier.WRITE)
async def add_priority_city(
    data: PriorityCityCreate,
    db: AsyncSession = Depends(get_db),
):
    """
    Fügt eine neue Prio-Stadt hinzu.

    Maximal 10 Prio-Städte erlaubt.
    """
    filter_service = FilterService(db)

    # Prüfe Limit
    existing = await filter_service.get_priority_cities()
    if len(existing) >= Limits.PRIORITY_CITIES_MAX:
        raise ConflictException(
            message=f"Maximal {Limits.PRIORITY_CITIES_MAX} Prio-Städte erlaubt"
        )

    # Prüfe auf Duplikat
    for city in existing:
        if city.city_name.lower() == data.city_name.lower():
            raise ConflictException(message="Diese Stadt ist bereits priorisiert")

    city = await filter_service.add_priority_city(
        city_name=data.city_name,
        priority_order=data.priority_order,
    )

    return PriorityCityResponse.model_validate(city)


@router.put(
    "/priority-cities",
    response_model=list[PriorityCityResponse],
    summary="Prio-Städte aktualisieren",
)
@rate_limit(RateLimitTier.WRITE)
async def update_priority_cities(
    data: PriorityCityUpdate,
    db: AsyncSession = Depends(get_db),
):
    """
    Ersetzt alle Prio-Städte.

    Ermöglicht das Neuordnen und Ändern aller Prio-Städte auf einmal.
    """
    if len(data.cities) > Limits.PRIORITY_CITIES_MAX:
        raise ConflictException(
            message=f"Maximal {Limits.PRIORITY_CITIES_MAX} Prio-Städte erlaubt"
        )

    filter_service = FilterService(db)
    cities = await filter_service.update_priority_cities(data.cities)

    return [PriorityCityResponse.model_validate(c) for c in cities]


@router.delete(
    "/priority-cities/{city_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Prio-Stadt entfernen",
)
@rate_limit(RateLimitTier.WRITE)
async def remove_priority_city(
    city_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Entfernt eine Stadt aus den Prio-Städten."""
    filter_service = FilterService(db)
    success = await filter_service.remove_priority_city(city_id)

    if not success:
        raise NotFoundException(message="Prio-Stadt nicht gefunden")


# ==================== System-Info ====================

@router.get(
    "/limits",
    summary="System-Limits anzeigen",
)
async def get_system_limits():
    """
    Gibt die konfigurierten System-Limits zurück.

    Diese Werte werden für Validierung und UI-Hinweise verwendet.
    """
    return {
        "csv_max_file_size_mb": Limits.CSV_MAX_FILE_SIZE_MB,
        "csv_max_rows": Limits.CSV_MAX_ROWS,
        "batch_delete_max": Limits.BATCH_DELETE_MAX,
        "batch_hide_max": Limits.BATCH_HIDE_MAX,
        "ai_check_max_candidates": Limits.AI_CHECK_MAX_CANDIDATES,
        "filter_multi_select_max": Limits.FILTER_MULTI_SELECT_MAX,
        "filter_presets_max": Limits.FILTER_PRESETS_MAX,
        "priority_cities_max": Limits.PRIORITY_CITIES_MAX,
        "page_size_default": Limits.PAGE_SIZE_DEFAULT,
        "page_size_max": Limits.PAGE_SIZE_MAX,
        "default_radius_km": Limits.DEFAULT_RADIUS_KM,
        "active_candidate_days": Limits.ACTIVE_CANDIDATE_DAYS,
    }
