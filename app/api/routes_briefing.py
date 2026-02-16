"""Morning Briefing Endpoints — Phase 13.

Optimiert für natürlichsprachliche Abfragen via Claude Skill.
Milad fragt morgens: "Was gibt es Neues?" → diese Endpoints liefern die Daten.

Endpoints:
- GET /briefing/overnight — Was ist seit gestern passiert?
- GET /briefing/top-matches — Beste unbearbeitete Matches
- GET /briefing/outreach-summary — Outreach-Funnel Zusammenfassung
- GET /briefing/action-items — Was muss Milad heute tun?
"""

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func, and_, or_, case
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.candidate import Candidate
from app.models.job import Job
from app.models.match import Match

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/briefing", tags=["Briefing"])


@router.get(
    "/overnight",
    summary="Was ist seit gestern passiert?",
    description="Zeigt alle Aktivitäten seit dem letzten Arbeitstag (neue Jobs, Matches, Klassifizierungen)",
)
async def get_overnight_briefing(
    hours: int = Query(default=24, ge=1, le=168, description="Zeitraum in Stunden"),
    db: AsyncSession = Depends(get_db),
):
    """Overnight Briefing: Was hat sich seit gestern geändert?"""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    # Neue Jobs
    new_jobs_query = select(func.count()).select_from(Job).where(
        Job.created_at >= cutoff,
        Job.deleted_at.is_(None),
    )
    new_jobs_result = await db.execute(new_jobs_query)
    new_jobs_count = new_jobs_result.scalar() or 0

    # Klassifizierte Jobs (mit quality_score)
    classified_jobs_query = select(
        func.count().label("total"),
        func.count().filter(Job.quality_score == "high").label("high"),
        func.count().filter(Job.quality_score == "medium").label("medium"),
        func.count().filter(Job.quality_score == "low").label("low"),
    ).select_from(Job).where(
        Job.created_at >= cutoff,
        Job.quality_score.isnot(None),
        Job.deleted_at.is_(None),
    )
    classified_result = await db.execute(classified_jobs_query)
    classified = classified_result.one()

    # Neue Matches
    new_matches_query = select(func.count()).select_from(Match).where(
        Match.created_at >= cutoff,
    )
    new_matches_result = await db.execute(new_matches_query)
    new_matches_count = new_matches_result.scalar() or 0

    # Top-Matches (Score > 75)
    top_matches_query = select(func.count()).select_from(Match).where(
        Match.created_at >= cutoff,
        Match.v2_score >= 75,
    )
    top_matches_result = await db.execute(top_matches_query)
    top_matches_count = top_matches_result.scalar() or 0

    # Neue Kandidaten
    new_candidates_query = select(func.count()).select_from(Candidate).where(
        Candidate.created_at >= cutoff,
    )
    new_candidates_result = await db.execute(new_candidates_query)
    new_candidates_count = new_candidates_result.scalar() or 0

    # Outreach-Antworten
    responses_query = select(func.count()).select_from(Match).where(
        Match.outreach_responded_at >= cutoff,
    )
    responses_result = await db.execute(responses_query)
    responses_count = responses_result.scalar() or 0

    return {
        "period_hours": hours,
        "since": cutoff.isoformat(),
        "summary": {
            "new_jobs": new_jobs_count,
            "classified_jobs": {
                "total": classified.total,
                "high": classified.high,
                "medium": classified.medium,
                "low": classified.low,
            },
            "new_matches": new_matches_count,
            "top_matches_score_75_plus": top_matches_count,
            "new_candidates": new_candidates_count,
            "outreach_responses": responses_count,
        },
    }


