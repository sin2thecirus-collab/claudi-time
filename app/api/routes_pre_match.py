"""Pre-Match Routes - Automatische Pre-Match-Listen.

Seiten:
- GET /pre-match           → Uebersichtsseite (Kacheln nach Beruf+Stadt)
- GET /pre-match/detail    → Detail-Seite (Kandidaten-Tabelle)
- GET /pre-match/calibration → Kalibrierungs-Dashboard

API:
- POST /api/pre-match/generate       → Pre-Matches generieren (Background)
- GET  /api/pre-match/generate/status → Status der Generierung
- POST /api/calibration/run           → Kalibrierung ausfuehren
- GET  /api/calibration/status        → Kalibrierungsdaten + Statistiken
- POST /api/quick-score/run           → Quick-AI Scoring starten (Background)
- GET  /api/quick-score/status        → Quick-AI Status
"""

import asyncio
import json
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

    # JSON-Daten fuer JavaScript (cities pro Beruf) — weil tojson auf Dataclass nicht klappt
    cities_json_map: dict[str, str] = {}
    for profession, city_groups in by_profession.items():
        cities_json_map[profession] = json.dumps(
            [
                {"job_title": g.job_title, "city": g.city, "match_count": g.match_count}
                for g in city_groups
            ],
            ensure_ascii=False,
        )

    return templates.TemplateResponse(
        "pre_match.html",
        {
            "request": request,
            "category": category,
            "groups": groups,
            "by_profession": by_profession,
            "cities_json_map": cities_json_map,
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
                "matches_filtered_out": result.matches_filtered_out,
                "errors_count": len(result.errors),
                "errors": result.errors[:10],
            }

    except Exception as e:
        logger.error(f"Pre-Match Generierung fehlgeschlagen: {e}", exc_info=True)
        _generate_status["error"] = str(e)[:500]

    finally:
        _generate_status["running"] = False
        _generate_status["finished_at"] = datetime.now(timezone.utc).isoformat()


# ═══════════════════════════════════════════════════════════════
# KALIBRIERUNG — Lernt aus DeepMatch-Ergebnissen
# ═══════════════════════════════════════════════════════════════

_calibration_status: dict = {
    "running": False,
    "started_at": None,
    "result": None,
    "finished_at": None,
    "error": None,
}


@router.get("/pre-match/calibration", response_class=HTMLResponse)
async def calibration_page(
    request: Request,
    category: str = Query(default="FINANCE"),
    db: AsyncSession = Depends(get_db),
):
    """Kalibrierungs-Dashboard: Zeigt Lern-Ergebnisse und ermoeglicht Kalibrierung."""
    from app.services.calibration_service import CalibrationService

    service = CalibrationService(db)

    # Lade Statistiken ueber AI-Matches
    ai_stats = await service.get_ai_match_stats(category)

    # Lade letzte Kalibrierungsdaten (falls vorhanden)
    last_calibration = await CalibrationService.load_calibration_data(db)
    last_calibration_dict = last_calibration.to_dict() if last_calibration else None

    # Aktuelle Matrix fuer Vergleich
    from app.services.pre_scoring_service import FINANCE_ROLE_SIMILARITY
    current_matrix = {
        f"{k[0]}|{k[1]}": v
        for k, v in FINANCE_ROLE_SIMILARITY.items()
        if k[0] != k[1]  # Keine Diagonale (immer 1.0)
    }

    return templates.TemplateResponse(
        "calibration.html",
        {
            "request": request,
            "category": category,
            "ai_stats": ai_stats,
            "last_calibration": last_calibration_dict,
            "last_calibration_json": json.dumps(last_calibration_dict, ensure_ascii=False) if last_calibration_dict else "null",
            "current_matrix_json": json.dumps(current_matrix, ensure_ascii=False),
            "calibration_status": _calibration_status,
        },
    )


@router.post("/api/calibration/run")
async def trigger_calibration(
    background_tasks: BackgroundTasks,
    category: str = Query(default="FINANCE"),
    db: AsyncSession = Depends(get_db),
):
    """Startet die Kalibrierung im Hintergrund."""
    global _calibration_status

    if _calibration_status["running"]:
        return JSONResponse(
            status_code=409,
            content={
                "error": "already_running",
                "message": "Kalibrierung laeuft bereits",
            },
        )

    _calibration_status = {
        "running": True,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "result": None,
        "finished_at": None,
        "error": None,
    }

    background_tasks.add_task(_run_calibration, category)

    return {
        "status": "started",
        "category": category,
        "message": "Kalibrierung gestartet — analysiere AI-Bewertungen...",
    }


@router.get("/api/calibration/status")
async def get_calibration_status():
    """Gibt den aktuellen Status der Kalibrierung zurueck."""
    return _calibration_status


@router.get("/api/calibration/data")
async def get_calibration_data(
    category: str = Query(default="FINANCE"),
    db: AsyncSession = Depends(get_db),
):
    """Gibt die gespeicherten Kalibrierungsdaten zurueck (JSON)."""
    from app.services.calibration_service import CalibrationService

    data = await CalibrationService.load_calibration_data(db)
    if not data:
        return JSONResponse(
            status_code=404,
            content={"error": "no_data", "message": "Keine Kalibrierungsdaten vorhanden"},
        )
    return data.to_dict()


@router.get("/api/calibration/ai-stats")
async def get_ai_stats(
    category: str = Query(default="FINANCE"),
    db: AsyncSession = Depends(get_db),
):
    """Gibt Statistiken ueber AI-bewertete Matches zurueck."""
    from app.services.calibration_service import CalibrationService

    service = CalibrationService(db)
    return await service.get_ai_match_stats(category)


async def _run_calibration(category: str):
    """Fuehrt die Kalibrierung als Background-Task aus."""
    global _calibration_status

    from app.database import async_session_maker
    from app.services.calibration_service import CalibrationService

    try:
        async with async_session_maker() as db:
            service = CalibrationService(db)

            # Kalibrierung ausfuehren
            result = await service.run_calibration(category)

            # Ergebnis in DB speichern
            await service.save_calibration_data(result)

            _calibration_status["result"] = result.to_dict()

    except Exception as e:
        logger.error(f"Kalibrierung fehlgeschlagen: {e}", exc_info=True)
        _calibration_status["error"] = str(e)[:500]

    finally:
        _calibration_status["running"] = False
        _calibration_status["finished_at"] = datetime.now(timezone.utc).isoformat()


# ═══════════════════════════════════════════════════════════════
# QUICK-AI SCORE — Guenstige KI-Schnellbewertung (Phase C)
# ═══════════════════════════════════════════════════════════════

_quick_score_status: dict = {
    "running": False,
    "started_at": None,
    "progress": None,
    "result": None,
    "finished_at": None,
    "error": None,
}


@router.post("/api/quick-score/run")
async def trigger_quick_score(
    background_tasks: BackgroundTasks,
    category: str = Query(default="FINANCE"),
    max_matches: int = Query(default=500),
):
    """Startet Quick-AI Scoring im Hintergrund.

    Bewertet alle Matches ohne Quick-Score mit einer guenstigen KI-Schnellbewertung.
    Kosten: ~$0.04 pro 1000 Matches.
    """
    global _quick_score_status

    if _quick_score_status["running"]:
        return JSONResponse(
            status_code=409,
            content={
                "error": "already_running",
                "message": "Quick-AI laeuft bereits",
            },
        )

    _quick_score_status = {
        "running": True,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "progress": "Starte Quick-AI...",
        "result": None,
        "finished_at": None,
        "error": None,
    }

    background_tasks.add_task(_run_quick_score, category, max_matches)

    return {
        "status": "started",
        "category": category,
        "max_matches": max_matches,
        "message": "Quick-AI Scoring gestartet...",
    }


@router.get("/api/quick-score/status")
async def get_quick_score_status():
    """Gibt den aktuellen Status des Quick-AI Scorings zurueck."""
    return _quick_score_status


async def _run_quick_score(category: str, max_matches: int = 500):
    """Fuehrt Quick-AI Scoring als Background-Task aus."""
    global _quick_score_status

    from app.database import async_session_maker
    from app.services.quick_score_service import QuickScoreService

    try:
        async with async_session_maker() as db:
            service = QuickScoreService(db)

            def progress_callback(step: str, detail: str):
                _quick_score_status["progress"] = f"{step}: {detail}"

            try:
                result = await service.score_batch(
                    category=category,
                    max_matches=max_matches,
                    progress_callback=progress_callback,
                )

                _quick_score_status["result"] = {
                    "total_matches": result.total_matches,
                    "scored": result.scored,
                    "skipped_error": result.skipped_error,
                    "avg_score": result.avg_score,
                    "total_cost_usd": result.total_cost_usd,
                    "errors": result.errors[:10],
                }
            finally:
                await service.close()

    except Exception as e:
        logger.error(f"Quick-AI Scoring fehlgeschlagen: {e}", exc_info=True)
        _quick_score_status["error"] = str(e)[:500]

    finally:
        _quick_score_status["running"] = False
        _quick_score_status["finished_at"] = datetime.now(timezone.utc).isoformat()
