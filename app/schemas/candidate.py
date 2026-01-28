"""Candidate Schemas für das Matching-Tool."""

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field

from app.schemas.validators import CityName, PostalCode


class WorkHistoryEntry(BaseModel):
    """Schema für einen Eintrag in der Berufserfahrung."""

    company: str | None = Field(default=None, description="Firmenname")
    position: str | None = Field(default=None, description="Position")
    start_date: str | None = Field(default=None, description="Startdatum")
    end_date: str | None = Field(default=None, description="Enddatum (oder 'heute')")
    description: str | None = Field(default=None, description="Beschreibung der Tätigkeit")


class EducationEntry(BaseModel):
    """Schema für einen Eintrag in der Ausbildung."""

    institution: str | None = Field(default=None, description="Bildungseinrichtung")
    degree: str | None = Field(default=None, description="Abschluss")
    field_of_study: str | None = Field(default=None, description="Fachrichtung")
    start_date: str | None = Field(default=None, description="Startdatum")
    end_date: str | None = Field(default=None, description="Enddatum")


class LanguageEntry(BaseModel):
    """Schema für eine Sprachkenntnis."""

    language: str = Field(description="Sprache (z.B. Englisch, Deutsch)")
    level: str | None = Field(default=None, description="Niveau (z.B. B2, Muttersprache, Grundkenntnisse)")


class CandidateBase(BaseModel):
    """Basis-Schema für Kandidaten."""

    first_name: str | None = Field(default=None, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=50)
    birth_date: date | None = None
    current_position: str | None = Field(default=None, max_length=255)
    current_company: str | None = Field(default=None, max_length=255)
    skills: list[str] | None = None
    street_address: str | None = Field(default=None, max_length=255)
    postal_code: PostalCode = None
    city: CityName = None


class CandidateCreate(CandidateBase):
    """Schema für Kandidaten-Erstellung (CRM-Sync)."""

    crm_id: str | None = Field(default=None, max_length=100, description="CRM-Referenz")
    cv_url: str | None = Field(default=None, max_length=500, description="URL zum CV")


class CandidateUpdate(BaseModel):
    """Schema für Kandidaten-Updates."""

    first_name: str | None = Field(default=None, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=50)
    birth_date: date | None = None
    current_position: str | None = Field(default=None, max_length=255)
    current_company: str | None = Field(default=None, max_length=255)
    skills: list[str] | None = None
    work_history: list[WorkHistoryEntry] | None = None
    education: list[EducationEntry] | None = None
    street_address: str | None = Field(default=None, max_length=255)
    postal_code: PostalCode = None
    city: CityName = None
    cv_text: str | None = None
    cv_url: str | None = Field(default=None, max_length=500)


class CandidateResponse(BaseModel):
    """Schema für Kandidaten-Response."""

    id: UUID
    crm_id: str | None
    first_name: str | None
    last_name: str | None
    full_name: str
    email: str | None
    phone: str | None
    birth_date: date | None
    age: int | None
    current_position: str | None
    current_company: str | None
    skills: list[str] | None
    languages: list[LanguageEntry] | None
    it_skills: list[str] | None
    work_history: list[WorkHistoryEntry] | None
    education: list[EducationEntry] | None
    further_education: list[EducationEntry] | None
    street_address: str | None
    postal_code: str | None
    city: str | None
    has_coordinates: bool
    cv_url: str | None
    cv_parsed_at: datetime | None
    hidden: bool
    is_active: bool
    crm_synced_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class CandidateListResponse(BaseModel):
    """Schema für paginierte Kandidaten-Listen."""

    items: list[CandidateResponse]
    total: int
    page: int
    per_page: int
    pages: int


class CandidateWithMatch(CandidateResponse):
    """Schema für Kandidaten mit Match-Daten (für Job-Detail)."""

    distance_km: float | None = Field(description="Entfernung zum Job in km")
    keyword_score: float | None = Field(description="Keyword-Score (0-1)")
    matched_keywords: list[str] | None = Field(description="Gefundene Keywords")
    ai_score: float | None = Field(description="KI-Score (0-1)")
    ai_explanation: str | None = Field(description="KI-Erklärung")
    ai_strengths: list[str] | None = Field(description="Stärken laut KI")
    ai_weaknesses: list[str] | None = Field(description="Schwächen laut KI")
    match_status: str | None = Field(description="Match-Status")
    match_id: UUID | None = Field(description="Match-ID")
    is_ai_checked: bool = Field(default=False, description="Hat KI-Bewertung")


class CVParseResult(BaseModel):
    """Schema für das Ergebnis des CV-Parsings."""

    first_name: str | None = None
    last_name: str | None = None
    birth_date: str | None = None  # String, da CV-Format variiert
    street_address: str | None = None
    postal_code: str | None = None
    city: str | None = None
    current_position: str | None = None
    skills: list[str] | None = None
    languages: list[LanguageEntry] | None = None
    it_skills: list[str] | None = None
    work_history: list[WorkHistoryEntry] | None = None
    education: list[EducationEntry] | None = None
    further_education: list[EducationEntry] | None = None
