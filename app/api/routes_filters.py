"""Filters API Routes - Endpoints für Filter-Optionen und Presets."""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.exception_handlers import NotFoundException, ConflictException
from app.api.rate_limiter import RateLimitTier, rate_limit
from app.config import Limits
from app.database import get_db
from app.schemas.filters import (
    FilterOptionsResponse,
    FilterPresetCreate,
    FilterPresetResponse,
)
from app.services.filter_service import FilterService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/filters", tags=["Filter"])


# ==================== Filter-Optionen ====================

@router.get(
    "/options",
    response_model=FilterOptionsResponse,
    summary="Alle Filter-Optionen",
    description="Gibt alle verfügbaren Werte für Filter-Dropdowns zurück",
)
async def get_filter_options(
    db: AsyncSession = Depends(get_db),
):
    """
    Lädt alle verfügbaren Filter-Optionen.

    Wird verwendet, um die Dropdowns in der UI zu befüllen.
    Enthält alle Städte, Skills, Branchen und Beschäftigungsarten.
    """
    filter_service = FilterService(db)

    cities = await filter_service.get_available_cities()
    skills = await filter_service.get_available_skills()
    industries = await filter_service.get_available_industries()
    employment_types = await filter_service.get_available_employment_types()

    return FilterOptionsResponse(
        cities=cities,
        skills=skills,
        industries=industries,
        employment_types=employment_types,
    )


@router.get(
    "/cities",
    summary="Verfügbare Städte",
)
async def get_cities(
    db: AsyncSession = Depends(get_db),
):
    """Gibt alle verfügbaren Städte zurück."""
    filter_service = FilterService(db)
    cities = await filter_service.get_available_cities()

    return {"cities": cities, "count": len(cities)}


@router.get(
    "/skills",
    summary="Verfügbare Skills",
)
async def get_skills(
    db: AsyncSession = Depends(get_db),
):
    """Gibt alle verfügbaren Skills aus Kandidaten zurück."""
    filter_service = FilterService(db)
    skills = await filter_service.get_available_skills()

    return {"skills": skills, "count": len(skills)}


@router.get(
    "/industries",
    summary="Verfügbare Branchen",
)
async def get_industries(
    db: AsyncSession = Depends(get_db),
):
    """Gibt alle verfügbaren Branchen aus Jobs zurück."""
    filter_service = FilterService(db)
    industries = await filter_service.get_available_industries()

    return {"industries": industries, "count": len(industries)}


@router.get(
    "/employment-types",
    summary="Verfügbare Beschäftigungsarten",
)
async def get_employment_types(
    db: AsyncSession = Depends(get_db),
):
    """Gibt alle verfügbaren Beschäftigungsarten zurück."""
    filter_service = FilterService(db)
    types = await filter_service.get_available_employment_types()

    return {"employment_types": types, "count": len(types)}


# ==================== Filter-Presets ====================

@router.get(
    "/presets",
    response_model=list[FilterPresetResponse],
    summary="Filter-Presets auflisten",
)
async def list_filter_presets(
    db: AsyncSession = Depends(get_db),
):
    """
    Gibt alle gespeicherten Filter-Presets zurück.

    Presets ermöglichen das schnelle Wiederverwenden von
    häufig genutzten Filter-Kombinationen.
    """
    filter_service = FilterService(db)
    presets = await filter_service.get_filter_presets()

    return [_preset_to_response(p) for p in presets]


@router.get(
    "/presets/{preset_id}",
    response_model=FilterPresetResponse,
    summary="Filter-Preset abrufen",
)
async def get_filter_preset(
    preset_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Gibt einen einzelnen Filter-Preset zurück."""
    filter_service = FilterService(db)
    preset = await filter_service.get_filter_preset(preset_id)

    if not preset:
        raise NotFoundException(message="Filter-Preset nicht gefunden")

    return _preset_to_response(preset)


@router.post(
    "/presets",
    response_model=FilterPresetResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Filter-Preset erstellen",
)
@rate_limit(RateLimitTier.WRITE)
async def create_filter_preset(
    data: FilterPresetCreate,
    db: AsyncSession = Depends(get_db),
):
    """
    Erstellt einen neuen Filter-Preset.

    Die filter_config enthält die gespeicherten Filter-Werte als JSON.
    """
    filter_service = FilterService(db)

    # Prüfe Limit
    existing = await filter_service.get_filter_presets()
    if len(existing) >= Limits.FILTER_PRESETS_MAX:
        raise ConflictException(
            message=f"Maximal {Limits.FILTER_PRESETS_MAX} Filter-Presets erlaubt"
        )

    preset = await filter_service.create_filter_preset(data)
    return _preset_to_response(preset)


@router.delete(
    "/presets/{preset_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Filter-Preset löschen",
)
@rate_limit(RateLimitTier.WRITE)
async def delete_filter_preset(
    preset_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Löscht einen Filter-Preset."""
    filter_service = FilterService(db)
    success = await filter_service.delete_filter_preset(preset_id)

    if not success:
        raise NotFoundException(message="Filter-Preset nicht gefunden")


@router.put(
    "/presets/{preset_id}/default",
    response_model=FilterPresetResponse,
    summary="Als Standard setzen",
)
@rate_limit(RateLimitTier.WRITE)
async def set_default_preset(
    preset_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """
    Setzt einen Preset als Standard-Filter.

    Der Standard-Preset wird automatisch beim Öffnen der Seite angewendet.
    """
    filter_service = FilterService(db)
    preset = await filter_service.set_default_preset(preset_id)

    if not preset:
        raise NotFoundException(message="Filter-Preset nicht gefunden")

    return _preset_to_response(preset)


# ==================== Hilfsfunktionen ====================

def _preset_to_response(preset) -> FilterPresetResponse:
    """Konvertiert ein FilterPreset-Model zu einem Response-Schema."""
    return FilterPresetResponse(
        id=preset.id,
        name=preset.name,
        filter_config=preset.filter_config,
        is_default=preset.is_default,
        created_at=preset.created_at,
        updated_at=preset.updated_at,
    )
