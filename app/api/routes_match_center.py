"""Match Center Routes - Sidebar + Tabellen-Layout.

Seiten:
- GET /match-center              -> Hauptseite (Sidebar + Tabelle)

API (HTMX + JSON):
- GET  /api/match-center/sidebar             -> Sidebar-Daten (JSON)
- GET  /api/match-center/matches             -> Kandidaten-Tabelle (HTMX partial)
- POST /api/match-center/bulk-status         -> Bulk Status-Aenderung (JSON)
- GET  /api/match-center/export              -> CSV-Download
- GET  /api/match-center/group               -> Legacy Grid-Detail (HTMX)
- GET  /api/match-center/compare/<match_id>  -> Vergleichs-Modal (HTMX)
- POST /api/match-center/status/<match_id>   -> Status aendern (HTMX)
- POST /api/match-center/feedback/<match_id> -> Feedback speichern (JSON)
"""

import csv
import io
import logging
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
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
    db: AsyncSession = Depends(get_db),
):
    """Match Center Hauptseite — Sidebar + Tabellen-Layout."""
    service = MatchCenterService(db)

    stats = await service.get_stats()
    stage_counts = await service.get_stage_counts()

    return templates.TemplateResponse(
        "match_center.html",
        {
            "request": request,
            "stats": stats,
            "stage_counts": stage_counts,
            "current_stage": stage,
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


# ═══════════════════════════════════════════════════════════════
# NEUE API-ENDPUNKTE (Sidebar + Tabelle + Bulk + Export)
# ═══════════════════════════════════════════════════════════════


@router.get("/api/match-center/sidebar")
async def get_sidebar_data(
    stage: str = Query("new", regex="^(new|in_progress|archive)$"),
    db: AsyncSession = Depends(get_db),
):
    """Sidebar-Daten: Jobtitel mit verschachtelten Stadt-Listen."""
    service = MatchCenterService(db)
    data = await service.get_sidebar_data(stage=stage)
    return JSONResponse(data)


@router.get("/api/match-center/matches", response_class=HTMLResponse)
async def get_matches_table(
    request: Request,
    title: str = Query(...),
    city: str = Query(...),
    stage: str = Query("new", regex="^(new|in_progress|archive)$"),
    status_filter: str = Query(None),
    score_min: int = Query(None, ge=0, le=100),
    score_max: int = Query(None, ge=0, le=100),
    page: int = Query(1, ge=1),
    per_page: int = Query(15, ge=1, le=100),
    sort_by: str = Query("score"),
    sort_dir: str = Query("desc", regex="^(asc|desc)$"),
    db: AsyncSession = Depends(get_db),
):
    """HTMX Partial: Paginierte Kandidaten-Tabelle fuer Jobtitel + Stadt."""
    service = MatchCenterService(db)

    result = await service.get_paginated_matches(
        title=title,
        city=city,
        stage=stage,
        status_filter=status_filter,
        score_min=score_min,
        score_max=score_max,
        page=page,
        per_page=per_page,
        sort_by=sort_by,
        sort_dir=sort_dir,
    )

    return templates.TemplateResponse(
        "partials/match_center_table.html",
        {
            "request": request,
            "matches": result["matches"],
            "total": result["total"],
            "page": result["page"],
            "per_page": result["per_page"],
            "pages": result["pages"],
            "status_counts": result["status_counts"],
            "current_title": title,
            "current_city": city,
            "current_stage": stage,
            "current_status_filter": status_filter,
            "current_score_min": score_min,
            "current_score_max": score_max,
            "current_sort_by": sort_by,
            "current_sort_dir": sort_dir,
        },
    )


@router.post("/api/match-center/bulk-status")
async def bulk_update_status(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Bulk Status-Aenderung fuer mehrere Matches."""
    try:
        body = await request.json()
        match_ids = body.get("match_ids", [])
        new_status = body.get("status", "")

        if not match_ids or not new_status:
            return JSONResponse({"error": "match_ids und status erforderlich"}, status_code=400)

        # String UUIDs konvertieren
        uuids = [UUID(mid) for mid in match_ids]

        service = MatchCenterService(db)
        count = await service.bulk_update_status(uuids, new_status)

        status_labels = {
            "new": "Neu",
            "presented": "Vorgestellt",
            "rejected": "Abgelehnt",
            "placed": "Vermittelt",
        }
        label = status_labels.get(new_status, new_status)

        return JSONResponse({
            "status": "ok",
            "updated": count,
            "message": f"{count} Matches als '{label}' markiert",
        })
    except Exception as e:
        logger.error(f"Bulk Status Fehler: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/match-center/export")
async def export_matches(
    match_ids: str = Query(..., description="Komma-separierte Match-IDs"),
    db: AsyncSession = Depends(get_db),
):
    """CSV-Export fuer ausgewaehlte Matches."""
    try:
        uuids = [UUID(mid.strip()) for mid in match_ids.split(",") if mid.strip()]
    except ValueError:
        return JSONResponse({"error": "Ungueltige Match-IDs"}, status_code=400)

    if not uuids:
        return JSONResponse({"error": "Keine Match-IDs angegeben"}, status_code=400)

    service = MatchCenterService(db)
    data = await service.get_export_data(uuids)

    if not data:
        return JSONResponse({"error": "Keine Daten gefunden"}, status_code=404)

    # CSV generieren
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=data[0].keys(), delimiter=";")
    writer.writeheader()
    writer.writerows(data)

    content = output.getvalue().encode("utf-8-sig")  # BOM fuer Excel

    return StreamingResponse(
        io.BytesIO(content),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="matches_export.csv"',
            "Content-Length": str(len(content)),
        },
    )


# ═══════════════════════════════════════════════════════════════
# LEGACY API-ENDPUNKTE (Grid-Detail, Compare, Status, Feedback)
# ═══════════════════════════════════════════════════════════════


@router.get("/api/match-center/group", response_class=HTMLResponse)
async def get_group_detail(
    request: Request,
    title: str = Query(...),
    city: str = Query(...),
    stage: str = Query("new", regex="^(new|in_progress|archive)$"),
    db: AsyncSession = Depends(get_db),
):
    """HTMX Partial: Jobs + Matches fuer eine Jobtitel+Stadt Kombination."""
    service = MatchCenterService(db)

    jobs = await service.get_group_jobs(
        job_title=title,
        city=city,
        stage=stage,
    )

    return templates.TemplateResponse(
        "partials/match_center_group.html",
        {
            "request": request,
            "jobs": jobs,
            "group_title": title,
            "group_city": city,
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


@router.get("/api/match-center/compare/{match_id}", response_class=HTMLResponse)
async def get_match_comparison_partial(
    request: Request,
    match_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """HTMX Partial: Vergleichs-Dialog fuer ein Match (Job + Kandidat)."""
    service = MatchCenterService(db)

    comparison = await service.get_match_comparison(match_id)

    if not comparison:
        return HTMLResponse(
            '<div class="p-6 text-center text-red-500">Match nicht gefunden</div>',
            status_code=404,
        )

    return templates.TemplateResponse(
        "partials/match_center_compare.html",
        {
            "request": request,
            "data": comparison,
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

    status_config = {
        "new": ("Neu", "bg-gray-100 text-gray-700"),
        "ai_checked": ("Bewertet", "bg-blue-100 text-blue-700"),
        "presented": ("Vorgestellt", "bg-green-100 text-green-700"),
        "rejected": ("Abgelehnt", "bg-red-100 text-red-700"),
        "placed": ("Vermittelt", "bg-purple-100 text-purple-700"),
    }
    label, css = status_config.get(status, ("Unbekannt", "bg-gray-100 text-gray-700"))

    from fastapi.responses import HTMLResponse as HR
    response = HR(
        f'<span class="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium {css}">{label}</span>'
    )
    response.headers["HX-Trigger"] = '{"showToast": {"message": "Status aktualisiert", "type": "success"}}'
    return response


@router.post("/api/match-center/feedback/{match_id}")
async def save_feedback(
    match_id: UUID,
    feedback: str = Query(..., regex="^(good|bad_distance|bad_skills|bad_seniority|maybe)$"),
    note: str = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Feedback fuer ein Match speichern + Learning Service + Status-Aenderung.

    Feedback-Werte:
    - good: Guter Match
    - bad_distance: Distanz passt nicht
    - bad_skills: Taetigkeiten passen nicht
    - bad_seniority: Seniority passt nicht
    - maybe: Vielleicht / neutral
    """
    from app.models.match import Match, MatchStatus
    from app.services.matching_learning_service import MatchingLearningService

    try:
        # Match laden
        match = await db.get(Match, match_id)
        if not match:
            return JSONResponse({"status": "not_found"}, status_code=404)

        # Outcome bestimmen: good / bad / neutral
        is_bad = feedback.startswith("bad_")
        outcome = "bad" if is_bad else ("good" if feedback == "good" else "neutral")
        rejection_reason = feedback if is_bad else None

        # 1. Feedback in Match speichern
        match.user_feedback = feedback
        match.feedback_note = note
        match.feedback_at = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
        match.rejection_reason = rejection_reason

        # 2. Bei negativem Feedback: Status auf REJECTED setzen
        if is_bad:
            match.status = MatchStatus.REJECTED

        await db.commit()

        # 3. Job-Kategorie ermitteln (fuer pro-Kategorie-Lernen)
        job_category = None
        if match.job_id:
            from app.models.job import Job
            job = await db.get(Job, match.job_id)
            if job:
                job_category = job.hotlist_job_title or job.position

        # 4. Learning Service aufrufen (Fehler blockiert Feedback nicht)
        learning_info = {}
        try:
            learning = MatchingLearningService(db)
            lr = await learning.record_feedback(
                match_id=match_id,
                outcome=outcome,
                note=note,
                source="user_feedback",
                rejection_reason=rejection_reason,
                job_category=job_category,
            )
            learning_info = {
                "weights_adjusted": lr.weights_adjusted,
                "learning_stage": lr.learning_stage if hasattr(lr, "learning_stage") else None,
            }
            logger.info(
                f"Learning Feedback: match={match_id}, outcome={outcome}, "
                f"reason={rejection_reason}, category={job_category}"
            )
        except Exception as le:
            logger.warning(f"Learning Service Fehler (Feedback trotzdem gespeichert): {le}")

        return JSONResponse({
            "status": "ok",
            "feedback": feedback,
            "outcome": outcome,
            "rejected": is_bad,
            **learning_info,
        })

    except Exception as e:
        logger.error(f"Feedback Fehler: {e}", exc_info=True)
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)


@router.get("/api/match-center/feedback-test")
async def feedback_test():
    """Test: Ist der Feedback-Endpoint erreichbar?"""
    return JSONResponse({"status": "reachable", "method": "engine.begin()"})


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
