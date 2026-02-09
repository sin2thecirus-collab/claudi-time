"""Status & Query API Endpunkte — Zentrale Uebersicht fuer Geodaten, Profiling, Matches, Aufgaben, Anrufe."""

import logging
from datetime import date, datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, Query
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
