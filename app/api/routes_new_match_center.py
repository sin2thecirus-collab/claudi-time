"""Neues Match Center — Claude Code Matching.

Seiten:
- GET /new-match-center                → Hauptseite (Dashboard + Match-Karten)

API:
- GET  /api/new-match-center/stats      → Dashboard-Statistiken (JSON)
- GET  /api/new-match-center/matches    → Match-Karten (HTMX partial)
- GET  /api/new-match-center/match/<id> → Match-Detail (HTMX modal)
- GET  /api/new-match-center/filters    → Filter-Optionen (JSON)
- POST /api/new-match-center/status/<id> → Status aendern (JSON)
- POST /api/new-match-center/feedback/<id> → Feedback speichern (JSON)
"""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.new_match_center_service import NewMatchCenterService

logger = logging.getLogger(__name__)
templates = Jinja2Templates(directory="app/templates")

router = APIRouter(tags=["New-Match-Center"])


# ═══════════════════════════════════════════════════════════════
# SEITEN
# ═══════════════════════════════════════════════════════════════


@router.get("/new-match-center", response_class=HTMLResponse)
async def new_match_center_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Neues Match Center — Dashboard + Match-Karten."""
    service = NewMatchCenterService(db)

    stats = await service.get_dashboard_stats()
    filters = await service.get_filter_options()

    # Erste Seite Matches laden (vorstellen, sortiert nach Score)
    matches_data = await service.get_matches(
        empfehlung="vorstellen",
        sort_by="score",
        sort_dir="desc",
        page=1,
        per_page=20,
    )

    return templates.TemplateResponse(
        "new_match_center.html",
        {
            "request": request,
            "stats": stats,
            "filters": filters,
            "matches": matches_data["matches"],
            "total": matches_data["total"],
            "page": matches_data["page"],
            "pages": matches_data["pages"],
        },
    )


# ═══════════════════════════════════════════════════════════════
# API
# ═══════════════════════════════════════════════════════════════


@router.get("/api/new-match-center/stats")
async def get_stats(
    db: AsyncSession = Depends(get_db),
):
    """Dashboard-Statistiken."""
    service = NewMatchCenterService(db)
    stats = await service.get_dashboard_stats()
    return JSONResponse(stats)


@router.get("/api/new-match-center/matches", response_class=HTMLResponse)
async def get_matches(
    request: Request,
    empfehlung: str = Query(None),
    city: str = Query(None),
    role: str = Query(None),
    wow_only: bool = Query(False),
    status_filter: str = Query(None),
    score_min: int = Query(None, ge=0, le=100),
    score_max: int = Query(None, ge=0, le=100),
    sort_by: str = Query("score"),
    sort_dir: str = Query("desc"),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """HTMX Partial: Match-Karten mit Filtern."""
    service = NewMatchCenterService(db)

    data = await service.get_matches(
        empfehlung=empfehlung,
        city=city,
        role=role,
        wow_only=wow_only,
        status_filter=status_filter,
        score_min=score_min,
        score_max=score_max,
        sort_by=sort_by,
        sort_dir=sort_dir,
        page=page,
        per_page=per_page,
    )

    return templates.TemplateResponse(
        "partials/new_match_center_cards.html",
        {
            "request": request,
            "matches": data["matches"],
            "total": data["total"],
            "page": data["page"],
            "per_page": data["per_page"],
            "pages": data["pages"],
            # Aktuelle Filter fuer Pagination
            "current_empfehlung": empfehlung,
            "current_city": city,
            "current_role": role,
            "current_wow_only": wow_only,
            "current_sort_by": sort_by,
            "current_sort_dir": sort_dir,
        },
    )


@router.get("/api/new-match-center/match/{match_id}", response_class=HTMLResponse)
async def get_match_detail(
    request: Request,
    match_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """HTMX Partial: Match-Detail Modal."""
    service = NewMatchCenterService(db)
    data = await service.get_match_detail(match_id)

    if not data:
        return HTMLResponse(
            '<div class="p-6 text-center text-red-500">Match nicht gefunden</div>',
            status_code=404,
        )

    return templates.TemplateResponse(
        "partials/new_match_center_detail.html",
        {
            "request": request,
            "data": data,
        },
    )


@router.get("/api/new-match-center/filters")
async def get_filters(
    db: AsyncSession = Depends(get_db),
):
    """Verfuegbare Filter-Optionen."""
    service = NewMatchCenterService(db)
    options = await service.get_filter_options()
    return JSONResponse(options)


@router.post("/api/new-match-center/status/{match_id}")
async def update_status(
    match_id: UUID,
    status: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """Match-Status aendern."""
    valid = {"new", "ai_checked", "presented", "rejected", "placed"}
    if status not in valid:
        return JSONResponse({"error": f"Ungueltiger Status: {status}"}, status_code=400)

    service = NewMatchCenterService(db)
    ok = await service.update_status(match_id, status)

    if not ok:
        return JSONResponse({"error": "Match nicht gefunden"}, status_code=404)

    return JSONResponse({"status": "ok", "new_status": status})


@router.post("/api/new-match-center/feedback/{match_id}")
async def save_feedback(
    match_id: UUID,
    feedback: str = Query(...),
    note: str = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Feedback fuer ein Match speichern."""
    valid = {"good", "bad_distance", "bad_skills", "bad_seniority", "maybe"}
    if feedback not in valid:
        return JSONResponse({"error": f"Ungueltiges Feedback: {feedback}"}, status_code=400)

    service = NewMatchCenterService(db)
    ok = await service.save_feedback(match_id, feedback, note)

    if not ok:
        return JSONResponse({"error": "Match nicht gefunden"}, status_code=404)

    return JSONResponse({"status": "ok", "feedback": feedback})
