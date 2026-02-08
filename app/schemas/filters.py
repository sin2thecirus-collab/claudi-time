"""Filter Schemas für das Matching-Tool."""

from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, Field

from app.config import Limits
from app.schemas.validators import CitiesList, SearchTerm, SkillsList


class SortOrder(str, Enum):
    """Sortierreihenfolge."""

    ASC = "asc"
    DESC = "desc"


class JobSortBy(str, Enum):
    """Sortierfelder für Jobs."""

    CREATED_AT = "created_at"
    IMPORTED_AT = "imported_at"
    COMPANY_NAME = "company_name"
    POSITION = "position"
    CITY = "city"
    EXPIRES_AT = "expires_at"
    MATCH_COUNT = "match_count"


class CandidateSortBy(str, Enum):
    """Sortierfelder für Kandidaten."""

    CREATED_AT = "created_at"
    CRM_SYNCED_AT = "crm_synced_at"
    FULL_NAME = "full_name"
    CITY = "city"
    DISTANCE_KM = "distance_km"
    AI_SCORE = "ai_score"
    KEYWORD_SCORE = "keyword_score"


class JobFilterParams(BaseModel):
    """Filter-Parameter für Job-Listen."""

    # Textsuche
    search: SearchTerm = Field(default=None, description="Suche in Position/Unternehmen")

    # Multi-Select Filter
    cities: CitiesList = Field(default=None, description="Filter nach Städten")
    industries: list[str] | None = Field(default=None, description="Filter nach Branchen")

    # Einzelne Filter
    company: SearchTerm = Field(default=None, description="Filter nach Unternehmen")
    position: SearchTerm = Field(default=None, description="Filter nach Position")

    # Status-Filter
    has_active_candidates: bool | None = Field(
        default=None, description="Nur Jobs mit aktiven Kandidaten"
    )
    include_deleted: bool = Field(
        default=False, description="Gelöschte Jobs einschließen"
    )
    include_expired: bool = Field(
        default=False, description="Abgelaufene Jobs einschließen"
    )

    # Datum-Filter
    created_after: datetime | None = Field(
        default=None, description="Erstellt nach Datum"
    )
    created_before: datetime | None = Field(
        default=None, description="Erstellt vor Datum"
    )
    expires_after: datetime | None = Field(
        default=None, description="Läuft ab nach Datum"
    )
    expires_before: datetime | None = Field(
        default=None, description="Läuft ab vor Datum"
    )

    # Zeitraum-Filter
    imported_days: int | None = Field(
        default=None, ge=1, le=365, description="Importiert in den letzten X Tagen"
    )
    updated_days: int | None = Field(
        default=None, ge=1, le=365, description="Aktualisiert in den letzten X Tagen"
    )

    # Sortierung
    sort_by: JobSortBy = Field(default=JobSortBy.CREATED_AT, description="Sortierfeld")
    sort_order: SortOrder = Field(default=SortOrder.DESC, description="Sortierreihenfolge")


class CandidateFilterParams(BaseModel):
    """Filter-Parameter für Kandidaten-Listen (im Job-Kontext)."""

    # Textsuche
    name: SearchTerm = Field(default=None, description="Suche nach Name")

    # Multi-Select Filter
    cities: CitiesList = Field(default=None, description="Filter nach Städten")
    skills: SkillsList = Field(default=None, description="Filter nach Skills (AND)")

    # Einzelne Filter
    position: SearchTerm = Field(default=None, description="Filter nach Position")
    city_search: str | None = Field(default=None, description="Freitext-Suche nach Stadt")
    hotlist_category: str | None = Field(default=None, description="Filter nach Kategorie (FINANCE, ENGINEERING)")

    # Distanz-Filter
    min_distance_km: float | None = Field(
        default=None, ge=0, description="Mindestentfernung in km"
    )
    max_distance_km: float | None = Field(
        default=None, le=Limits.DEFAULT_RADIUS_KM, description="Maximale Entfernung in km"
    )

    # Status-Filter
    only_active: bool = Field(
        default=True, description="Nur aktive Kandidaten (≤30 Tage)"
    )
    include_hidden: bool = Field(
        default=False, description="Ausgeblendete Kandidaten einschließen"
    )
    only_ai_checked: bool = Field(
        default=False, description="Nur mit KI-Bewertung"
    )
    min_ai_score: float | None = Field(
        default=None, ge=0, le=1, description="Mindest-KI-Score"
    )
    status: str | None = Field(
        default=None, description="Filter nach Match-Status"
    )

    # Sortierung (Standard: neueste zuerst)
    sort_by: CandidateSortBy = Field(
        default=CandidateSortBy.CREATED_AT, description="Sortierfeld"
    )
    sort_order: SortOrder = Field(default=SortOrder.DESC, description="Sortierreihenfolge")


class FilterPresetCreate(BaseModel):
    """Schema für neuen Filter-Preset."""

    name: str = Field(min_length=1, max_length=100, description="Name des Presets")
    filter_config: dict = Field(description="Filter-Konfiguration als JSON")
    is_default: bool = Field(default=False, description="Als Standard setzen")


class FilterPresetResponse(BaseModel):
    """Schema für Filter-Preset-Response."""

    id: UUID
    name: str
    filter_config: dict
    is_default: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class FilterOptionsResponse(BaseModel):
    """Schema für verfügbare Filter-Optionen."""

    cities: list[str] = Field(description="Alle verfügbaren Städte")
    skills: list[str] = Field(description="Alle verfügbaren Skills")
    industries: list[str] = Field(description="Alle verfügbaren Branchen")
    employment_types: list[str] = Field(description="Alle Beschäftigungsarten")
