"""Status & Query API Endpunkte — Zentrale Uebersicht fuer Geodaten, Profiling, Matches, Aufgaben, Anrufe."""

import logging
from datetime import date, datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import case, cast, func, select, Float
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.candidate import Candidate
from app.models.job import Job
from app.models.match import Match, MatchStatus
from app.models.ats_todo import ATSTodo, TodoPriority, TodoStatus
from app.models.ats_call_note import ATSCallNote, CallDirection, CallType
from app.models.company import Company

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/status", tags=["Status"])


# ══════════════════════════════════════════════════════════════════
# 1. GEODATEN
# ══════════════════════════════════════════════════════════════════

@router.get("/geodaten")
async def get_geodaten_status(db: AsyncSession = Depends(get_db)):
    """Geodaten-Coverage fuer Kandidaten und Jobs."""
    # Kandidaten
    cand_total = (await db.execute(select(func.count(Candidate.id)).where(Candidate.deleted_at.is_(None)))).scalar() or 0
    cand_with_coords = (await db.execute(
        select(func.count(Candidate.id)).where(
            Candidate.deleted_at.is_(None),
            Candidate.address_coords.isnot(None),
        )
    )).scalar() or 0
    cand_with_city_no_coords = (await db.execute(
        select(func.count(Candidate.id)).where(
            Candidate.deleted_at.is_(None),
            Candidate.city.isnot(None),
            Candidate.city != "",
            Candidate.address_coords.is_(None),
        )
    )).scalar() or 0
    cand_no_address = (await db.execute(
        select(func.count(Candidate.id)).where(
            Candidate.deleted_at.is_(None),
            (Candidate.city.is_(None)) | (Candidate.city == ""),
            Candidate.address_coords.is_(None),
        )
    )).scalar() or 0

    # Jobs
    job_total = (await db.execute(select(func.count(Job.id)).where(Job.deleted_at.is_(None)))).scalar() or 0
    job_with_coords = (await db.execute(
        select(func.count(Job.id)).where(
            Job.deleted_at.is_(None),
            Job.location_coords.isnot(None),
        )
    )).scalar() or 0
    job_with_city_no_coords = (await db.execute(
        select(func.count(Job.id)).where(
            Job.deleted_at.is_(None),
            ((Job.city.isnot(None)) & (Job.city != "")) | ((Job.work_location_city.isnot(None)) & (Job.work_location_city != "")),
            Job.location_coords.is_(None),
        )
    )).scalar() or 0

    cand_without = cand_total - cand_with_coords
    job_without = job_total - job_with_coords

    return {
        "candidates": {
            "total": cand_total,
            "with_coords": cand_with_coords,
            "without_coords": cand_without,
            "coverage_pct": round(cand_with_coords / cand_total * 100, 1) if cand_total > 0 else 0,
            "with_city_no_coords": cand_with_city_no_coords,
            "no_address_at_all": cand_no_address,
        },
        "jobs": {
            "total": job_total,
            "with_coords": job_with_coords,
            "without_coords": job_without,
            "coverage_pct": round(job_with_coords / job_total * 100, 1) if job_total > 0 else 0,
            "with_city_no_coords": job_with_city_no_coords,
        },
    }


# ══════════════════════════════════════════════════════════════════
# 2. PROFILING
# ══════════════════════════════════════════════════════════════════

@router.get("/profiling")
async def get_profiling_status(db: AsyncSession = Depends(get_db)):
    """Profiling-Coverage fuer Kandidaten und Jobs mit Level-Distribution."""
    # --- Kandidaten ---
    cand_total = (await db.execute(select(func.count(Candidate.id)).where(Candidate.deleted_at.is_(None)))).scalar() or 0
    cand_profiled = (await db.execute(
        select(func.count(Candidate.id)).where(
            Candidate.deleted_at.is_(None),
            Candidate.v2_profile_created_at.isnot(None),
        )
    )).scalar() or 0

    # Level-Distribution (Kandidaten)
    cand_level_rows = (await db.execute(
        select(Candidate.v2_seniority_level, func.count(Candidate.id))
        .where(
            Candidate.deleted_at.is_(None),
            Candidate.v2_seniority_level.isnot(None),
        )
        .group_by(Candidate.v2_seniority_level)
        .order_by(Candidate.v2_seniority_level)
    )).all()
    cand_level_dist = {str(row[0]): row[1] for row in cand_level_rows}

    # Trajectory-Distribution (Kandidaten)
    cand_traj_rows = (await db.execute(
        select(Candidate.v2_career_trajectory, func.count(Candidate.id))
        .where(
            Candidate.deleted_at.is_(None),
            Candidate.v2_career_trajectory.isnot(None),
        )
        .group_by(Candidate.v2_career_trajectory)
    )).all()
    cand_traj_dist = {row[0]: row[1] for row in cand_traj_rows}

    # Finance-Kandidaten (hotlist_category = 'Finanz- und Rechnungswesen' o.ae.)
    finance_total = (await db.execute(
        select(func.count(Candidate.id)).where(
            Candidate.deleted_at.is_(None),
            Candidate.hotlist_category.ilike("%finanz%"),
        )
    )).scalar() or 0
    finance_profiled = (await db.execute(
        select(func.count(Candidate.id)).where(
            Candidate.deleted_at.is_(None),
            Candidate.hotlist_category.ilike("%finanz%"),
            Candidate.v2_profile_created_at.isnot(None),
        )
    )).scalar() or 0

    # --- Jobs ---
    job_total = (await db.execute(select(func.count(Job.id)).where(Job.deleted_at.is_(None)))).scalar() or 0
    job_profiled = (await db.execute(
        select(func.count(Job.id)).where(
            Job.deleted_at.is_(None),
            Job.v2_profile_created_at.isnot(None),
        )
    )).scalar() or 0

    # Level-Distribution (Jobs)
    job_level_rows = (await db.execute(
        select(Job.v2_seniority_level, func.count(Job.id))
        .where(
            Job.deleted_at.is_(None),
            Job.v2_seniority_level.isnot(None),
        )
        .group_by(Job.v2_seniority_level)
        .order_by(Job.v2_seniority_level)
    )).all()
    job_level_dist = {str(row[0]): row[1] for row in job_level_rows}

    # Backfill-Status (aus routes_matching_v2 importieren)
    try:
        from app.api.routes_matching_v2 import _backfill_status
        backfill = dict(_backfill_status)
    except ImportError:
        backfill = {"running": False, "info": "import_failed"}

    cand_missing = cand_total - cand_profiled
    job_missing = job_total - job_profiled

    return {
        "candidates": {
            "total": cand_total,
            "profiled": cand_profiled,
            "missing": cand_missing,
            "coverage_pct": round(cand_profiled / cand_total * 100, 1) if cand_total > 0 else 0,
            "finance_total": finance_total,
            "finance_profiled": finance_profiled,
            "level_distribution": cand_level_dist,
            "trajectory_distribution": cand_traj_dist,
        },
        "jobs": {
            "total": job_total,
            "profiled": job_profiled,
            "missing": job_missing,
            "coverage_pct": round(job_profiled / job_total * 100, 1) if job_total > 0 else 0,
            "level_distribution": job_level_dist,
        },
        "backfill_status": backfill,
    }


