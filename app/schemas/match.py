"""Match Schemas für das Matching-Tool."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.config import Limits


class MatchResponse(BaseModel):
    """Schema für Match-Response."""

    id: UUID
    job_id: UUID
    candidate_id: UUID

    # Basis-Matching-Daten
    distance_km: float | None
    keyword_score: float | None
    matched_keywords: list[str] | None

    # KI-Bewertung
    ai_score: float | None
    ai_explanation: str | None
    ai_strengths: list[str] | None
    ai_weaknesses: list[str] | None
    ai_checked_at: datetime | None
    is_ai_checked: bool

    # Status
    status: str
    is_excellent: bool

    # Vermittlung
    placed_at: datetime | None
    placed_notes: str | None

    # Timestamps
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class MatchWithDetails(MatchResponse):
    """Schema für Match mit Job- und Kandidaten-Details."""

    # Job-Details
    job_company_name: str
    job_position: str
    job_city: str | None

    # Kandidaten-Details
    candidate_full_name: str
    candidate_current_position: str | None
    candidate_city: str | None


class MatchListResponse(BaseModel):
    """Schema für paginierte Match-Listen."""

    items: list[MatchResponse]
    total: int
    page: int
    per_page: int
    pages: int


class AICheckRequest(BaseModel):
    """Request für KI-Check von Kandidaten."""

    job_id: UUID = Field(description="Job-ID")
    candidate_ids: list[UUID] = Field(
        min_length=1,
        max_length=Limits.AI_CHECK_MAX_CANDIDATES,
        description=f"Kandidaten-IDs (max. {Limits.AI_CHECK_MAX_CANDIDATES})",
    )


class AICheckResultItem(BaseModel):
    """Ergebnis für einen einzelnen KI-Check."""

    candidate_id: UUID
    match_id: UUID
    success: bool
    ai_score: float | None = None
    ai_explanation: str | None = None
    ai_strengths: list[str] | None = None
    ai_weaknesses: list[str] | None = None
    error: str | None = None


class AICheckResponse(BaseModel):
    """Response für KI-Check."""

    job_id: UUID
    total_candidates: int
    successful_checks: int
    failed_checks: int
    results: list[AICheckResultItem]
    estimated_cost_usd: float = Field(description="Geschätzte Kosten in USD")
    actual_cost_usd: float | None = Field(
        default=None, description="Tatsächliche Kosten (nach Abschluss)"
    )


class MatchStatusUpdate(BaseModel):
    """Schema für Status-Update eines Matches."""

    status: str = Field(
        description="Neuer Status (ai_checked, presented, rejected, placed)"
    )


class MatchPlacedUpdate(BaseModel):
    """Schema für Vermittlungs-Update."""

    notes: str | None = Field(
        default=None,
        max_length=1000,
        description="Notizen zur Vermittlung",
    )