@router.get(
    "/top-matches",
    summary="Beste unbearbeitete Matches",
    description="Die besten Matches (höchster Score) die noch nicht angeschrieben wurden",
)
async def get_top_matches(
    min_score: float = Query(default=60, ge=0, le=100, description="Minimum Score"),
    limit: int = Query(default=10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
):
    """Top-Matches die noch nicht angeschrieben wurden."""
    query = (
        select(Match)
        .options(selectinload(Match.job), selectinload(Match.candidate))
        .where(
            Match.v2_score >= min_score,
            or_(
                Match.outreach_status.is_(None),
                Match.outreach_status == "pending",
            ),
        )
        .order_by(Match.v2_score.desc())
        .limit(limit)
    )

    result = await db.execute(query)
    matches = result.scalars().all()

    return {
        "items": [
            {
                "match_id": str(m.id),
                "score": round(m.v2_score, 1) if m.v2_score else None,
                "candidate_name": m.candidate.full_name if m.candidate else "Unbekannt",
                "candidate_email": m.candidate.email if m.candidate else None,
                "candidate_city": m.candidate.city if m.candidate else None,
                "job_position": m.job.position if m.job else "Unbekannt",
                "job_company": m.job.company_name if m.job else "Unbekannt",
                "job_city": m.job.display_city if m.job else None,
                "drive_time_car_min": m.drive_time_car_min,
                "drive_time_transit_min": m.drive_time_transit_min,
                "distance_km": round(m.distance_km, 1) if m.distance_km else None,
                "quality": m.job.quality_score if m.job else None,
            }
            for m in matches
        ],
        "total": len(matches),
        "min_score_filter": min_score,
    }


@router.get(
    "/outreach-summary",
    summary="Outreach-Funnel Zusammenfassung",
    description="Übersicht: Wie viele gesendet, geantwortet, interessiert, abgelehnt?",
)
async def get_outreach_summary(
    db: AsyncSession = Depends(get_db),
):
    """Outreach-Funnel Zusammenfassung für Morning Briefing."""
    # Funnel-Zahlen
    funnel_query = select(
        func.count().filter(Match.outreach_status == "sent").label("sent"),
        func.count().filter(Match.outreach_status == "responded").label("responded"),
        func.count().filter(Match.outreach_status == "interested").label("interested"),
        func.count().filter(Match.outreach_status == "declined").label("declined"),
        func.count().filter(Match.outreach_status == "no_response").label("no_response"),
    ).select_from(Match).where(Match.outreach_status.isnot(None))

    funnel_result = await db.execute(funnel_query)
    funnel = funnel_result.one()

    # Letzte Antworten (max 5)
    recent_responses_query = (
        select(Match)
        .options(selectinload(Match.job), selectinload(Match.candidate))
        .where(
            Match.outreach_status.in_(["responded", "interested", "declined"]),
            Match.outreach_responded_at.isnot(None),
        )
        .order_by(Match.outreach_responded_at.desc())
        .limit(5)
    )
    recent_result = await db.execute(recent_responses_query)
    recent_responses = recent_result.scalars().all()

    return {
        "funnel": {
            "sent": funnel.sent,
            "responded": funnel.responded,
            "interested": funnel.interested,
            "declined": funnel.declined,
            "no_response": funnel.no_response,
        },
        "recent_responses": [
            {
                "candidate_name": m.candidate.full_name if m.candidate else "Unbekannt",
                "job_position": m.job.position if m.job else "Unbekannt",
                "job_company": m.job.company_name if m.job else "Unbekannt",
                "status": m.outreach_status,
                "feedback": m.candidate_feedback,
                "responded_at": m.outreach_responded_at.isoformat() if m.outreach_responded_at else None,
            }
            for m in recent_responses
        ],
    }


@router.get(
    "/action-items",
    summary="Was muss heute getan werden?",
    description="Priorisierte Aktionsliste: Follow-ups, unbearbeitete Antworten, Top-Matches",
)
async def get_action_items(
    db: AsyncSession = Depends(get_db),
):
    """Tägliche Aktionsliste für Milad."""
    now = datetime.now(timezone.utc)
    items = []

    # 1. Unbearbeitete Antworten (responded aber nicht interested/declined)
    responded_query = (
        select(Match)
        .options(selectinload(Match.job), selectinload(Match.candidate))
        .where(Match.outreach_status == "responded")
        .order_by(Match.outreach_responded_at.asc())
        .limit(10)
    )
    responded_result = await db.execute(responded_query)
    responded_matches = responded_result.scalars().all()

    for m in responded_matches:
        items.append({
            "priority": "high",
            "type": "review_response",
            "message": f"{m.candidate.full_name if m.candidate else 'Kandidat'} hat auf Ihre E-Mail geantwortet ({m.job.position if m.job else 'Position'} bei {m.job.company_name if m.job else 'Unternehmen'})",
            "match_id": str(m.id),
            "action": "Antwort prüfen und Status auf 'interested' oder 'declined' setzen",
        })

    # 2. Follow-ups nötig (gesendet vor 3+ Tagen, keine Antwort)
    followup_cutoff = now - timedelta(days=3)
    followup_query = (
        select(func.count()).select_from(Match)
        .where(
            Match.outreach_status == "sent",
            Match.outreach_sent_at <= followup_cutoff,
        )
    )
    followup_result = await db.execute(followup_query)
    followup_count = followup_result.scalar() or 0

    if followup_count > 0:
        items.append({
            "priority": "medium",
            "type": "follow_up",
            "message": f"{followup_count} Kandidaten warten seit 3+ Tagen auf Antwort",
            "action": "Follow-up E-Mail senden oder als 'no_response' markieren",
        })

    # 3. Interessierte Kandidaten zur Vorstellung bereit
    interested_query = (
        select(func.count()).select_from(Match)
        .where(
            Match.outreach_status == "interested",
            or_(
                Match.presentation_status.is_(None),
                Match.presentation_status == "not_sent",
            ),
        )
    )
    interested_result = await db.execute(interested_query)
    interested_count = interested_result.scalar() or 0

    if interested_count > 0:
        items.append({
            "priority": "high",
            "type": "present_candidate",
            "message": f"{interested_count} Kandidaten sind interessiert und bereit für Vorstellung beim Kunden",
            "action": "Profil-PDF generieren und an Kunden senden",
        })

    # 4. Top-Matches noch nicht angeschrieben
    top_uncontacted_query = (
        select(func.count()).select_from(Match)
        .where(
            Match.v2_score >= 75,
            or_(
                Match.outreach_status.is_(None),
                Match.outreach_status == "pending",
            ),
        )
    )
    top_result = await db.execute(top_uncontacted_query)
    top_count = top_result.scalar() or 0

    if top_count > 0:
        items.append({
            "priority": "medium",
            "type": "outreach_new",
            "message": f"{top_count} Top-Matches (Score > 75) wurden noch nicht angeschrieben",
            "action": "Matches prüfen und E-Mails senden",
        })

    return {
        "date": now.strftime("%d.%m.%Y"),
        "action_items": items,
        "total_items": len(items),
    }