# ══════════════════════════════════════════════════════════════════
# 3. MATCHES
# ══════════════════════════════════════════════════════════════════

@router.get("/matches")
async def get_matches_status(db: AsyncSession = Depends(get_db)):
    """Match-Statistiken — Score-Verteilung, Status, Distanz."""
    total = (await db.execute(select(func.count(Match.id)))).scalar() or 0

    # V2 Matches (mit v2_score)
    v2_total = (await db.execute(
        select(func.count(Match.id)).where(Match.v2_score.isnot(None))
    )).scalar() or 0

    # Score-Distribution
    score_dist_result = (await db.execute(
        select(
            func.sum(case((Match.v2_score >= 90, 1), else_=0)).label("s90_100"),
            func.sum(case(((Match.v2_score >= 80) & (Match.v2_score < 90), 1), else_=0)).label("s80_89"),
            func.sum(case(((Match.v2_score >= 70) & (Match.v2_score < 80), 1), else_=0)).label("s70_79"),
            func.sum(case(((Match.v2_score >= 60) & (Match.v2_score < 70), 1), else_=0)).label("s60_69"),
            func.sum(case((Match.v2_score < 60, 1), else_=0)).label("below_60"),
        ).where(Match.v2_score.isnot(None))
    )).one()

    # Avg Score
    avg_score = (await db.execute(
        select(func.avg(Match.v2_score)).where(Match.v2_score.isnot(None))
    )).scalar()

    # Status-Verteilung
    status_rows = (await db.execute(
        select(Match.status, func.count(Match.id))
        .group_by(Match.status)
    )).all()
    status_dist = {row[0].value if hasattr(row[0], 'value') else str(row[0]): row[1] for row in status_rows}

    # Distanz
    matches_with_dist = (await db.execute(
        select(func.count(Match.id)).where(Match.distance_km.isnot(None))
    )).scalar() or 0
    avg_dist = (await db.execute(
        select(func.avg(Match.distance_km)).where(Match.distance_km.isnot(None))
    )).scalar()

    return {
        "total_matches": total,
        "v2_matches": v2_total,
        "score_distribution": {
            "90_100": score_dist_result.s90_100 or 0,
            "80_89": score_dist_result.s80_89 or 0,
            "70_79": score_dist_result.s70_79 or 0,
            "60_69": score_dist_result.s60_69 or 0,
            "below_60": score_dist_result.below_60 or 0,
        },
        "avg_v2_score": round(float(avg_score), 1) if avg_score else 0,
        "matches_by_status": status_dist,
        "matches_with_distance": matches_with_dist,
        "avg_distance_km": round(float(avg_dist), 1) if avg_dist else 0,
    }


