"""Match Center Routes - Einheitliche job-zentrische Match-Verwaltung.

Ersetzt Pre-Match + DeepMatch Navigation mit einem unified Match Center.

Seiten:
- GET /match-center              -> Job-zentrische Uebersicht (Tabs: Neu / In Bearbeitung / Archiv)
- GET /match-center/job/<job_id> -> Einzelner Job mit allen Kandidaten-Matches

API (HTMX):
- GET  /api/match-center/jobs                   -> Job-Liste nach Stage (HTMX partial)
- GET  /api/match-center/job/<job_id>/matches   -> Kandidaten fuer einen Job (HTMX partial)
- POST /api/match-center/status/<match_id>      -> Match-Status aendern
- POST /api/match-center/feedback/<match_id>    -> Feedback speichern

Legacy-Redirects:
- GET /pre-match       -> 301 /match-center
- GET /pre-match/*     -> 301 /match-center
- GET /deep-match      -> 301 /match-center
"""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.match_center_service import MatchCenterService

logger = logging.getLogger(__name__)
templates = Jinja2Templates(directory="app/templates")

router = APIRouter(tags=["Match-Center"])


# ═══════════════════════════════════════════════════════════════
# SEITEN
# ═══════════════════════════════════════════════════════════════


@router.get("/match-center", response_class=HTMLResponse)
async def match_center_page(
    request: Request,
    stage: str = Query("new", regex="^(new|in_progress|archive)$"),
    search: str = Query(None),
    page: int = Query(1, ge=1),
    db: AsyncSession = Depends(get_db),
):
    """Match Center Hauptseite - Job-zentrische Uebersicht."""
    service = MatchCenterService(db)

    # Statistiken und Stage-Counts parallel
    stats = await service.get_stats()
    stage_counts = await service.get_stage_counts()
    jobs, total = await service.get_jobs_overview(
        stage=stage,
        search=search,
        page=page,
        per_page=20,
    )

    total_pages = max(1, (total + 19) // 20)

    return templates.TemplateResponse(
        "match_center.html",
        {
            "request": request,
            "stats": stats,
            "stage_counts": stage_counts,
            "jobs": jobs,
            "current_stage": stage,
            "current_search": search or "",
            "current_page": page,
            "total_pages": total_pages,
            "total_jobs": total,
        },
    )


@router.get("/match-center/job/{job_id}", response_class=HTMLResponse)
async def match_center_job_detail(
    request: Request,
    job_id: UUID,
    sort_by: str = Query("ai_score"),
    db: AsyncSession = Depends(get_db),
):
    """Detail-Ansicht: Einzelner Job mit allen Kandidaten-Matches."""
    service = MatchCenterService(db)

    job = await service.get_job_detail(job_id)
    if not job:
        return templates.TemplateResponse(
            "match_center.html",
            {"request": request, "error": "Job nicht gefunden"},
            status_code=404,
        )

    matches = await service.get_job_matches(job_id, sort_by=sort_by, limit=50)

    return templates.TemplateResponse(
        "match_center_job.html",
        {
            "request": request,
            "job": job,
            "matches": matches,
            "sort_by": sort_by,
        },
    )


# ═══════════════════════════════════════════════════════════════
# API - HTMX PARTIALS
# ═══════════════════════════════════════════════════════════════


@router.get("/api/match-center/jobs", response_class=HTMLResponse)
async def get_jobs_partial(
    request: Request,
    stage: str = Query("new", regex="^(new|in_progress|archive)$"),
    search: str = Query(None),
    page: int = Query(1, ge=1),
    db: AsyncSession = Depends(get_db),
):
    """HTMX Partial: Job-Liste nach Stage."""
    service = MatchCenterService(db)

    jobs, total = await service.get_jobs_overview(
        stage=stage,
        search=search,
        page=page,
        per_page=20,
    )
    total_pages = max(1, (total + 19) // 20)

    return templates.TemplateResponse(
        "partials/match_center_jobs.html",
        {
            "request": request,
            "jobs": jobs,
            "current_stage": stage,
            "current_search": search or "",
            "current_page": page,
            "total_pages": total_pages,
            "total_jobs": total,
        },
    )


@router.get("/api/match-center/job/{job_id}/matches", response_class=HTMLResponse)
async def get_job_matches_partial(
    request: Request,
    job_id: UUID,
    sort_by: str = Query("ai_score"),
    db: AsyncSession = Depends(get_db),
):
    """HTMX Partial: Kandidaten-Matches fuer einen Job."""
    service = MatchCenterService(db)

    matches = await service.get_job_matches(job_id, sort_by=sort_by, limit=10)

    return templates.TemplateResponse(
        "partials/match_center_candidates.html",
        {
            "request": request,
            "matches": matches,
            "job_id": job_id,
        },
    )


@router.post("/api/match-center/status/{match_id}", response_class=HTMLResponse)
async def update_match_status(
    request: Request,
    match_id: UUID,
    status: str = Query(..., regex="^(new|ai_checked|presented|rejected|placed)$"),
    db: AsyncSession = Depends(get_db),
):
    """Match-Status aendern (HTMX)."""
    service = MatchCenterService(db)

    match = await service.update_match_status(match_id, status)

    if not match:
        return HTMLResponse("<span class='text-red-500 text-xs'>Nicht gefunden</span>", status_code=404)

    # Status-Labels und Farben
    status_config = {
        "new": ("Neu", "bg-gray-100 text-gray-700"),
        "ai_checked": ("Bewertet", "bg-blue-100 text-blue-700"),
        "presented": ("Vorgestellt", "bg-green-100 text-green-700"),
        "rejected": ("Abgelehnt", "bg-red-100 text-red-700"),
        "placed": ("Vermittelt", "bg-purple-100 text-purple-700"),
    }
    label, css = status_config.get(status, ("Unbekannt", "bg-gray-100 text-gray-700"))

    # Trigger Toast via HX-Trigger Header
    from fastapi.responses import HTMLResponse as HR
    response = HR(
        f'<span class="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium {css}">{label}</span>'
    )
    response.headers["HX-Trigger"] = '{"showToast": {"message": "Status aktualisiert", "type": "success"}}'
    return response


@router.post("/api/match-center/feedback/{match_id}", response_class=HTMLResponse)
async def save_feedback(
    request: Request,
    match_id: UUID,
    feedback: str = Query(..., regex="^(good|bad|maybe)$"),
    note: str = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Feedback fuer ein Match speichern (HTMX)."""
    service = MatchCenterService(db)

    match = await service.save_feedback(match_id, feedback, note)

    if not match:
        return HTMLResponse("<span class='text-red-500 text-xs'>Nicht gefunden</span>", status_code=404)

    feedback_icons = {
        "good": "text-green-600",
        "bad": "text-red-600",
        "maybe": "text-yellow-600",
    }
    icon_class = feedback_icons.get(feedback, "text-gray-400")

    response = HTMLResponse(
        f'<span class="text-sm {icon_class} font-medium">Feedback gespeichert</span>'
    )
    response.headers["HX-Trigger"] = '{"showToast": {"message": "Feedback gespeichert", "type": "success"}}'
    return response


# ═══════════════════════════════════════════════════════════════
# LEGACY-REDIRECTS
# ═══════════════════════════════════════════════════════════════


@router.get("/pre-match", response_class=RedirectResponse, include_in_schema=False)
async def redirect_pre_match():
    """Redirect alte Pre-Match URL zum Match Center."""
    return RedirectResponse("/match-center", status_code=301)


@router.get("/pre-match/detail", response_class=RedirectResponse, include_in_schema=False)
async def redirect_pre_match_detail():
    """Redirect alte Pre-Match Detail URL zum Match Center."""
    return RedirectResponse("/match-center", status_code=301)


@router.get("/pre-match/calibration", response_class=RedirectResponse, include_in_schema=False)
async def redirect_calibration():
    """Redirect alte Calibration URL zum Match Center."""
    return RedirectResponse("/match-center", status_code=301)


## /deep-match Redirect nicht noetig — Route existiert noch in routes_hotlisten.py
## Die DeepMatch-Funktionalitaet bleibt erhalten, nur nicht mehr in der Nav.
