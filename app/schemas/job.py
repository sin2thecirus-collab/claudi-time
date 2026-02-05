"""Job Schemas für das Matching-Tool."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, HttpUrl, field_validator

from app.schemas.validators import CityName, PostalCode


class JobBase(BaseModel):
    """Basis-Schema für Jobs."""

    company_name: str = Field(min_length=1, max_length=255, description="Firmenname")
    position: str = Field(min_length=1, max_length=255, description="Positionsbezeichnung")
    street_address: str | None = Field(
        default=None, max_length=255, description="Straße und Hausnummer"
    )
    postal_code: PostalCode = Field(default=None, description="Postleitzahl (5 Ziffern)")
    city: CityName = Field(default=None, description="Stadt")
    work_location_city: CityName = Field(
        default=None, description="Arbeitsort (falls abweichend)"
    )
    job_url: str | None = Field(default=None, max_length=500, description="URL zur Stellenanzeige")
    job_text: str | None = Field(default=None, description="Volltext der Stellenanzeige")
    employment_type: str | None = Field(
        default=None, max_length=100, description="Beschäftigungsart"
    )
    industry: str | None = Field(default=None, max_length=100, description="Branche")
    company_size: str | None = Field(
        default=None, max_length=50, description="Unternehmensgröße"
    )


class JobCreate(JobBase):
    """Schema für Job-Erstellung (CSV-Import und manuell)."""

    company_id: UUID | None = Field(default=None, description="Verknüpftes Unternehmen")
    expires_at: datetime | None = Field(default=None, description="Ablaufdatum")

    @field_validator("company_name", "position", mode="before")
    @classmethod
    def strip_strings(cls, v: str | None) -> str | None:
        """Entfernt führende/folgende Leerzeichen."""
        if isinstance(v, str):
            return v.strip()
        return v


class JobUpdate(BaseModel):
    """Schema für Job-Updates."""

    company_name: str | None = Field(default=None, min_length=1, max_length=255)
    position: str | None = Field(default=None, min_length=1, max_length=255)
    street_address: str | None = Field(default=None, max_length=255)
    postal_code: PostalCode = None
    city: CityName = None
    work_location_city: CityName = None
    job_url: str | None = Field(default=None, max_length=500)
    job_text: str | None = None
    employment_type: str | None = Field(default=None, max_length=100)
    industry: str | None = Field(default=None, max_length=100)
    company_size: str | None = Field(default=None, max_length=50)
    expires_at: datetime | None = None
    excluded_from_deletion: bool | None = None


class JobResponse(BaseModel):
    """Schema für Job-Response."""

    id: UUID
    company_name: str
    position: str
    street_address: str | None
    postal_code: str | None
    city: str | None
    work_location_city: str | None
    display_city: str
    job_url: str | None
    job_text: str | None
    employment_type: str | None
    industry: str | None
    company_size: str | None
    has_coordinates: bool
    expires_at: datetime | None
    excluded_from_deletion: bool
    is_deleted: bool
    is_expired: bool
    imported_at: datetime | None = None
    last_updated_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    # Zusätzliche Felder für Listen-Ansicht
    match_count: int | None = Field(
        default=None, description="Anzahl der Matches (nur in Listen)"
    )
    active_candidate_count: int | None = Field(
        default=None, description="Anzahl aktiver Kandidaten (nur in Listen)"
    )

    model_config = {"from_attributes": True}


class JobListResponse(BaseModel):
    """Schema für paginierte Job-Listen."""

    items: list[JobResponse]
    total: int
    page: int
    per_page: int
    pages: int


class JobImportRow(BaseModel):
    """Schema für eine Zeile im CSV-Import."""

    company_name: str = Field(alias="Unternehmen")
    position: str = Field(alias="Position")
    street_address: str | None = Field(default=None, alias="Straße")
    postal_code: str | None = Field(default=None, alias="PLZ")
    city: str | None = Field(default=None, alias="Stadt")
    work_location_city: str | None = Field(default=None, alias="Arbeitsort")
    job_url: str | None = Field(default=None, alias="URL")
    job_text: str | None = Field(default=None, alias="Beschreibung")
    employment_type: str | None = Field(default=None, alias="Beschäftigungsart")
    industry: str | None = Field(default=None, alias="Branche")
    company_size: str | None = Field(default=None, alias="Unternehmensgröße")

    model_config = {"populate_by_name": True}


class ImportJobResponse(BaseModel):
    """Schema für Import-Job-Status."""

    id: UUID
    filename: str
    total_rows: int
    processed_rows: int
    successful_rows: int
    failed_rows: int
    status: str
    progress_percent: float
    error_message: str | None
    errors_detail: dict | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}
