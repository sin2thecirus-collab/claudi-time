"""Candidate Schemas für das Matching-Tool."""

from datetime import date, datetime
from typing import Literal
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
    """Schema für Kandidaten-Erstellung."""

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
    salary: str | None = Field(default=None, max_length=100, description="Gehaltswunsch z.B. '55.000 €'")
    notice_period: str | None = Field(default=None, max_length=100, description="Kuendigungsfrist z.B. '3 Monate'")
    erp: list[str] | None = Field(default=None, description="ERP-Kenntnisse z.B. ['SAP', 'DATEV']")
    rating: int | None = Field(default=None, description="Sterne-Bewertung 1-5")
    source: str | None = Field(default=None, max_length=50, description="Quelle z.B. 'StepStone', 'LinkedIn'")
    last_contact: datetime | None = Field(default=None, description="Datum des letzten Kontakts")
    willingness_to_change: Literal["ja", "nein", "unbekannt"] | None = Field(default=None, description="Wechselbereitschaft")
    candidate_notes: str | None = Field(default=None, description="Freitext-Notizen")
    presented_at_companies: list[dict] | None = Field(default=None, description="Vorgestellt/Beworben bei Unternehmen")
    # Qualifizierungsgespräch-Felder
    desired_positions: str | None = Field(default=None, description="Gewünschte Positionen (Freitext)")
    key_activities: str | None = Field(default=None, description="Tätigkeiten die voll umfänglich beherrscht werden")
    home_office_days: str | None = Field(default=None, max_length=50, description="Home-Office Tage z.B. '2 bis 3 Tage'")
    commute_max: str | None = Field(default=None, max_length=100, description="Pendelbereitschaft z.B. '30 min'")
    commute_transport: str | None = Field(default=None, max_length=50, description="Auto / ÖPNV / Beides")
    erp_main: str | None = Field(default=None, max_length=100, description="ERP-Steckenpferd z.B. 'DATEV'")
    employment_type: str | None = Field(default=None, max_length=50, description="Vollzeit / Teilzeit")
    part_time_hours: str | None = Field(default=None, max_length=50, description="Teilzeit-Stunden z.B. '30 Stunden'")
    preferred_industries: str | None = Field(default=None, description="Bevorzugte Branchen (Freitext)")
    avoided_industries: str | None = Field(default=None, description="Branchen vermeiden (Freitext)")
    open_office_ok: str | None = Field(default=None, max_length=20, description="Großraumbüro OK: ja/nein/egal")
    whatsapp_ok: bool | None = Field(default=None, description="WhatsApp-Kontakt erlaubt?")
    other_recruiters: str | None = Field(default=None, description="Andere Recruiter aktiv? Details")
    exclusivity_agreed: bool | None = Field(default=None, description="Exklusivität vereinbart?")
    applied_at_companies_text: str | None = Field(default=None, description="Wo bereits beworben (Freitext aus Transkription)")
    call_transcript: str | None = Field(default=None, description="Volle Transkription des Gesprächs")
    call_summary: str | None = Field(default=None, description="KI-generierte Zusammenfassung")
    call_date: datetime | None = Field(default=None, description="Datum des Qualifizierungsgesprächs")
    call_type: str | None = Field(default=None, max_length=50, description="qualifizierung/kurz/kunde/sonstig")


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
    salary: str | None
    notice_period: str | None
    erp: list[str] | None
    rating: int | None
    source: str | None
    last_contact: datetime | None
    willingness_to_change: str | None
    candidate_notes: str | None
    candidate_number: int | None
    presented_at_companies: list[dict] | None
    # Qualifizierungsgespräch-Felder
    desired_positions: str | None
    key_activities: str | None
    home_office_days: str | None
    commute_max: str | None
    commute_transport: str | None
    erp_main: str | None
    employment_type: str | None
    part_time_hours: str | None
    preferred_industries: str | None
    avoided_industries: str | None
    open_office_ok: str | None
    whatsapp_ok: bool | None
    other_recruiters: str | None
    exclusivity_agreed: bool | None
    applied_at_companies_text: str | None
    call_transcript: str | None
    call_summary: str | None
    call_date: datetime | None
    call_type: str | None
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
    email: str | None = None
    phone: str | None = None
    birth_date: str | None = None  # String, da CV-Format variiert
    estimated_age: int | None = None  # Geschaetztes Alter wenn kein Geburtsdatum
    street_address: str | None = None
    postal_code: str | None = None
    city: str | None = None
    current_position: str | None = None
    current_company: str | None = None
    skills: list[str] | None = None
    languages: list[LanguageEntry] | None = None
    it_skills: list[str] | None = None
    work_history: list[WorkHistoryEntry] | None = None
    education: list[EducationEntry] | None = None
    further_education: list[EducationEntry] | None = None
