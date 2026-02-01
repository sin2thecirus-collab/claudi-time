"""Pre-Match Routes - Automatische Pre-Match-Listen.

Seiten:
- GET /pre-match           → Uebersichtsseite (Kacheln nach Beruf+Stadt)
- GET /pre-match/detail    → Detail-Seite (Kandidaten-Tabelle)

API:
- POST /api/pre-match/generate       → Pre-Matches generieren (Background)
- GET  /api/pre-match/generate/status → Status der Generierung
"""

import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.pre_match_service import PreMatchService

logger = logging.getLogger(__name__)
templates = Jinja2Templates(directory="app/templates")

router = APIRouter(tags=["Pre-Match"])


# ═══════════════════════════════════════════════════════════════
# In-Memory Status fuer Background-Generierung
# ═══════════════════════════════════════════════════════════════

_generate_status: dict = {
    "running": False,
    "started_at": None,
    "category": None,
    "progress": None,
    "result": None,
    "finished_at": None,
    "error": None,
}


# ═══════════════════════════════════════════════════════════════
# SEITEN
# ═══════════════════════════════════════════════════════════════


@router.get("/pre-match", response_class=HTMLResponse)
async def pre_match_page(
    request: Request,
    category: str = Query(default="FINANCE"),
    db: AsyncSession = Depends(get_db),
):
    """Uebersichtsseite: Kacheln nach Beruf + Stadt."""
    service = PreMatchService(db)

    groups = await service.get_overview(category)
    stats = await service.get_stats(category)

    # Gruppiere nach Beruf fuer hierarchische Darstellung
    by_profession: dict[str, list] = {}
    for g in groups:
        if g.job_title not in by_profession:
            by_profession[g.job_title] = []
        by_profession[g.job_title].append(g)

    return templates.TemplateResponse(
        "pre_match.html",
        {
            "request": request,
            "category": category,
            "groups": groups,
            "by_profession": by_profession,
            "stats": stats,
            "generate_status": _generate_status,
        },
    )


@router.get("/pre-match/detail", response_class=HTMLResponse)
async def pre_match_detail_page(
    request: Request,
    job_title: str = Query(...),
    city: str = Query(...),
    category: str = Query(default="FINANCE"),
    sort: str = Query(default="distance"),
    db: AsyncSession = Depends(get_db),
):
    """Detail-Seite: Kandidaten-Tabelle fuer eine Beruf+Stadt Kombo."""
    service = PreMatchService(db)

    matches = await service.get_detail(
        job_title=job_title,
        city=city,
        category=category,
        sort_by=sort,
    )

    # Stats fuer diese Kombo
    total = len(matches)
    close_count = sum(1 for m in matches if m.distance_km is not None and m.distance_km <= 5)
    distances = [m.distance_km for m in matches if m.distance_km is not None]
    avg_distance = round(sum(distances) / len(distances), 1) if distances else 0
    ai_checked = sum(1 for m in matches if m.ai_score is not None)

    return templates.TemplateResponse(
        "pre_match_detail.html",
        {
            "request": request,
            "job_title": job_title,
            "city": city,
            "category": category,
            "sort": sort,
            "matches": matches,
            "total": total,
            "close_count": close_count,
            "avg_distance": avg_distance,
            "ai_checked": ai_checked,
        },
    )


# ═══════════════════════════════════════════════════════════════
# API ENDPOINTS
# ═══════════════════════════════════════════════════════════════


@router.post("/api/pre-match/generate")
async def trigger_generate(
    background_tasks: BackgroundTasks,
    category: str = Query(default="FINANCE"),
    force: bool = Query(default=False),
    db: AsyncSession = Depends(get_db),
):
    """Startet die Pre-Match-Generierung im Hintergrund.

    Args:
        category: Kategorie (FINANCE/ENGINEERING)
        force: Wenn True, loescht alle alten Matches und generiert komplett neu
    """
    global _generate_status

    if _generate_status["running"]:
        return JSONResponse(
            status_code=409,
            content={
                "error": "already_running",
                "message": "Generierung laeuft bereits",
                "started_at": _generate_status["started_at"],
            },
        )

    _generate_status = {
        "running": True,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "category": category,
        "progress": "Loesche alte Matches..." if force else "Starte...",
        "result": None,
        "finished_at": None,
        "error": None,
    }

    # Background-Task mit eigener DB-Session
    background_tasks.add_task(_run_generate, category, force)

    return {
        "status": "started",
        "category": category,
        "force": force,
        "message": "Alte Matches werden geloescht + neu generiert" if force else "Pre-Match-Generierung gestartet",
    }


@router.get("/api/pre-match/generate/status")
async def get_generate_status():
    """Gibt den aktuellen Status der Generierung zurueck."""
    return _generate_status


# ═══════════════════════════════════════════════════════════════
# BACKGROUND TASK
# ═══════════════════════════════════════════════════════════════


async def _run_generate(category: str, force: bool = False):
    """Fuehrt die Pre-Match-Generierung als Background-Task aus."""
    global _generate_status

    from app.database import async_session_maker

    try:
        async with async_session_maker() as db:
            service = PreMatchService(db)

            def progress_callback(step: str, detail: str):
                _generate_status["progress"] = f"{step}: {detail}"

            if force:
                result = await service.purge_and_regenerate(
                    category=category,
                    progress_callback=progress_callback,
                )
            else:
                result = await service.generate_all(
                    category=category,
                    progress_callback=progress_callback,
                )

            _generate_status["result"] = {
                "combos_processed": result.combos_processed,
                "matches_created": result.matches_created,
                "matches_updated": result.matches_updated,
                "matches_skipped": result.matches_skipped,
                "errors_count": len(result.errors),
                "errors": result.errors[:10],
            }

    except Exception as e:
        logger.error(f"Pre-Match Generierung fehlgeschlagen: {e}", exc_info=True)
        _generate_status["error"] = str(e)[:500]

    finally:
        _generate_status["running"] = False
        _generate_status["finished_at"] = datetime.now(timezone.utc).isoformat()
