"""Matches API Routes - Endpoints für Job-Kandidaten-Matches."""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.exception_handlers import NotFoundException, ConflictException
from app.api.rate_limiter import RateLimitTier, rate_limit
from app.config import Limits
from app.database import get_db
from app.models.match import MatchStatus
from app.schemas.match import (
    AICheckRequest,
    AICheckResponse,
    AICheckResultItem,
    MatchListResponse,
    MatchPlacedUpdate,
    MatchResponse,
    MatchStatusUpdate,
    MatchWithDetails,
)
from app.schemas.validators import BatchDeleteRequest
from app.services.matching_service import MatchingService
from app.services.openai_service import OpenAIService
from app.services.job_service import JobService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/matches", tags=["Matches"])


# ==================== KI-Check ====================

@router.post(
    "/ai-check",
    response_model=AICheckResponse,
    summary="KI-Check für Kandidaten",
    description="Bewertet ausgewählte Kandidaten mit KI für einen Job",
)
@rate_limit(RateLimitTier.AI)
async def ai_check_candidates(
    request: AICheckRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Führt eine KI-Bewertung für ausgewählte Kandidaten durch.

    - Maximal 50 Kandidaten pro Anfrage
    - Zeigt geschätzte Kosten vor der Ausführung
    - Speichert Ergebnisse in den Match-Einträgen
    """
    # Job prüfen
    job_service = JobService(db)
    job = await job_service.get_job(request.job_id)
    if not job:
        raise NotFoundException(message="Job nicht gefunden")

    # Kosten schätzen
    openai_service = OpenAIService()
    estimated_cost = openai_service.estimate_cost(len(request.candidate_ids))

    # Matches laden
    matching_service = MatchingService(db)
    results: list[AICheckResultItem] = []
    total_cost = 0.0
    successful = 0
    failed = 0

    for candidate_id in request.candidate_ids:
        # Match laden oder erstellen
        from sqlalchemy import select, and_
        from app.models.match import Match
        from app.models.candidate import Candidate

        # Kandidat prüfen
        candidate = await db.get(Candidate, candidate_id)
        if not candidate:
            results.append(AICheckResultItem(
                candidate_id=candidate_id,
                match_id=UUID("00000000-0000-0000-0000-000000000000"),
                success=False,
                error="Kandidat nicht gefunden",
            ))
            failed += 1
            continue

        # Match laden
        query = select(Match).where(
            and_(
                Match.job_id == request.job_id,
                Match.candidate_id == candidate_id,
            )
        )
        result = await db.execute(query)
        match = result.scalar_one_or_none()

        if not match:
            results.append(AICheckResultItem(
                candidate_id=candidate_id,
                match_id=UUID("00000000-0000-0000-0000-000000000000"),
                success=False,
                error="Kein Match vorhanden - Kandidat außerhalb des Radius?",
            ))
            failed += 1
            continue

        # KI-Bewertung durchführen
        try:
            evaluation = await openai_service.evaluate_match(job, candidate)

            # Match aktualisieren
            match.ai_score = evaluation.score
            match.ai_explanation = evaluation.explanation
            match.ai_strengths = evaluation.strengths
            match.ai_weaknesses = evaluation.weaknesses
            match.ai_checked_at = evaluation.checked_at
            match.status = MatchStatus.AI_CHECKED

            await db.commit()
            await db.refresh(match)

            total_cost += evaluation.cost_usd

            results.append(AICheckResultItem(
                candidate_id=candidate_id,
                match_id=match.id,
                success=True,
                ai_score=evaluation.score,
                ai_explanation=evaluation.explanation,
                ai_strengths=evaluation.strengths,
                ai_weaknesses=evaluation.weaknesses,
            ))
            successful += 1

        except Exception as e:
            logger.error(f"KI-Check fehlgeschlagen für {candidate_id}: {e}")
            results.append(AICheckResultItem(
                candidate_id=candidate_id,
                match_id=match.id,
                success=False,
                error=str(e),
            ))
            failed += 1

    return AICheckResponse(
        job_id=request.job_id,
        total_candidates=len(request.candidate_ids),
        successful_checks=successful,
        failed_checks=failed,
        results=results,
        estimated_cost_usd=estimated_cost,
        actual_cost_usd=total_cost,
    )


@router.get(
    "/ai-check/estimate",
    summary="Kosten schätzen",
)
async def estimate_ai_check_cost(
    count: int = Query(ge=1, le=Limits.AI_CHECK_MAX_CANDIDATES),
):
    """
    Schätzt die Kosten für einen KI-Check.

    Basiert auf durchschnittlichem Token-Verbrauch pro Bewertung.
    """
    openai_service = OpenAIService()
    estimated_cost = openai_service.estimate_cost(count)

    return {
        "candidate_count": count,
        "estimated_cost_usd": estimated_cost,
        "price_per_candidate_usd": estimated_cost / count if count > 0 else 0,
    }


# ==================== Match-Operationen ====================

@router.get(
    "/job/{job_id}",
    response_model=MatchListResponse,
    summary="Matches für einen Job",
)
async def get_matches_for_job(
    job_id: UUID,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=Limits.PAGE_SIZE_DEFAULT, ge=1, le=Limits.PAGE_SIZE_MAX),
    include_hidden: bool = Query(default=False),
    only_ai_checked: bool = Query(default=False),
    min_ai_score: float | None = Query(default=None, ge=0, le=1),
    match_status: str | None = Query(default=None),
    sort_by: str = Query(default="distance"),
    db: AsyncSession = Depends(get_db),
):
    """Gibt alle Matches für einen Job zurück."""
    # Job prüfen
    job_service = JobService(db)
    job = await job_service.get_job(job_id)
    if not job:
        raise NotFoundException(message="Job nicht gefunden")

    # Status parsen
    status_filter = None
    if match_status:
        try:
            status_filter = MatchStatus(match_status)
        except ValueError:
            pass

    matching_service = MatchingService(db)
    matches, total = await matching_service.get_matches_for_job(
        job_id=job_id,
        include_hidden=include_hidden,
        only_ai_checked=only_ai_checked,
        min_ai_score=min_ai_score,
        status=status_filter,
        sort_by=sort_by,
        limit=per_page,
        offset=(page - 1) * per_page,
    )

    pages = (total + per_page - 1) // per_page if per_page > 0 else 0

    return MatchListResponse(
        items=[_match_to_response(m) for m in matches],
        total=total,
        page=page,
        per_page=per_page,
        pages=pages,
    )


@router.get(
    "/{match_id}",
    response_model=MatchWithDetails,
    summary="Match-Details abrufen",
)
async def get_match(
    match_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Gibt die Details eines Matches zurück."""
    matching_service = MatchingService(db)
    match = await matching_service.get_match(match_id)

    if not match:
        raise NotFoundException(message="Match nicht gefunden")

    return _match_to_detailed_response(match)


@router.put(
    "/{match_id}/status",
    response_model=MatchResponse,
    summary="Match-Status ändern",
)
@rate_limit(RateLimitTier.WRITE)
async def update_match_status(
    match_id: UUID,
    data: MatchStatusUpdate,
    db: AsyncSession = Depends(get_db),
):
    """
    Ändert den Status eines Matches.

    Mögliche Status:
    - ai_checked: KI-Bewertung abgeschlossen
    - presented: Kandidat wurde vorgestellt
    - rejected: Kandidat wurde abgelehnt
    - placed: Kandidat wurde vermittelt
    """
    # Status validieren
    try:
        new_status = MatchStatus(data.status)
    except ValueError:
        raise ConflictException(
            message=f"Ungültiger Status. Erlaubt: {[s.value for s in MatchStatus]}"
        )

    matching_service = MatchingService(db)
    match = await matching_service.update_match_status(match_id, new_status)

    if not match:
        raise NotFoundException(message="Match nicht gefunden")

    return _match_to_response(match)


@router.put(
    "/{match_id}/placed",
    response_model=MatchResponse,
    summary="Als vermittelt markieren",
)
@rate_limit(RateLimitTier.WRITE)
async def mark_as_placed(
    match_id: UUID,
    data: MatchPlacedUpdate,
    db: AsyncSession = Depends(get_db),
):
    """
    Markiert einen Match als erfolgreich vermittelt.

    Optional können Notizen hinzugefügt werden.
    """
    matching_service = MatchingService(db)
    match = await matching_service.mark_as_placed(match_id, data.notes)

    if not match:
        raise NotFoundException(message="Match nicht gefunden")

    return _match_to_response(match)


@router.delete(
    "/{match_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Match löschen",
)
@rate_limit(RateLimitTier.WRITE)
async def delete_match(
    match_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Löscht einen Match."""
    matching_service = MatchingService(db)
    success = await matching_service.delete_match(match_id)

    if not success:
        raise NotFoundException(message="Match nicht gefunden")


@router.delete(
    "/batch",
    summary="Mehrere Matches löschen",
)
@rate_limit(RateLimitTier.WRITE)
async def batch_delete_matches(
    request: BatchDeleteRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Löscht mehrere Matches auf einmal.

    Maximal 100 Matches pro Anfrage.
    """
    matching_service = MatchingService(db)
    deleted_count = await matching_service.batch_delete_matches(request.ids)

    return {"deleted_count": deleted_count}


# ==================== Statistiken ====================

@router.get(
    "/job/{job_id}/statistics",
    summary="Match-Statistiken für einen Job",
)
async def get_match_statistics(
    job_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Gibt Statistiken über die Matches eines Jobs zurück."""
    job_service = JobService(db)
    job = await job_service.get_job(job_id)
    if not job:
        raise NotFoundException(message="Job nicht gefunden")

    matching_service = MatchingService(db)
    stats = await matching_service.get_match_statistics(job_id)

    return stats


@router.get(
    "/excellent",
    summary="Exzellente Matches",
)
async def get_excellent_matches(
    limit: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """
    Gibt die besten Matches zurück.

    Kriterien für exzellente Matches:
    - Distanz ≤ 5km
    - Mindestens 3 gematchte Keywords
    - Status: neu (noch nicht bearbeitet)
    """
    matching_service = MatchingService(db)
    matches = await matching_service.get_excellent_matches()

    return {
        "items": [_match_to_detailed_response(m) for m in matches[:limit]],
        "total": len(matches),
    }


# ==================== Hilfsfunktionen ====================

def _match_to_response(match) -> MatchResponse:
    """Konvertiert ein Match-Model zu einem Response-Schema."""
    return MatchResponse(
        id=match.id,
        job_id=match.job_id,
        candidate_id=match.candidate_id,
        distance_km=match.distance_km,
        keyword_score=match.keyword_score,
        matched_keywords=match.matched_keywords,
        ai_score=match.ai_score,
        ai_explanation=match.ai_explanation,
        ai_strengths=match.ai_strengths,
        ai_weaknesses=match.ai_weaknesses,
        ai_checked_at=match.ai_checked_at,
        is_ai_checked=match.is_ai_checked,
        status=match.status.value,
        is_excellent=match.is_excellent,
        placed_at=match.placed_at,
        placed_notes=match.placed_notes,
        created_at=match.created_at,
        updated_at=match.updated_at,
    )


def _match_to_detailed_response(match) -> MatchWithDetails:
    """Konvertiert ein Match-Model mit Details zu einem Response-Schema."""
    base = _match_to_response(match)

    return MatchWithDetails(
        **base.model_dump(),
        job_company_name=match.job.company_name if match.job else "Unbekannt",
        job_position=match.job.position if match.job else "Unbekannt",
        job_city=match.job.display_city if match.job else None,
        candidate_full_name=match.candidate.full_name if match.candidate else "Unbekannt",
        candidate_current_position=match.candidate.current_position if match.candidate else None,
        candidate_city=match.candidate.city if match.candidate else None,
    )
