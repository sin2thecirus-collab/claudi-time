"""Hotlisten Routes - Seiten und API-Endpoints für das Hotlisten & DeepMatch System.

3-Stufen-System:
- /hotlisten → Übersicht der Kategorien (FINANCE, ENGINEERING)
- /match-bereiche → Stadt × Beruf Grid mit Counts
- /deepmatch → Kandidaten-Auswahl + KI-Bewertung
"""

import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func, and_, case
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.candidate import Candidate
from app.models.job import Job
from app.models.match import Match
from sqlalchemy import text as sa_text, literal_column

from app.services.categorization_service import CategorizationService, HotlistCategory
from app.services.pre_scoring_service import PreScoringService
from app.services.deepmatch_service import DeepMatchService
from app.services.finance_classifier_service import FinanceClassifierService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Hotlisten"])
templates = Jinja2Templates(directory="app/templates")


# ════════════════════════════════════════════════════════════════
# STUFE 1: HOTLISTEN — Kategorie-Übersicht
# ════════════════════════════════════════════════════════════════

@router.get("/hotlisten", response_class=HTMLResponse)
async def hotlisten_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Hotlisten-Übersichtsseite: Zeigt FINANCE und ENGINEERING mit Statistiken."""
    # Kandidaten-Counts pro Kategorie
    candidates_q = await db.execute(
        select(
            Candidate.hotlist_category,
            func.count(Candidate.id),
        )
        .where(Candidate.deleted_at.is_(None))
        .where(Candidate.hotlist_category.isnot(None))
        .group_by(Candidate.hotlist_category)
    )
    candidate_counts = dict(candidates_q.all())

    # Jobs-Counts pro Kategorie
    jobs_q = await db.execute(
        select(
            Job.hotlist_category,
            func.count(Job.id),
        )
        .where(Job.deleted_at.is_(None))
        .where(Job.hotlist_category.isnot(None))
        .group_by(Job.hotlist_category)
    )
    job_counts = dict(jobs_q.all())

    categories = [
        {
            "name": "FINANCE",
            "label": "Finance & Accounting",
            "icon": "💰",
            "description": "Buchhalter, Controller, Steuerfachangestellte",
            "candidates": candidate_counts.get(HotlistCategory.FINANCE, 0),
            "jobs": job_counts.get(HotlistCategory.FINANCE, 0),
            "color": "blue",
        },
        {
            "name": "ENGINEERING",
            "label": "Engineering & Technik",
            "icon": "⚡",
            "description": "Servicetechniker, Elektriker, SHK, Mechanik",
            "candidates": candidate_counts.get(HotlistCategory.ENGINEERING, 0),
            "jobs": job_counts.get(HotlistCategory.ENGINEERING, 0),
            "color": "green",
        },
    ]

    total_uncategorized_candidates = candidate_counts.get(HotlistCategory.SONSTIGE, 0)
    total_uncategorized_jobs = job_counts.get(HotlistCategory.SONSTIGE, 0)

    return templates.TemplateResponse(
        "hotlisten.html",
        {
            "request": request,
            "categories": categories,
            "sonstige_candidates": total_uncategorized_candidates,
            "sonstige_jobs": total_uncategorized_jobs,
        },
    )


# ════════════════════════════════════════════════════════════════
# STUFE 2: MATCH-BEREICHE — Stadt × Beruf Grid
# ════════════════════════════════════════════════════════════════

@router.get("/match-bereiche", response_class=HTMLResponse)
async def match_bereiche_page(
    request: Request,
    category: str = Query(default="FINANCE"),
    db: AsyncSession = Depends(get_db),
):
    """Match-Bereiche: Stadt × Beruf Grid mit Kandidaten- und Job-Counts."""
    # Kandidaten nach Stadt gruppiert
    candidates_by_city = await db.execute(
        select(
            Candidate.hotlist_city,
            func.count(Candidate.id),
        )
        .where(
            and_(
                Candidate.hotlist_category == category,
                Candidate.deleted_at.is_(None),
                Candidate.hotlist_city.isnot(None),
            )
        )
        .group_by(Candidate.hotlist_city)
        .order_by(func.count(Candidate.id).desc())
    )
    candidate_city_counts = dict(candidates_by_city.all())

    # Jobs nach Stadt gruppiert
    jobs_by_city = await db.execute(
        select(
            Job.hotlist_city,
            func.count(Job.id),
        )
        .where(
            and_(
                Job.hotlist_category == category,
                Job.deleted_at.is_(None),
                Job.hotlist_city.isnot(None),
            )
        )
        .group_by(Job.hotlist_city)
        .order_by(func.count(Job.id).desc())
    )
    job_city_counts = dict(jobs_by_city.all())

    # Kandidaten nach Job-Title gruppiert (UNNEST für Multi-Titel-Array)
    candidates_by_title_q = await db.execute(
        sa_text("""
            SELECT unnested_title, COUNT(DISTINCT id)
            FROM candidates, LATERAL unnest(
                COALESCE(hotlist_job_titles, ARRAY[hotlist_job_title])
            ) AS unnested_title
            WHERE hotlist_category = :category
              AND deleted_at IS NULL
              AND (hotlist_job_titles IS NOT NULL OR hotlist_job_title IS NOT NULL)
            GROUP BY unnested_title
            ORDER BY COUNT(DISTINCT id) DESC
        """),
        {"category": category},
    )
    candidate_title_counts = dict(candidates_by_title_q.all())

    # Jobs nach Job-Title gruppiert (UNNEST für Multi-Titel-Array)
    jobs_by_title_q = await db.execute(
        sa_text("""
            SELECT unnested_title, COUNT(DISTINCT id)
            FROM jobs, LATERAL unnest(
                COALESCE(hotlist_job_titles, ARRAY[hotlist_job_title])
            ) AS unnested_title
            WHERE hotlist_category = :category
              AND deleted_at IS NULL
              AND (hotlist_job_titles IS NOT NULL OR hotlist_job_title IS NOT NULL)
            GROUP BY unnested_title
            ORDER BY COUNT(DISTINCT id) DESC
        """),
        {"category": category},
    )
    job_title_counts = dict(jobs_by_title_q.all())

    # Alle Städte sammeln (Union von Kandidaten und Jobs)
    all_cities = sorted(
        set(candidate_city_counts.keys()) | set(job_city_counts.keys())
    )

    # Alle Titel sammeln
    all_titles = sorted(
        set(candidate_title_counts.keys()) | set(job_title_counts.keys())
    )

    # Grid-Daten aufbauen
    city_data = [
        {
            "name": city,
            "candidates": candidate_city_counts.get(city, 0),
            "jobs": job_city_counts.get(city, 0),
        }
        for city in all_cities
    ]

    title_data = [
        {
            "name": title,
            "candidates": candidate_title_counts.get(title, 0),
            "jobs": job_title_counts.get(title, 0),
        }
        for title in all_titles
    ]

    # Stadt × Beruf Kombinationen — Kandidaten (UNNEST für Multi-Titel)
    combo_candidates_q = await db.execute(
        sa_text("""
            SELECT hotlist_city, unnested_title, COUNT(DISTINCT id)
            FROM candidates, LATERAL unnest(
                COALESCE(hotlist_job_titles, ARRAY[hotlist_job_title])
            ) AS unnested_title
            WHERE hotlist_category = :category
              AND deleted_at IS NULL
              AND hotlist_city IS NOT NULL
              AND (hotlist_job_titles IS NOT NULL OR hotlist_job_title IS NOT NULL)
            GROUP BY hotlist_city, unnested_title
            ORDER BY hotlist_city, unnested_title
        """),
        {"category": category},
    )
    combo_cand = {(r[0], r[1]): r[2] for r in combo_candidates_q.all()}

    # Stadt × Beruf Kombinationen — Jobs (UNNEST für Multi-Titel)
    combo_jobs_q = await db.execute(
        sa_text("""
            SELECT hotlist_city, unnested_title, COUNT(DISTINCT id)
            FROM jobs, LATERAL unnest(
                COALESCE(hotlist_job_titles, ARRAY[hotlist_job_title])
            ) AS unnested_title
            WHERE hotlist_category = :category
              AND deleted_at IS NULL
              AND hotlist_city IS NOT NULL
              AND (hotlist_job_titles IS NOT NULL OR hotlist_job_title IS NOT NULL)
            GROUP BY hotlist_city, unnested_title
            ORDER BY hotlist_city, unnested_title
        """),
        {"category": category},
    )
    combo_jobs = {(r[0], r[1]): r[2] for r in combo_jobs_q.all()}

    # Alle Kombinationen sammeln, nach Stadt gruppiert
    all_combos = sorted(set(combo_cand.keys()) | set(combo_jobs.keys()))
    combo_by_city = {}
    for city, title in all_combos:
        if city not in combo_by_city:
            combo_by_city[city] = []
        cand_count = combo_cand.get((city, title), 0)
        jobs_count = combo_jobs.get((city, title), 0)
        combo_by_city[city].append({
            "title": title,
            "candidates": cand_count,
            "jobs": jobs_count,
            "can_match": cand_count > 0 and jobs_count > 0,
        })
    # Nach Kandidaten-Summe sortieren (größte Stadt zuerst)
    combo_data = sorted(
        combo_by_city.items(),
        key=lambda x: sum(t["candidates"] for t in x[1]),
        reverse=True,
    )

    return templates.TemplateResponse(
        "match_bereiche.html",
        {
            "request": request,
            "category": category,
            "category_label": "Finance & Accounting" if category == "FINANCE" else "Engineering & Technik",
            "city_data": city_data,
            "title_data": title_data,
            "combo_data": combo_data,
        },
    )


# ════════════════════════════════════════════════════════════════
# STUFE 3: DEEPMATCH — Kandidaten-Auswahl + KI-Bewertung
# ════════════════════════════════════════════════════════════════

@router.get("/deepmatch", response_class=HTMLResponse)
async def deepmatch_page(
    request: Request,
    category: str = Query(default="FINANCE"),
    city: Optional[str] = None,
    job_title: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """DeepMatch-Seite: Kandidaten mit Pre-Scores, Checkbox-Auswahl, KI-Bewertung."""
    # Matches laden: Kandidat + Job gleiche Kategorie, optional Stadt/Titel filtern
    query = (
        select(Match, Candidate, Job)
        .join(Candidate, Match.candidate_id == Candidate.id)
        .join(Job, Match.job_id == Job.id)
        .where(
            and_(
                Candidate.hotlist_category == category,
                Job.hotlist_category == category,
                Candidate.deleted_at.is_(None),
                Job.deleted_at.is_(None),
            )
        )
    )

    if city:
        query = query.where(Candidate.hotlist_city == city)
    if job_title:
        # ANY() für Array-Match: Kandidat hat job_title in seinen hotlist_job_titles
        query = query.where(
            func.coalesce(Candidate.hotlist_job_titles, func.array([Candidate.hotlist_job_title])).any(job_title)
        )

    # Sortierung: pre_score DESC (beste zuerst), dann ai_score
    query = query.order_by(
        Match.pre_score.desc().nullslast(),
        Match.ai_score.desc().nullslast(),
    )
    query = query.limit(100)  # Max 100 Matches anzeigen

    result = await db.execute(query)
    rows = result.all()

    matches_data = []
    for match, candidate, job in rows:
        matches_data.append({
            "match_id": str(match.id),
            "candidate_name": candidate.full_name,
            "candidate_position": candidate.current_position or "—",
            "candidate_city": candidate.hotlist_city or candidate.city or "—",
            "candidate_job_titles": candidate.hotlist_job_titles or ([candidate.hotlist_job_title] if candidate.hotlist_job_title else []),
            "job_position": job.position,
            "job_company": job.company_name,
            "job_city": job.hotlist_city or job.display_city,
            "job_job_titles": job.hotlist_job_titles or ([job.hotlist_job_title] if job.hotlist_job_title else []),
            "distance_km": round(match.distance_km, 1) if match.distance_km else None,
            "pre_score": round(match.pre_score, 0) if match.pre_score else None,
            "ai_score": round(match.ai_score * 100, 0) if match.ai_score else None,
            "ai_explanation": match.ai_explanation,
            "ai_strengths": match.ai_strengths or [],
            "ai_weaknesses": match.ai_weaknesses or [],
            "status": match.status.value if match.status else "new",
            "user_feedback": match.user_feedback,
        })

    return templates.TemplateResponse(
        "deepmatch.html",
        {
            "request": request,
            "category": category,
            "category_label": "Finance & Accounting" if category == "FINANCE" else "Engineering & Technik",
            "city": city,
            "job_title": job_title,
            "matches": matches_data,
            "total_matches": len(matches_data),
        },
    )


# ════════════════════════════════════════════════════════════════
# API-ENDPOINTS (JSON, für HTMX)
# ════════════════════════════════════════════════════════════════

@router.post("/api/hotlisten/categorize", tags=["Hotlisten API"])
async def trigger_categorization(
    force: bool = Query(default=False),
    db: AsyncSession = Depends(get_db),
):
    """Kategorisiert alle Kandidaten und Jobs."""
    cat_service = CategorizationService(db)
    result = await cat_service.categorize_all(force=force)
    return result


@router.post("/api/hotlisten/classify-finance", tags=["Hotlisten API"])
async def trigger_finance_classification(
    force: bool = Query(default=False),
    target: str = Query(default="candidates"),  # "candidates", "jobs", "both"
    db: AsyncSession = Depends(get_db),
):
    """Klassifiziert FINANCE-Kandidaten/Jobs via OpenAI (einmalig für Training)."""
    service = FinanceClassifierService(db)
    results = {}

    if target in ("candidates", "both"):
        cand_result = await service.classify_all_finance_candidates(force=force)
        results["candidates"] = {
            "total": cand_result.total,
            "classified": cand_result.classified,
            "skipped_leadership": cand_result.skipped_leadership,
            "skipped_no_cv": cand_result.skipped_no_cv,
            "skipped_no_role": cand_result.skipped_no_role,
            "skipped_error": cand_result.skipped_error,
            "multi_title_count": cand_result.multi_title_count,
            "roles_distribution": cand_result.roles_distribution,
            "cost_usd": cand_result.cost_usd,
            "duration_seconds": cand_result.duration_seconds,
        }

    if target in ("jobs", "both"):
        job_result = await service.classify_all_finance_jobs(force=force)
        results["jobs"] = {
            "total": job_result.total,
            "classified": job_result.classified,
            "skipped_no_role": job_result.skipped_no_role,
            "skipped_error": job_result.skipped_error,
            "multi_title_count": job_result.multi_title_count,
            "roles_distribution": job_result.roles_distribution,
            "cost_usd": job_result.cost_usd,
            "duration_seconds": job_result.duration_seconds,
        }

    return results


@router.post("/api/hotlisten/pre-score", tags=["Hotlisten API"])
async def trigger_pre_scoring(
    category: str = Query(default="FINANCE"),
    city: Optional[str] = None,
    force: bool = Query(default=False),
    db: AsyncSession = Depends(get_db),
):
    """Berechnet Pre-Scores für Matches einer Kategorie."""
    service = PreScoringService(db)
    result = await service.score_matches_for_category(
        category=category,
        city=city,
        force=force,
    )
    return {
        "total": result.total_matches,
        "scored": result.scored,
        "skipped": result.skipped,
        "avg_score": result.avg_score,
    }


@router.post("/api/deepmatch/evaluate", tags=["Hotlisten API"])
async def evaluate_deepmatch(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Führt DeepMatch-Bewertung für ausgewählte Matches durch."""
    body = await request.json()
    match_ids = [UUID(mid) for mid in body.get("match_ids", [])]

    if not match_ids:
        raise HTTPException(status_code=400, detail="Keine Matches ausgewählt")

    if len(match_ids) > 20:
        raise HTTPException(status_code=400, detail="Maximal 20 Matches pro Batch")

    async with DeepMatchService(db) as service:
        result = await service.evaluate_selected_matches(match_ids)

    return {
        "total_requested": result.total_requested,
        "evaluated": result.evaluated,
        "skipped_low_score": result.skipped_low_score,
        "skipped_error": result.skipped_error,
        "avg_ai_score": result.avg_ai_score,
        "total_cost_usd": result.total_cost_usd,
        "results": [
            {
                "match_id": str(r.match_id),
                "candidate_name": r.candidate_name,
                "job_position": r.job_position,
                "ai_score": r.ai_score,
                "explanation": r.explanation,
                "strengths": r.strengths,
                "weaknesses": r.weaknesses,
                "success": r.success,
            }
            for r in result.results
        ],
    }


@router.post("/api/deepmatch/feedback", tags=["Hotlisten API"])
async def save_deepmatch_feedback(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Speichert User-Feedback zu einem DeepMatch-Ergebnis."""
    body = await request.json()
    match_id = UUID(body.get("match_id"))
    feedback = body.get("feedback")  # "good", "neutral", "bad"
    note = body.get("note")

    if feedback not in ("good", "neutral", "bad"):
        raise HTTPException(status_code=400, detail="Ungültiges Feedback")

    async with DeepMatchService(db) as service:
        success = await service.save_feedback(match_id, feedback, note)

    if not success:
        raise HTTPException(status_code=404, detail="Match nicht gefunden")

    return {"success": True}
