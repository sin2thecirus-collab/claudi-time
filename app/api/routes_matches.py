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
            # Job- und Kandidaten-Daten für OpenAI aufbereiten
            job_data = {
                "position": job.position,
                "company_name": job.company_name,
                "industry": job.industry,
                "job_text": job.job_text,
            }
            candidate_data = {
                "full_name": candidate.full_name,
                "current_position": candidate.current_position,
                "current_company": candidate.current_company,
                "skills": candidate.skills or [],
                "work_history": candidate.work_history or [],
                "education": candidate.education or [],
            }

            evaluation = await openai_service.evaluate_match(job_data, candidate_data)

            # Match aktualisieren
            from datetime import datetime, timezone
            match.ai_score = evaluation.score
            match.ai_explanation = evaluation.explanation
            match.ai_strengths = evaluation.strengths
            match.ai_weaknesses = evaluation.weaknesses
            match.ai_checked_at = datetime.now(timezone.utc)
            match.status = MatchStatus.AI_CHECKED

            await db.commit()
            await db.refresh(match)

            total_cost += evaluation.usage.cost_usd

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


# ==================== Outreach (Phase 11-12) ====================

@router.get(
    "/{match_id}/job-pdf",
    summary="Job-Description-PDF generieren",
    description="Generiert ein Sincirus Branded PDF mit Stellendetails für den Kandidaten",
)
async def get_job_pdf(
    match_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Generiert ein Job-Description-PDF für einen Match."""
    from fastapi.responses import StreamingResponse
    from io import BytesIO
    from app.services.job_description_pdf_service import JobDescriptionPdfService

    pdf_service = JobDescriptionPdfService(db)

    try:
        pdf_bytes = await pdf_service.generate_job_pdf(match_id)
    except ValueError as e:
        raise NotFoundException(message=str(e))

    return StreamingResponse(
        BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="Stellenbeschreibung_{match_id}.pdf"',
        },
    )


@router.post(
    "/{match_id}/send-to-candidate",
    summary="E-Mail + Job-PDF an Kandidat senden",
    description="Generiert personalisierte E-Mail mit Job-PDF und sendet an den Kandidaten",
)
@rate_limit(RateLimitTier.WRITE)
async def send_to_candidate(
    match_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Sendet eine personalisierte E-Mail mit Job-Description-PDF an den Kandidaten."""
    from app.services.outreach_service import OutreachService

    outreach = OutreachService(db)
    result = await outreach.send_to_candidate(match_id)

    if not result.get("success"):
        raise ConflictException(message=result.get("message", "Fehler beim Senden"))

    return result


@router.post(
    "/batch-send",
    summary="Mehrere Kandidaten anschreiben",
    description="Sendet E-Mails an mehrere Kandidaten gleichzeitig",
)
@rate_limit(RateLimitTier.AI)
async def batch_send(
    request: dict,
    db: AsyncSession = Depends(get_db),
):
    """Sendet E-Mails an mehrere Kandidaten (max 20 pro Batch)."""
    from app.services.outreach_service import OutreachService

    match_ids = request.get("match_ids", [])
    if not match_ids:
        raise ConflictException(message="Keine match_ids angegeben")
    if len(match_ids) > 20:
        raise ConflictException(message="Maximal 20 Matches pro Batch")

    # Strings zu UUIDs konvertieren
    uuids = [UUID(mid) if isinstance(mid, str) else mid for mid in match_ids]

    outreach = OutreachService(db)
    result = await outreach.batch_send(uuids)
    return result


@router.patch(
    "/{match_id}/outreach-status",
    summary="Outreach-Status updaten",
    description="Aktualisiert den Outreach-Status eines Matches (responded/interested/declined etc.)",
)
@rate_limit(RateLimitTier.WRITE)
async def update_outreach_status(
    match_id: UUID,
    request: dict,
    db: AsyncSession = Depends(get_db),
):
    """Aktualisiert den Outreach-Status eines Matches.

    Mögliche Status:
    - pending: Noch nicht gesendet
    - sent: E-Mail wurde gesendet
    - responded: Kandidat hat geantwortet
    - interested: Kandidat hat Interesse
    - declined: Kandidat hat abgelehnt
    - no_response: Keine Antwort nach X Tagen
    """
    from app.models.match import Match
    from datetime import datetime, timezone

    valid_outreach = {"pending", "sent", "responded", "interested", "declined", "no_response"}
    valid_presentation = {"not_sent", "presented", "interview", "rejected", "hired"}

    match = await db.get(Match, match_id)
    if not match:
        raise NotFoundException(message="Match nicht gefunden")

    outreach_status = request.get("outreach_status")
    presentation_status = request.get("presentation_status")
    feedback = request.get("candidate_feedback")

    if outreach_status:
        if outreach_status not in valid_outreach:
            raise ConflictException(message=f"Ungültiger outreach_status. Erlaubt: {valid_outreach}")
        match.outreach_status = outreach_status
        if outreach_status == "responded":
            match.outreach_responded_at = datetime.now(timezone.utc)

    if presentation_status:
        if presentation_status not in valid_presentation:
            raise ConflictException(message=f"Ungültiger presentation_status. Erlaubt: {valid_presentation}")
        match.presentation_status = presentation_status
        if presentation_status == "presented":
            match.presentation_sent_at = datetime.now(timezone.utc)

    if feedback is not None:
        match.candidate_feedback = feedback

    await db.commit()
    await db.refresh(match)

    return {
        "match_id": str(match.id),
        "outreach_status": match.outreach_status,
        "outreach_sent_at": match.outreach_sent_at.isoformat() if match.outreach_sent_at else None,
        "outreach_responded_at": match.outreach_responded_at.isoformat() if match.outreach_responded_at else None,
        "candidate_feedback": match.candidate_feedback,
        "presentation_status": match.presentation_status,
        "presentation_sent_at": match.presentation_sent_at.isoformat() if match.presentation_sent_at else None,
    }


@router.get(
    "/outreach/pipeline",
    summary="Outreach-Pipeline Übersicht",
    description="Zeigt den gesamten Outreach-Funnel: gesendet → geantwortet → interessiert → vorgestellt",
)
async def get_outreach_pipeline(
    db: AsyncSession = Depends(get_db),
):
    """Outreach-Pipeline: Funnel-Übersicht aller Matches im Outreach-Prozess."""
    from sqlalchemy import select, func, case
    from app.models.match import Match

    query = select(
        func.count().label("total"),
        func.count(Match.outreach_status).filter(Match.outreach_status == "sent").label("sent"),
        func.count(Match.outreach_status).filter(Match.outreach_status == "responded").label("responded"),
        func.count(Match.outreach_status).filter(Match.outreach_status == "interested").label("interested"),
        func.count(Match.outreach_status).filter(Match.outreach_status == "declined").label("declined"),
        func.count(Match.outreach_status).filter(Match.outreach_status == "no_response").label("no_response"),
        func.count(Match.presentation_status).filter(Match.presentation_status == "presented").label("presented"),
        func.count(Match.presentation_status).filter(Match.presentation_status == "interview").label("interview"),
        func.count(Match.presentation_status).filter(Match.presentation_status == "hired").label("hired"),
    ).where(Match.outreach_status.isnot(None))

    result = await db.execute(query)
    row = result.one()

    return {
        "funnel": {
            "sent": row.sent,
            "responded": row.responded,
            "interested": row.interested,
            "declined": row.declined,
            "no_response": row.no_response,
        },
        "presentation": {
            "presented": row.presented,
            "interview": row.interview,
            "hired": row.hired,
        },
        "total_outreach": row.total,
    }


@router.get(
    "/outreach/awaiting-response",
    summary="Wer hat nicht geantwortet?",
    description="Matches wo E-Mail gesendet aber keine Antwort erhalten",
)
async def get_awaiting_response(
    days: int = Query(default=3, ge=1, le=30, description="Seit wie vielen Tagen gesendet"),
    limit: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Zeigt Matches die seit X Tagen auf Antwort warten."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    from datetime import datetime, timezone, timedelta
    from app.models.match import Match

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    query = (
        select(Match)
        .options(selectinload(Match.job), selectinload(Match.candidate))
        .where(
            Match.outreach_status == "sent",
            Match.outreach_sent_at <= cutoff,
        )
        .order_by(Match.outreach_sent_at.asc())
        .limit(limit)
    )

    result = await db.execute(query)
    matches = result.scalars().all()

    return {
        "items": [
            {
                "match_id": str(m.id),
                "candidate_name": m.candidate.full_name if m.candidate else "Unbekannt",
                "candidate_email": m.candidate.email if m.candidate else None,
                "job_position": m.job.position if m.job else "Unbekannt",
                "job_company": m.job.company_name if m.job else "Unbekannt",
                "sent_at": m.outreach_sent_at.isoformat() if m.outreach_sent_at else None,
                "days_waiting": (datetime.now(timezone.utc) - m.outreach_sent_at).days if m.outreach_sent_at else 0,
            }
            for m in matches
        ],
        "total": len(matches),
        "filter_days": days,
    }


@router.get(
    "/outreach/interested",
    summary="Interessierte Kandidaten",
    description="Alle Kandidaten die Interesse gezeigt haben und bereit für Vorstellung sind",
)
async def get_interested_candidates(
    limit: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Zeigt alle Matches mit interessierten Kandidaten."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    from app.models.match import Match

    query = (
        select(Match)
        .options(selectinload(Match.job), selectinload(Match.candidate))
        .where(Match.outreach_status == "interested")
        .order_by(Match.outreach_responded_at.desc())
        .limit(limit)
    )

    result = await db.execute(query)
    matches = result.scalars().all()

    return {
        "items": [
            {
                "match_id": str(m.id),
                "candidate_name": m.candidate.full_name if m.candidate else "Unbekannt",
                "candidate_email": m.candidate.email if m.candidate else None,
                "candidate_city": m.candidate.city if m.candidate else None,
                "job_position": m.job.position if m.job else "Unbekannt",
                "job_company": m.job.company_name if m.job else "Unbekannt",
                "job_city": m.job.display_city if m.job else None,
                "candidate_feedback": m.candidate_feedback,
                "responded_at": m.outreach_responded_at.isoformat() if m.outreach_responded_at else None,
                "drive_time_car_min": m.drive_time_car_min,
                "presentation_status": m.presentation_status or "not_sent",
            }
            for m in matches
        ],
        "total": len(matches),
    }


@router.get(
    "/filter",
    summary="Flexibler Match-Filter",
    description="Filter nach Fahrzeit, Quality, Rolle, Score, Outreach-Status etc.",
)
async def filter_matches(
    max_drive_time: int | None = Query(default=None, ge=1, description="Max Fahrzeit Auto in Minuten"),
    min_score: float | None = Query(default=None, ge=0, description="Min Matching-Score"),
    role: str | None = Query(default=None, description="Job-Rolle (z.B. finanzbuchhalter)"),
    city: str | None = Query(default=None, description="Job-Stadt"),
    outreach_status: str | None = Query(default=None, description="Outreach-Status"),
    min_quality: str | None = Query(default=None, description="Min Quality (high/medium)"),
    no_response_days: int | None = Query(default=None, ge=1, description="Keine Antwort seit X Tagen"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """Flexibler Filter-Endpoint für Matches (für Claude Skill / Morning Briefing)."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    from datetime import datetime, timezone, timedelta
    from app.models.match import Match
    from app.models.job import Job

    query = (
        select(Match)
        .join(Match.job)
        .options(selectinload(Match.job), selectinload(Match.candidate))
    )

    # Filter anwenden
    conditions = []

    if max_drive_time:
        conditions.append(Match.drive_time_car_min <= max_drive_time)

    if min_score:
        conditions.append(Match.v2_score >= min_score)

    if role:
        # Case-insensitive Match auf classification_data.primary_role
        conditions.append(
            Job.classification_data["primary_role"].astext.ilike(f"%{role}%")
        )

    if city:
        conditions.append(
            Job.city.ilike(f"%{city}%") | Job.work_location_city.ilike(f"%{city}%")
        )

    if outreach_status:
        conditions.append(Match.outreach_status == outreach_status)

    if min_quality:
        if min_quality == "high":
            conditions.append(Job.quality_score == "high")
        elif min_quality == "medium":
            conditions.append(Job.quality_score.in_(["high", "medium"]))

    if no_response_days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=no_response_days)
        conditions.append(Match.outreach_status == "sent")
        conditions.append(Match.outreach_sent_at <= cutoff)

    if conditions:
        query = query.where(*conditions)

    # Sortierung: Score absteigend
    query = query.order_by(Match.v2_score.desc().nullslast()).offset(offset).limit(limit)

    result = await db.execute(query)
    matches = result.scalars().all()

    return {
        "items": [
            {
                "match_id": str(m.id),
                "candidate_name": m.candidate.full_name if m.candidate else "Unbekannt",
                "candidate_email": m.candidate.email if m.candidate else None,
                "candidate_city": m.candidate.city if m.candidate else None,
                "job_position": m.job.position if m.job else "Unbekannt",
                "job_company": m.job.company_name if m.job else "Unbekannt",
                "job_city": m.job.display_city if m.job else None,
                "score": round(m.v2_score, 1) if m.v2_score else None,
                "drive_time_car_min": m.drive_time_car_min,
                "drive_time_transit_min": m.drive_time_transit_min,
                "distance_km": round(m.distance_km, 1) if m.distance_km else None,
                "outreach_status": m.outreach_status,
                "quality": m.job.quality_score if m.job else None,
            }
            for m in matches
        ],
        "total": len(matches),
        "filters_applied": {
            k: v for k, v in {
                "max_drive_time": max_drive_time,
                "min_score": min_score,
                "role": role,
                "city": city,
                "outreach_status": outreach_status,
                "min_quality": min_quality,
                "no_response_days": no_response_days,
            }.items() if v is not None
        },
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