@router.get("/matches/query")
async def query_matches(
    candidate_id: UUID | None = Query(None, description="Filter auf Kandidat"),
    job_id: UUID | None = Query(None, description="Filter auf Job"),
    min_score: float = Query(0, description="Mindest-Score (0-100)"),
    max_score: float = Query(100, description="Max-Score (0-100)"),
    days: int = Query(365, description="Zeitraum in Tagen"),
    status: str | None = Query(None, description="Match-Status (new, ai_checked, presented, rejected, placed)"),
    sort_by: str = Query("score", description="Sortierung: score oder date"),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Flexible Match-Abfrage mit Filtern."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    # Base query mit JOINs fuer Namen
    query = (
        select(
            Match.id,
            Match.v2_score,
            Match.v2_score_breakdown,
            Match.distance_km,
            Match.status,
            Match.v2_matched_at,
            Match.created_at,
            Match.candidate_id,
            Match.job_id,
            Job.position.label("job_title"),
            Job.company_name.label("company"),
            Candidate.first_name,
            Candidate.last_name,
        )
        .outerjoin(Job, Match.job_id == Job.id)
        .outerjoin(Candidate, Match.candidate_id == Candidate.id)
        .where(Match.v2_score.isnot(None))
    )

    count_query = select(func.count(Match.id)).where(Match.v2_score.isnot(None))

    # Filter
    if candidate_id:
        query = query.where(Match.candidate_id == candidate_id)
        count_query = count_query.where(Match.candidate_id == candidate_id)
    if job_id:
        query = query.where(Match.job_id == job_id)
        count_query = count_query.where(Match.job_id == job_id)
    if min_score > 0:
        query = query.where(Match.v2_score >= min_score)
        count_query = count_query.where(Match.v2_score >= min_score)
    if max_score < 100:
        query = query.where(Match.v2_score <= max_score)
        count_query = count_query.where(Match.v2_score <= max_score)
    if status:
        match_status = MatchStatus(status)
        query = query.where(Match.status == match_status)
        count_query = count_query.where(Match.status == match_status)

    # Zeitraum: v2_matched_at oder created_at
    query = query.where(
        func.coalesce(Match.v2_matched_at, Match.created_at) >= cutoff
    )
    count_query = count_query.where(
        func.coalesce(Match.v2_matched_at, Match.created_at) >= cutoff
    )

    # Sortierung
    if sort_by == "date":
        query = query.order_by(func.coalesce(Match.v2_matched_at, Match.created_at).desc())
    else:
        query = query.order_by(Match.v2_score.desc())

    # Total
    total = (await db.execute(count_query)).scalar() or 0

    # Pagination
    offset = (page - 1) * per_page
    query = query.offset(offset).limit(per_page)

    rows = (await db.execute(query)).all()

    items = []
    for row in rows:
        candidate_name = " ".join(filter(None, [row.first_name, row.last_name])) or "Unbekannt"
        items.append({
            "match_id": str(row.id),
            "job_title": row.job_title or "Unbekannt",
            "company": row.company or "Unbekannt",
            "candidate_name": candidate_name,
            "candidate_id": str(row.candidate_id) if row.candidate_id else None,
            "job_id": str(row.job_id) if row.job_id else None,
            "v2_score": round(float(row.v2_score), 1) if row.v2_score else 0,
            "v2_score_breakdown": row.v2_score_breakdown,
            "distance_km": round(float(row.distance_km), 1) if row.distance_km else None,
            "status": row.status.value if hasattr(row.status, 'value') else str(row.status),
            "matched_at": row.v2_matched_at.isoformat() if row.v2_matched_at else (row.created_at.isoformat() if row.created_at else None),
        })

    filters_applied = {}
    if candidate_id:
        filters_applied["candidate_id"] = str(candidate_id)
    if job_id:
        filters_applied["job_id"] = str(job_id)
    if min_score > 0:
        filters_applied["min_score"] = min_score
    if max_score < 100:
        filters_applied["max_score"] = max_score
    if status:
        filters_applied["status"] = status
    filters_applied["days"] = days

    return {
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page if per_page > 0 else 0,
        "filters_applied": filters_applied,
    }


# ══════════════════════════════════════════════════════════════════
# 4. AUFGABEN (ATS Todos)
# ══════════════════════════════════════════════════════════════════

@router.get("/aufgaben")
async def get_aufgaben_status(db: AsyncSession = Depends(get_db)):
    """Aufgaben-Uebersicht mit Status- und Prioritaetsverteilung."""
    total = (await db.execute(select(func.count(ATSTodo.id)))).scalar() or 0
    open_count = (await db.execute(
        select(func.count(ATSTodo.id)).where(ATSTodo.status == TodoStatus.OPEN)
    )).scalar() or 0
    in_progress = (await db.execute(
        select(func.count(ATSTodo.id)).where(ATSTodo.status == TodoStatus.IN_PROGRESS)
    )).scalar() or 0
    done = (await db.execute(
        select(func.count(ATSTodo.id)).where(ATSTodo.status == TodoStatus.DONE)
    )).scalar() or 0
    cancelled = (await db.execute(
        select(func.count(ATSTodo.id)).where(ATSTodo.status == TodoStatus.CANCELLED)
    )).scalar() or 0

    today = date.today()
    overdue = (await db.execute(
        select(func.count(ATSTodo.id)).where(
            ATSTodo.due_date < today,
            ATSTodo.status.in_([TodoStatus.OPEN, TodoStatus.IN_PROGRESS]),
        )
    )).scalar() or 0
    due_today = (await db.execute(
        select(func.count(ATSTodo.id)).where(
            ATSTodo.due_date == today,
            ATSTodo.status.in_([TodoStatus.OPEN, TodoStatus.IN_PROGRESS]),
        )
    )).scalar() or 0

    # Priority-Distribution
    prio_rows = (await db.execute(
        select(ATSTodo.priority, func.count(ATSTodo.id))
        .where(ATSTodo.status.in_([TodoStatus.OPEN, TodoStatus.IN_PROGRESS]))
        .group_by(ATSTodo.priority)
    )).all()
    prio_dist = {row[0].value if hasattr(row[0], 'value') else str(row[0]): row[1] for row in prio_rows}

    return {
        "total": total,
        "open": open_count,
        "in_progress": in_progress,
        "done": done,
        "cancelled": cancelled,
        "overdue": overdue,
        "due_today": due_today,
        "by_priority": prio_dist,
    }


@router.get("/aufgaben/query")
async def query_aufgaben(
    candidate_id: UUID | None = Query(None),
    contact_id: UUID | None = Query(None),
    company_id: UUID | None = Query(None),
    ats_job_id: UUID | None = Query(None),
    status: str | None = Query(None, description="open, in_progress, done, cancelled"),
    priority: str | None = Query(None, description="unwichtig, mittelmaessig, wichtig, dringend, sehr_dringend"),
    days: int = Query(365, description="Zeitraum in Tagen"),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Flexible Aufgaben-Abfrage mit Filtern nach Kandidat, Kontakt, Unternehmen, Job."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    query = (
        select(
            ATSTodo.id,
            ATSTodo.title,
            ATSTodo.description,
            ATSTodo.status,
            ATSTodo.priority,
            ATSTodo.due_date,
            ATSTodo.completed_at,
            ATSTodo.created_at,
            ATSTodo.candidate_id,
            ATSTodo.company_id,
            ATSTodo.contact_id,
            ATSTodo.ats_job_id,
        )
        .where(ATSTodo.created_at >= cutoff)
    )
    count_query = select(func.count(ATSTodo.id)).where(ATSTodo.created_at >= cutoff)

    # Filter
    if candidate_id:
        query = query.where(ATSTodo.candidate_id == candidate_id)
        count_query = count_query.where(ATSTodo.candidate_id == candidate_id)
    if contact_id:
        query = query.where(ATSTodo.contact_id == contact_id)
        count_query = count_query.where(ATSTodo.contact_id == contact_id)
    if company_id:
        query = query.where(ATSTodo.company_id == company_id)
        count_query = count_query.where(ATSTodo.company_id == company_id)
    if ats_job_id:
        query = query.where(ATSTodo.ats_job_id == ats_job_id)
        count_query = count_query.where(ATSTodo.ats_job_id == ats_job_id)
    if status:
        todo_status = TodoStatus(status)
        query = query.where(ATSTodo.status == todo_status)
        count_query = count_query.where(ATSTodo.status == todo_status)
    if priority:
        todo_prio = TodoPriority(priority)
        query = query.where(ATSTodo.priority == todo_prio)
        count_query = count_query.where(ATSTodo.priority == todo_prio)

    # Sortierung
    query = query.order_by(ATSTodo.status.asc(), ATSTodo.priority.desc(), ATSTodo.due_date.asc().nullslast())

    # Total
    total = (await db.execute(count_query)).scalar() or 0

    # Pagination
    offset = (page - 1) * per_page
    query = query.offset(offset).limit(per_page)

    rows = (await db.execute(query)).all()

    items = []
    for row in rows:
        items.append({
            "todo_id": str(row.id),
            "title": row.title,
            "description": row.description,
            "status": row.status.value if hasattr(row.status, 'value') else str(row.status),
            "priority": row.priority.value if hasattr(row.priority, 'value') else str(row.priority),
            "due_date": row.due_date.isoformat() if row.due_date else None,
            "completed_at": row.completed_at.isoformat() if row.completed_at else None,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "candidate_id": str(row.candidate_id) if row.candidate_id else None,
            "company_id": str(row.company_id) if row.company_id else None,
            "contact_id": str(row.contact_id) if row.contact_id else None,
            "ats_job_id": str(row.ats_job_id) if row.ats_job_id else None,
            "is_overdue": row.due_date < date.today() if row.due_date and row.status in (TodoStatus.OPEN, TodoStatus.IN_PROGRESS) else False,
        })

    return {
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page if per_page > 0 else 0,
    }


# ══════════════════════════════════════════════════════════════════
# 5. ANRUFPROTOKOLLE (ATS Call Notes)
# ══════════════════════════════════════════════════════════════════

@router.get("/anrufe")
async def get_anrufe_status(db: AsyncSession = Depends(get_db)):
    """Anruf-Uebersicht mit Typ-, Richtungs- und Dauerstatistiken."""
    total = (await db.execute(select(func.count(ATSCallNote.id)))).scalar() or 0

    # By Type
    type_rows = (await db.execute(
        select(ATSCallNote.call_type, func.count(ATSCallNote.id))
        .group_by(ATSCallNote.call_type)
    )).all()
    type_dist = {row[0].value if hasattr(row[0], 'value') else str(row[0]): row[1] for row in type_rows}

    # By Direction
    dir_rows = (await db.execute(
        select(ATSCallNote.direction, func.count(ATSCallNote.id))
        .group_by(ATSCallNote.direction)
    )).all()
    dir_dist = {(row[0].value if hasattr(row[0], 'value') else str(row[0])): row[1] for row in dir_rows if row[0] is not None}

    # Duration
    total_minutes = (await db.execute(
        select(func.sum(ATSCallNote.duration_minutes)).where(ATSCallNote.duration_minutes.isnot(None))
    )).scalar() or 0
    avg_minutes = (await db.execute(
        select(func.avg(ATSCallNote.duration_minutes)).where(ATSCallNote.duration_minutes.isnot(None))
    )).scalar()

    # Letzte 7 / 30 Tage
    now = datetime.now(timezone.utc)
    calls_7d = (await db.execute(
        select(func.count(ATSCallNote.id)).where(ATSCallNote.called_at >= now - timedelta(days=7))
    )).scalar() or 0
    calls_30d = (await db.execute(
        select(func.count(ATSCallNote.id)).where(ATSCallNote.called_at >= now - timedelta(days=30))
    )).scalar() or 0

    return {
        "total": total,
        "by_type": type_dist,
        "by_direction": dir_dist,
        "total_duration_minutes": total_minutes,
        "avg_duration_minutes": round(float(avg_minutes), 1) if avg_minutes else 0,
        "calls_last_7_days": calls_7d,
        "calls_last_30_days": calls_30d,
    }


@router.get("/anrufe/query")
async def query_anrufe(
    candidate_id: UUID | None = Query(None),
    contact_id: UUID | None = Query(None),
    company_id: UUID | None = Query(None),
    ats_job_id: UUID | None = Query(None),
    call_type: str | None = Query(None, description="acquisition, qualification, followup, candidate_call"),
    direction: str | None = Query(None, description="outbound, inbound"),
    days: int = Query(365, description="Zeitraum in Tagen"),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Flexible Anruf-Abfrage mit Filtern nach Kandidat, Kontakt, Unternehmen, Job."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    query = (
        select(
            ATSCallNote.id,
            ATSCallNote.call_type,
            ATSCallNote.direction,
            ATSCallNote.summary,
            ATSCallNote.duration_minutes,
            ATSCallNote.called_at,
            ATSCallNote.created_at,
            ATSCallNote.candidate_id,
            ATSCallNote.company_id,
            ATSCallNote.contact_id,
            ATSCallNote.ats_job_id,
            ATSCallNote.action_items,
        )
        .where(ATSCallNote.called_at >= cutoff)
    )
    count_query = select(func.count(ATSCallNote.id)).where(ATSCallNote.called_at >= cutoff)

    # Filter
    if candidate_id:
        query = query.where(ATSCallNote.candidate_id == candidate_id)
        count_query = count_query.where(ATSCallNote.candidate_id == candidate_id)
    if contact_id:
        query = query.where(ATSCallNote.contact_id == contact_id)
        count_query = count_query.where(ATSCallNote.contact_id == contact_id)
    if company_id:
        query = query.where(ATSCallNote.company_id == company_id)
        count_query = count_query.where(ATSCallNote.company_id == company_id)
    if ats_job_id:
        query = query.where(ATSCallNote.ats_job_id == ats_job_id)
        count_query = count_query.where(ATSCallNote.ats_job_id == ats_job_id)
    if call_type:
        ct = CallType(call_type)
        query = query.where(ATSCallNote.call_type == ct)
        count_query = count_query.where(ATSCallNote.call_type == ct)
    if direction:
        cd = CallDirection(direction)
        query = query.where(ATSCallNote.direction == cd)
        count_query = count_query.where(ATSCallNote.direction == cd)

    # Sortierung
    query = query.order_by(ATSCallNote.called_at.desc())

    # Total
    total = (await db.execute(count_query)).scalar() or 0

    # Pagination
    offset = (page - 1) * per_page
    query = query.offset(offset).limit(per_page)

    rows = (await db.execute(query)).all()

    items = []
    for row in rows:
        items.append({
            "call_id": str(row.id),
            "call_type": row.call_type.value if hasattr(row.call_type, 'value') else str(row.call_type),
            "direction": row.direction.value if row.direction and hasattr(row.direction, 'value') else (str(row.direction) if row.direction else None),
            "summary": row.summary,
            "duration_minutes": row.duration_minutes,
            "called_at": row.called_at.isoformat() if row.called_at else None,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "candidate_id": str(row.candidate_id) if row.candidate_id else None,
            "company_id": str(row.company_id) if row.company_id else None,
            "contact_id": str(row.contact_id) if row.contact_id else None,
            "ats_job_id": str(row.ats_job_id) if row.ats_job_id else None,
            "action_items": row.action_items,
        })

    return {
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page if per_page > 0 else 0,
    }


# ══════════════════════════════════════════════════════════════════
# 6. GESAMTUEBERSICHT
# ══════════════════════════════════════════════════════════════════

@router.get("/overview")
async def get_overview(db: AsyncSession = Depends(get_db)):
    """Kompakte Gesamtuebersicht aller Systembereiche."""
    # Geodaten (kompakt)
    cand_total = (await db.execute(select(func.count(Candidate.id)).where(Candidate.deleted_at.is_(None)))).scalar() or 0
    cand_with_coords = (await db.execute(
        select(func.count(Candidate.id)).where(Candidate.deleted_at.is_(None), Candidate.address_coords.isnot(None))
    )).scalar() or 0
    job_total = (await db.execute(select(func.count(Job.id)).where(Job.deleted_at.is_(None)))).scalar() or 0
    job_with_coords = (await db.execute(
        select(func.count(Job.id)).where(Job.deleted_at.is_(None), Job.location_coords.isnot(None))
    )).scalar() or 0

    # Profiling (kompakt)
    cand_profiled = (await db.execute(
        select(func.count(Candidate.id)).where(Candidate.deleted_at.is_(None), Candidate.v2_profile_created_at.isnot(None))
    )).scalar() or 0
    job_profiled = (await db.execute(
        select(func.count(Job.id)).where(Job.deleted_at.is_(None), Job.v2_profile_created_at.isnot(None))
    )).scalar() or 0

    # Matches (kompakt)
    match_total = (await db.execute(select(func.count(Match.id)))).scalar() or 0
    avg_score = (await db.execute(
        select(func.avg(Match.v2_score)).where(Match.v2_score.isnot(None))
    )).scalar()

    # Aufgaben (kompakt)
    today = date.today()
    todo_open = (await db.execute(
        select(func.count(ATSTodo.id)).where(ATSTodo.status.in_([TodoStatus.OPEN, TodoStatus.IN_PROGRESS]))
    )).scalar() or 0
    todo_overdue = (await db.execute(
        select(func.count(ATSTodo.id)).where(
            ATSTodo.due_date < today,
            ATSTodo.status.in_([TodoStatus.OPEN, TodoStatus.IN_PROGRESS]),
        )
    )).scalar() or 0

    # Anrufe (kompakt)
    now = datetime.now(timezone.utc)
    call_total = (await db.execute(select(func.count(ATSCallNote.id)))).scalar() or 0
    calls_7d = (await db.execute(
        select(func.count(ATSCallNote.id)).where(ATSCallNote.called_at >= now - timedelta(days=7))
    )).scalar() or 0

    return {
        "geodaten": {
            "candidates_total": cand_total,
            "candidates_with_coords": cand_with_coords,
            "candidates_coverage_pct": round(cand_with_coords / cand_total * 100, 1) if cand_total > 0 else 0,
            "jobs_total": job_total,
            "jobs_with_coords": job_with_coords,
            "jobs_coverage_pct": round(job_with_coords / job_total * 100, 1) if job_total > 0 else 0,
        },
        "profiling": {
            "candidates_total": cand_total,
            "candidates_profiled": cand_profiled,
            "candidates_coverage_pct": round(cand_profiled / cand_total * 100, 1) if cand_total > 0 else 0,
            "jobs_total": job_total,
            "jobs_profiled": job_profiled,
            "jobs_coverage_pct": round(job_profiled / job_total * 100, 1) if job_total > 0 else 0,
        },
        "matches": {
            "total": match_total,
            "avg_v2_score": round(float(avg_score), 1) if avg_score else 0,
        },
        "aufgaben": {
            "open": todo_open,
            "overdue": todo_overdue,
        },
        "anrufe": {
            "total": call_total,
            "last_7_days": calls_7d,
        },
    }


# ══════════════════════════════════════════════════════════════════
# 7. HTML PARTIAL FUER EINSTELLUNGEN
# ══════════════════════════════════════════════════════════════════

def _progress_bar(label: str, current: int, total: int, color: str = "primary") -> str:
    """Erzeugt eine Fortschrittsbalken-Zeile als HTML."""
    pct = round(current / total * 100, 1) if total > 0 else 0
    return f"""
    <div class="flex items-center justify-between text-sm mb-1">
        <span class="text-gray-600">{label}</span>
        <span class="font-medium text-gray-900">{current:,} / {total:,} ({pct}%)</span>
    </div>
    <div class="w-full bg-gray-200 rounded-full h-2.5 mb-3">
        <div class="bg-{color}-600 h-2.5 rounded-full transition-all" style="width: {min(pct, 100)}%"></div>
    </div>
    """


def _stat_card(label: str, value: str, sub: str = "") -> str:
    """Erzeugt eine kleine Stat-Karte."""
    sub_html = f'<span class="text-xs text-gray-400 mt-0.5">{sub}</span>' if sub else ""
    return f"""
    <div class="bg-gray-50 rounded-lg p-3 text-center">
        <div class="text-lg font-bold text-gray-900">{value}</div>
        <div class="text-xs text-gray-500">{label}</div>
        {sub_html}
    </div>
    """


@router.get("/system-html", response_class=HTMLResponse)
async def get_system_status_html(db: AsyncSession = Depends(get_db)):
    """HTML-Partial fuer die Einstellungsseite — System-Status auf einen Blick."""
    # --- Daten sammeln (kompakt, wie overview) ---
    cand_total = (await db.execute(select(func.count(Candidate.id)).where(Candidate.deleted_at.is_(None)))).scalar() or 0
    cand_with_coords = (await db.execute(
        select(func.count(Candidate.id)).where(Candidate.deleted_at.is_(None), Candidate.address_coords.isnot(None))
    )).scalar() or 0
    cand_profiled = (await db.execute(
        select(func.count(Candidate.id)).where(Candidate.deleted_at.is_(None), Candidate.v2_profile_created_at.isnot(None))
    )).scalar() or 0

    job_total = (await db.execute(select(func.count(Job.id)).where(Job.deleted_at.is_(None)))).scalar() or 0
    job_with_coords = (await db.execute(
        select(func.count(Job.id)).where(Job.deleted_at.is_(None), Job.location_coords.isnot(None))
    )).scalar() or 0
    job_profiled = (await db.execute(
        select(func.count(Job.id)).where(Job.deleted_at.is_(None), Job.v2_profile_created_at.isnot(None))
    )).scalar() or 0

    match_total = (await db.execute(select(func.count(Match.id)))).scalar() or 0
    avg_score = (await db.execute(
        select(func.avg(Match.v2_score)).where(Match.v2_score.isnot(None))
    )).scalar()

    today = date.today()
    todo_open = (await db.execute(
        select(func.count(ATSTodo.id)).where(ATSTodo.status.in_([TodoStatus.OPEN, TodoStatus.IN_PROGRESS]))
    )).scalar() or 0
    todo_overdue = (await db.execute(
        select(func.count(ATSTodo.id)).where(
            ATSTodo.due_date < today,
            ATSTodo.status.in_([TodoStatus.OPEN, TodoStatus.IN_PROGRESS]),
        )
    )).scalar() or 0

    now = datetime.now(timezone.utc)
    call_total = (await db.execute(select(func.count(ATSCallNote.id)))).scalar() or 0
    calls_7d = (await db.execute(
        select(func.count(ATSCallNote.id)).where(ATSCallNote.called_at >= now - timedelta(days=7))
    )).scalar() or 0

    # Backfill-Status
    try:
        from app.api.routes_matching_v2 import _backfill_status
        bf = dict(_backfill_status)
    except ImportError:
        bf = {"running": False}

    # Level-Distribution
    cand_level_rows = (await db.execute(
        select(Candidate.v2_seniority_level, func.count(Candidate.id))
        .where(Candidate.deleted_at.is_(None), Candidate.v2_seniority_level.isnot(None))
        .group_by(Candidate.v2_seniority_level)
        .order_by(Candidate.v2_seniority_level)
    )).all()

    # --- HTML bauen ---
    avg_str = f"{round(float(avg_score), 1)}" if avg_score else "—"

    # Backfill-Anzeige
    backfill_html = ""
    if bf.get("running"):
        bf_pct = round(bf["processed"] / bf["total"] * 100, 1) if bf.get("total", 0) > 0 else 0
        backfill_html = f"""
        <div class="bg-yellow-50 border border-yellow-200 rounded-lg p-3 mb-4">
            <div class="flex items-center gap-2 mb-2">
                <div class="animate-spin h-4 w-4 border-2 border-yellow-600 border-t-transparent rounded-full"></div>
                <span class="font-medium text-yellow-800">Profiling laeuft: {bf.get('type', 'unbekannt')}</span>
            </div>
            <div class="flex items-center justify-between text-sm mb-1">
                <span class="text-yellow-700">{bf['processed']} / {bf['total']}</span>
                <span class="font-medium text-yellow-800">{bf_pct}%</span>
            </div>
            <div class="w-full bg-yellow-200 rounded-full h-2">
                <div class="bg-yellow-600 h-2 rounded-full transition-all" style="width: {min(bf_pct, 100)}%"></div>
            </div>
            <div class="text-xs text-yellow-600 mt-1">Kosten bisher: ${bf.get('cost_usd', 0):.4f}</div>
        </div>
        """

    # Level-Badges
    level_labels = {1: "Einsteiger", 2: "Junior", 3: "Fachkraft", 4: "Senior", 5: "Experte", 6: "Leiter"}
    level_colors = {1: "gray", 2: "blue", 3: "green", 4: "amber", 5: "orange", 6: "red"}
    level_badges_html = ""
    for level, count in cand_level_rows:
        lbl = level_labels.get(level, f"Level {level}")
        level_badges_html += f"""
        <div class="flex items-center justify-between py-1 text-sm">
            <span class="inline-flex items-center gap-1.5">
                <span class="w-5 h-5 rounded-full bg-{level_colors.get(level, 'gray')}-100 text-{level_colors.get(level, 'gray')}-700 text-xs font-bold flex items-center justify-center">{level}</span>
                <span class="text-gray-600">{lbl}</span>
            </span>
            <span class="font-medium text-gray-900">{count:,}</span>
        </div>
        """

    html = f"""
    <div class="space-y-5">
        {backfill_html}

        <!-- Uebersicht-Karten -->
        <div class="grid grid-cols-2 sm:grid-cols-4 gap-3">
            {_stat_card("Kandidaten", f"{cand_total:,}")}
            {_stat_card("Jobs", f"{job_total:,}")}
            {_stat_card("Matches", f"{match_total:,}", f"&Oslash; {avg_str}%")}
            {_stat_card("Offene Aufgaben", str(todo_open), f"{todo_overdue} ueberfaellig" if todo_overdue > 0 else "")}
        </div>

        <!-- Geodaten Coverage -->
        <div>
            <h3 class="text-sm font-semibold text-gray-700 mb-2">Geodaten-Coverage</h3>
            {_progress_bar("Kandidaten", cand_with_coords, cand_total, "blue")}
            {_progress_bar("Jobs", job_with_coords, job_total, "green")}
        </div>

        <!-- Profiling Coverage -->
        <div>
            <h3 class="text-sm font-semibold text-gray-700 mb-2">Profiling-Coverage</h3>
            {_progress_bar("Kandidaten", cand_profiled, cand_total, "indigo")}
            {_progress_bar("Jobs", job_profiled, job_total, "emerald")}
        </div>

        <!-- Level-Distribution -->
        <div>
            <h3 class="text-sm font-semibold text-gray-700 mb-2">Level-Verteilung (Kandidaten)</h3>
            <div class="bg-gray-50 rounded-lg p-3">
                {level_badges_html if level_badges_html else '<span class="text-sm text-gray-400">Noch keine Level-Daten</span>'}
            </div>
        </div>

        <!-- Anrufe -->
        <div class="flex items-center justify-between text-sm text-gray-500 pt-2 border-t border-gray-100">
            <span>Anrufe gesamt: <strong class="text-gray-900">{call_total}</strong></span>
            <span>Letzte 7 Tage: <strong class="text-gray-900">{calls_7d}</strong></span>
        </div>
    </div>
    """
    return HTMLResponse(content=html)


# ══════════════════════════════════════════════════════════════════
# 8. KLASSIFIZIERUNG LIVE-STATUS (HTML-Seite)
# ══════════════════════════════════════════════════════════════════

@router.get("/klassifizierung", response_class=HTMLResponse)
async def klassifizierung_live_status():
    """Standalone HTML-Seite die beide Klassifizierungs-Endpoints pollt."""
    html = """<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Klassifizierung — Live-Status</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
  body { font-family: 'Inter', system-ui, sans-serif; background: #09090B; color: #fafafa; }
  .card { background: #18181B; border: 1px solid #27272A; border-radius: 12px; padding: 24px; }
  .progress-bg { background: #27272A; border-radius: 9999px; height: 10px; overflow: hidden; }
  .progress-fill { height: 100%; border-radius: 9999px; transition: width 0.5s ease; }
  .pulse { animation: pulse 2s cubic-bezier(0.4, 0, 0.6, 1) infinite; }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.5; } }
  .spinner { animation: spin 1s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body class="min-h-screen p-6">
<div class="max-w-3xl mx-auto space-y-6">
  <div class="flex items-center gap-3 mb-2">
    <h1 class="text-2xl font-bold">Klassifizierung — Live-Status</h1>
    <span id="auto-refresh" class="text-xs text-zinc-500">Auto-Refresh alle 3s</span>
  </div>

  <!-- Kandidaten -->
  <div class="card" id="cand-card">
    <div class="flex items-center justify-between mb-4">
      <h2 class="text-lg font-semibold">Kandidaten</h2>
      <span id="cand-badge" class="text-xs px-2 py-1 rounded-full bg-zinc-700 text-zinc-400">—</span>
    </div>
    <div id="cand-db" class="text-sm text-zinc-400 mb-3"></div>
    <div class="progress-bg mb-2"><div id="cand-bar" class="progress-fill bg-blue-500" style="width:0%"></div></div>
    <div class="flex justify-between text-sm">
      <span id="cand-progress" class="text-zinc-400">—</span>
      <span id="cand-pct" class="font-medium text-zinc-300">—</span>
    </div>
    <div id="cand-stats" class="mt-3 grid grid-cols-3 gap-3 text-center text-xs"></div>
    <div id="cand-result" class="mt-3 text-sm text-zinc-400 hidden"></div>
  </div>

  <!-- Jobs -->
  <div class="card" id="job-card">
    <div class="flex items-center justify-between mb-4">
      <h2 class="text-lg font-semibold">Jobs</h2>
      <span id="job-badge" class="text-xs px-2 py-1 rounded-full bg-zinc-700 text-zinc-400">—</span>
    </div>
    <div id="job-db" class="text-sm text-zinc-400 mb-3"></div>
    <div class="progress-bg mb-2"><div id="job-bar" class="progress-fill bg-emerald-500" style="width:0%"></div></div>
    <div class="flex justify-between text-sm">
      <span id="job-progress" class="text-zinc-400">—</span>
      <span id="job-pct" class="font-medium text-zinc-300">—</span>
    </div>
    <div id="job-stats" class="mt-3 grid grid-cols-3 gap-3 text-center text-xs"></div>
    <div id="job-result" class="mt-3 text-sm text-zinc-400 hidden"></div>
  </div>

  <!-- Start-Buttons -->
  <div class="card">
    <p class="font-medium text-zinc-300 mb-4">Klassifizierung starten:</p>
    <div class="flex gap-3">
      <button id="btn-cand" onclick="startClassification('candidates')"
        class="flex-1 px-4 py-3 bg-blue-600 hover:bg-blue-700 text-white font-medium rounded-lg transition-colors disabled:opacity-50 disabled:cursor-not-allowed">
        Kandidaten klassifizieren
      </button>
      <button id="btn-job" onclick="startClassification('jobs')"
        class="flex-1 px-4 py-3 bg-emerald-600 hover:bg-emerald-700 text-white font-medium rounded-lg transition-colors disabled:opacity-50 disabled:cursor-not-allowed">
        Jobs klassifizieren
      </button>
    </div>
    <div id="start-msg" class="mt-3 text-sm text-zinc-400 hidden"></div>
  </div>
</div>

<script>
function stat(label, value, color) {
  return `<div class="bg-zinc-900 rounded-lg p-2"><div class="font-bold text-${color}-400">${value}</div><div class="text-zinc-500">${label}</div></div>`;
}

async function poll() {
  try {
    // Kandidaten
    const cr = await fetch('/api/candidates/maintenance/classification-status');
    const cd = await cr.json();
    const cl = cd.live_progress || {};
    const cdb = cd.db_status || {};

    document.getElementById('cand-db').textContent =
      `DB: ${cdb.classified || 0} / ${cdb.total_finance || 0} klassifiziert (${cdb.classification_percent || 0}%)`;

    if (cl.running) {
      document.getElementById('cand-badge').className = 'text-xs px-2 py-1 rounded-full bg-blue-900 text-blue-300 pulse';
      document.getElementById('cand-badge').textContent = 'Laeuft...';
      const pct = cl.total > 0 ? Math.round(cl.processed / cl.total * 100) : 0;
      document.getElementById('cand-bar').style.width = pct + '%';
      document.getElementById('cand-progress').textContent = `${cl.processed} / ${cl.total}`;
      document.getElementById('cand-pct').textContent = pct + '%';
      document.getElementById('cand-stats').innerHTML =
        stat('Klassifiziert', cl.classified, 'blue') +
        stat('Fehler', cl.errors, 'red') +
        stat('Kosten', '$' + (cl.cost_usd || 0).toFixed(3), 'amber');
    } else if (cl.result) {
      document.getElementById('cand-badge').className = 'text-xs px-2 py-1 rounded-full bg-green-900 text-green-300';
      document.getElementById('cand-badge').textContent = 'Fertig';
      document.getElementById('cand-bar').style.width = '100%';
      const r = cl.result;
      document.getElementById('cand-progress').textContent = `${r.classified || 0} / ${r.total || 0}`;
      document.getElementById('cand-pct').textContent = '100%';
      document.getElementById('cand-stats').innerHTML =
        stat('Klassifiziert', r.classified || 0, 'blue') +
        stat('Fehler', r.errors || 0, 'red') +
        stat('Kosten', '$' + (r.cost_usd || 0).toFixed(3), 'amber');
      if (r.duration_seconds) {
        document.getElementById('cand-result').classList.remove('hidden');
        document.getElementById('cand-result').textContent = `Dauer: ${Math.round(r.duration_seconds)}s | Rollen: ${JSON.stringify(r.roles_distribution || {})}`;
      }
    } else {
      document.getElementById('cand-badge').textContent = 'Inaktiv';
      document.getElementById('cand-badge').className = 'text-xs px-2 py-1 rounded-full bg-zinc-700 text-zinc-400';
    }
  } catch(e) { console.error('Kandidaten-Poll Fehler:', e); }

  try {
    // Jobs
    const jr = await fetch('/api/jobs/maintenance/classification-status');
    const jd = await jr.json();
    const jl = jd.live_progress || {};
    const jdb = jd.db_status || {};

    document.getElementById('job-db').textContent =
      `DB: ${jdb.classified || 0} / ${jdb.total_finance || 0} klassifiziert (${jdb.classification_percent || 0}%)`;

    if (jl.running) {
      document.getElementById('job-badge').className = 'text-xs px-2 py-1 rounded-full bg-emerald-900 text-emerald-300 pulse';
      document.getElementById('job-badge').textContent = 'Laeuft...';
      const pct = jl.total > 0 ? Math.round(jl.processed / jl.total * 100) : 0;
      document.getElementById('job-bar').style.width = pct + '%';
      document.getElementById('job-progress').textContent = `${jl.processed} / ${jl.total}`;
      document.getElementById('job-pct').textContent = pct + '%';
      document.getElementById('job-stats').innerHTML =
        stat('Klassifiziert', jl.classified, 'emerald') +
        stat('Fehler', jl.errors, 'red') +
        stat('Kosten', '$' + (jl.cost_usd || 0).toFixed(3), 'amber');
    } else if (jl.result) {
      document.getElementById('job-badge').className = 'text-xs px-2 py-1 rounded-full bg-green-900 text-green-300';
      document.getElementById('job-badge').textContent = 'Fertig';
      document.getElementById('job-bar').style.width = '100%';
      const r = jl.result;
      document.getElementById('job-progress').textContent = `${r.classified || 0} / ${r.total || 0}`;
      document.getElementById('job-pct').textContent = '100%';
      document.getElementById('job-stats').innerHTML =
        stat('Klassifiziert', r.classified || 0, 'emerald') +
        stat('Fehler', r.errors || 0, 'red') +
        stat('Kosten', '$' + (r.cost_usd || 0).toFixed(3), 'amber');
      if (r.duration_seconds) {
        document.getElementById('job-result').classList.remove('hidden');
        document.getElementById('job-result').textContent = `Dauer: ${Math.round(r.duration_seconds)}s | Rollen: ${JSON.stringify(r.roles_distribution || {})}`;
      }
    } else {
      document.getElementById('job-badge').textContent = 'Inaktiv';
      document.getElementById('job-badge').className = 'text-xs px-2 py-1 rounded-full bg-zinc-700 text-zinc-400';
    }
  } catch(e) { console.error('Jobs-Poll Fehler:', e); }
}

async function startClassification(type) {
  const btn = document.getElementById(type === 'candidates' ? 'btn-cand' : 'btn-job');
  const msg = document.getElementById('start-msg');
  btn.disabled = true;
  btn.textContent = 'Wird gestartet...';
  try {
    const url = '/api/' + type + '/maintenance/reclassify-finance?force=true';
    const csrfMatch = document.cookie.match(/pp_csrf=([^;]+)/);
    const headers = {};
    if (csrfMatch) headers['X-CSRF-Token'] = csrfMatch[1];
    const res = await fetch(url, {method: 'POST', headers});
    const data = await res.json();
    msg.classList.remove('hidden');
    if (res.status === 429) {
      msg.textContent = 'Rate-Limit erreicht. Bitte kurz warten.';
      msg.className = 'mt-3 text-sm text-amber-400';
    } else if (res.status === 401 || res.status === 403) {
      msg.textContent = 'Nicht authentifiziert (HTTP ' + res.status + '). Bitte neu einloggen.';
      msg.className = 'mt-3 text-sm text-red-400';
    } else if (data.status === 'started') {
      msg.textContent = (type === 'candidates' ? 'Kandidaten' : 'Jobs') + '-Klassifizierung gestartet!';
      msg.className = 'mt-3 text-sm text-green-400';
    } else if (data.status === 'already_running') {
      msg.textContent = 'Laeuft bereits!';
      msg.className = 'mt-3 text-sm text-amber-400';
    } else {
      msg.textContent = 'HTTP ' + res.status + ': ' + JSON.stringify(data);
      msg.className = 'mt-3 text-sm text-red-400';
    }
  } catch(e) {
    msg.classList.remove('hidden');
    msg.textContent = 'Fehler: ' + e.message;
    msg.className = 'mt-3 text-sm text-red-400';
  }
  btn.disabled = false;
  btn.textContent = type === 'candidates' ? 'Kandidaten klassifizieren' : 'Jobs klassifizieren';
  setTimeout(() => poll(), 1000);
}

poll();
setInterval(poll, 3000);
</script>
</body>
</html>"""
    return HTMLResponse(content=html)
