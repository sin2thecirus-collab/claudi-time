"""Settings API Routes - Endpoints für Prio-Städte und Einstellungen."""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.exception_handlers import NotFoundException, ConflictException
from app.api.rate_limiter import RateLimitTier, rate_limit
from app.config import Limits
from app.database import get_db
from app.models.settings import SystemSetting
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


# ==================== System-Einstellungen (Key-Value) ====================


class SystemSettingUpdate(BaseModel):
    """Schema fuer System-Einstellungs-Update."""
    value: str = Field(min_length=1, max_length=500)


@router.get(
    "/system/{key}",
    summary="System-Einstellung lesen",
)
async def get_system_setting(
    key: str,
    db: AsyncSession = Depends(get_db),
):
    """Liest eine System-Einstellung nach Key."""
    result = await db.execute(
        select(SystemSetting).where(SystemSetting.key == key)
    )
    setting = result.scalar_one_or_none()
    if not setting:
        raise NotFoundException(message=f"Einstellung '{key}' nicht gefunden")
    return {
        "key": setting.key,
        "value": setting.value,
        "description": setting.description,
        "updated_at": setting.updated_at.isoformat() if setting.updated_at else None,
    }


@router.put(
    "/system/{key}",
    summary="System-Einstellung aendern",
)
@rate_limit(RateLimitTier.WRITE)
async def update_system_setting(
    key: str,
    data: SystemSettingUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Aendert eine System-Einstellung. Gilt NUR fuer zukuenftige Operationen."""
    result = await db.execute(
        select(SystemSetting).where(SystemSetting.key == key)
    )
    setting = result.scalar_one_or_none()
    if not setting:
        raise NotFoundException(message=f"Einstellung '{key}' nicht gefunden")

    # Validierung fuer bekannte Keys
    if key == "drive_time_score_threshold":
        try:
            val = int(data.value)
            if not (0 <= val <= 100):
                raise ValueError
        except ValueError:
            return {"error": "Wert muss eine Zahl zwischen 0 und 100 sein"}, 400

    old_value = setting.value
    setting.value = data.value
    await db.commit()

    logger.info(f"System-Einstellung '{key}' geaendert: {old_value} → {data.value}")

    return {
        "key": setting.key,
        "value": setting.value,
        "old_value": old_value,
        "description": setting.description,
        "message": f"Einstellung '{key}' auf {data.value} geaendert. Gilt fuer zukuenftige Matches.",
    }


# ── Helper: Threshold aus DB lesen (wird von matching_engine importiert) ──

async def get_drive_time_threshold(db: AsyncSession) -> int:
    """Liest den drive_time_score_threshold aus der DB. Fallback: 80."""
    try:
        result = await db.execute(
            select(SystemSetting.value).where(
                SystemSetting.key == "drive_time_score_threshold"
            )
        )
        val = result.scalar_one_or_none()
        if val is not None:
            return int(val)
    except Exception:
        pass
    return 80  # Fallback
