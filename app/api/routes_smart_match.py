"""Smart-Match API-Endpunkte - Embedding-basiertes Matching fuer Finance.

Endpunkte:
- POST /api/smart-match/embeddings/generate → Alle Finance-Embeddings generieren (Background)
- GET  /api/smart-match/embeddings/status → Embedding-Fortschritt
- POST /api/smart-match/job/{job_id} → Einzelnen Job smart-matchen
- POST /api/smart-match/generate → Alle Finance-Jobs smart-matchen (Background)
- GET  /api/smart-match/generate/status → Matching-Fortschritt
- GET  /api/smart-match/stats → Embedding-Statistiken
- POST /api/smart-match/estimate → Kosten-Schaetzung
"""

import logging
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Smart-Match"])


# ═══════════════════════════════════════════════════════════════
# IN-MEMORY STATUS TRACKING
# ═══════════════════════════════════════════════════════════════

_embedding_status: dict = {
    "running": False,
    "started_at": None,
    "step": None,
    "detail": None,
    "result": None,
    "finished_at": None,
    "error": None,
}

_matching_status: dict = {
    "running": False,
    "started_at": None,
    "step": None,
    "detail": None,
    "result": None,
    "finished_at": None,
    "error": None,
}


# ═══════════════════════════════════════════════════════════════
# EMBEDDING-ENDPUNKTE
# ═══════════════════════════════════════════════════════════════

@router.post("/api/smart-match/embeddings/generate")
async def trigger_embedding_generation(
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """Startet die Embedding-Generierung fuer alle Finance-Kandidaten und -Jobs im Hintergrund."""
    if _embedding_status["running"]:
        return {
            "status": "already_running",
            "started_at": _embedding_status["started_at"],
            "detail": _embedding_status["detail"],
        }

    _embedding_status["running"] = True
    _embedding_status["started_at"] = datetime.now(timezone.utc).isoformat()
    _embedding_status["step"] = "starting"
    _embedding_status["detail"] = "Starte Embedding-Generierung..."
    _embedding_status["result"] = None
    _embedding_status["finished_at"] = None
    _embedding_status["error"] = None

    background_tasks.add_task(_run_embedding_generation)

    return {
        "status": "started",
        "message": "Embedding-Generierung gestartet (laeuft im Hintergrund)",
    }


@router.get("/api/smart-match/embeddings/status")
async def get_embedding_status():
    """Gibt den aktuellen Status der Embedding-Generierung zurueck."""
    return _embedding_status


async def _run_embedding_generation():
    """Background-Task: Generiert Embeddings fuer alle Finance-Dokumente."""
    from app.database import async_session_maker
    from app.services.embedding_service import EmbeddingService

    def progress(step: str, detail: str):
        _embedding_status["step"] = step
        _embedding_status["detail"] = detail

    try:
        async with async_session_maker() as db:
            service = EmbeddingService(db)

            # Schritt 1: Kandidaten-Embeddings
            progress("candidates", "Generiere Kandidaten-Embeddings...")
            cand_result = await service.embed_all_finance_candidates(
                progress_callback=progress,
            )

            # Schritt 2: Job-Embeddings
            progress("jobs", "Generiere Job-Embeddings...")
            job_result = await service.embed_all_finance_jobs(
                progress_callback=progress,
            )

            # Statistiken holen
            stats = await service.get_embedding_stats()

            _embedding_status["result"] = {
                "candidates": cand_result,
                "jobs": job_result,
                "stats": stats,
                "total_cost_usd": service.total_cost_usd,
            }
            _embedding_status["step"] = "done"
            _embedding_status["detail"] = (
                f"Fertig! Kandidaten: {cand_result['embedded']}/{cand_result['total']} embedded, "
                f"Jobs: {job_result['embedded']}/{job_result['total']} embedded, "
                f"Kosten: ~${service.total_cost_usd:.4f}"
            )

            await service.close()
            await db.commit()

    except Exception as e:
        logger.exception(f"Embedding-Generierung fehlgeschlagen: {e}")
        _embedding_status["error"] = str(e)
        _embedding_status["step"] = "error"
        _embedding_status["detail"] = f"Fehler: {str(e)[:200]}"
    finally:
        _embedding_status["running"] = False
        _embedding_status["finished_at"] = datetime.now(timezone.utc).isoformat()


# ═══════════════════════════════════════════════════════════════
# SMART-MATCH ENDPUNKTE
# ═══════════════════════════════════════════════════════════════

@router.post("/api/smart-match/job/{job_id}")
async def smart_match_single_job(
    job_id: UUID,
    top_n: int = Query(default=10, ge=1, le=50),
    max_distance_km: float = Query(default=30.0, ge=1.0, le=200.0),
    db: AsyncSession = Depends(get_db),
):
    """Fuehrt Smart-Matching fuer einen einzelnen Job durch (synchron).

    Findet die Top-N aehnlichsten Finance-Kandidaten und bewertet sie durch Deep-AI.
    """
    from app.services.smart_matching_service import SmartMatchingService

    async with SmartMatchingService(db) as service:
        result = await service.match_job(
            job_id=job_id,
            top_n=top_n,
            max_distance_km=max_distance_km,
        )

    return {
        "status": "completed",
        "job_id": str(result.job_id),
        "job_position": result.job_position,
        "job_company": result.job_company,
        "candidates_found": result.embedding_candidates_found,
        "candidates_evaluated": result.deep_ai_evaluated,
        "matches_created": result.matches_created,
        "matches_updated": result.matches_updated,
        "duration_seconds": result.duration_seconds,
        "total_cost_usd": result.total_cost_usd,
        "errors": result.errors,
        "candidates": [
            {
                "candidate_id": str(c.candidate_id),
                "candidate_name": c.candidate_name,
                "similarity": c.similarity,
                "distance_km": c.distance_km,
                "ai_score": c.ai_score,
                "ai_explanation": c.ai_explanation,
                "ai_strengths": c.ai_strengths,
                "ai_weaknesses": c.ai_weaknesses,
                "ai_risks": c.ai_risks,
            }
            for c in result.candidates
        ],
    }


@router.post("/api/smart-match/generate")
async def trigger_smart_match_all(
    background_tasks: BackgroundTasks,
    top_n: int = Query(default=10, ge=1, le=50),
    max_distance_km: float = Query(default=30.0, ge=1.0, le=200.0),
    skip_matched: bool = Query(default=True, description="Bereits gematchte Jobs ueberspringen"),
    db: AsyncSession = Depends(get_db),
):
    """Startet Smart-Matching fuer ALLE aktiven Finance-Jobs im Hintergrund.

    Mit skip_matched=True (Standard) werden nur neue/ungematchte Jobs verarbeitet.
    Mit skip_matched=False wird ein vollstaendiger Re-Match aller Jobs durchgefuehrt.
    """
    if _matching_status["running"]:
        return {
            "status": "already_running",
            "started_at": _matching_status["started_at"],
            "detail": _matching_status["detail"],
        }

    mode = "inkrementell (nur neue Jobs)" if skip_matched else "komplett (alle Jobs)"
    _matching_status["running"] = True
    _matching_status["started_at"] = datetime.now(timezone.utc).isoformat()
    _matching_status["step"] = "starting"
    _matching_status["detail"] = f"Starte Smart-Matching {mode}..."
    _matching_status["result"] = None
    _matching_status["finished_at"] = None
    _matching_status["error"] = None

    background_tasks.add_task(_run_smart_match_all, top_n, max_distance_km, skip_matched)

    return {
        "status": "started",
        "message": f"Smart-Matching gestartet ({mode})",
    }


@router.get("/api/smart-match/generate/status")
async def get_smart_match_status():
    """Gibt den aktuellen Status des Smart-Matching-Laufs zurueck."""
    return _matching_status


async def _run_smart_match_all(
    top_n: int = 10,
    max_distance_km: float = 30.0,
    skip_already_matched: bool = True,
):
    """Background-Task: Smart-Matching fuer alle Finance-Jobs."""
    from app.database import async_session_maker
    from app.services.smart_matching_service import SmartMatchingService

    def progress(step: str, detail: str):
        _matching_status["step"] = step
        _matching_status["detail"] = detail

    try:
        async with async_session_maker() as db:
            service = SmartMatchingService(db)

            result = await service.match_all_finance_jobs(
                top_n=top_n,
                max_distance_km=max_distance_km,
                progress_callback=progress,
                skip_already_matched=skip_already_matched,
            )

            _matching_status["result"] = result
            _matching_status["step"] = "done"
            _matching_status["detail"] = (
                f"Fertig! {result['jobs_matched']}/{result['total_jobs']} Jobs gematcht, "
                f"{result['total_candidates_evaluated']} Kandidaten bewertet, "
                f"Kosten: ~${result['total_cost_usd']:.2f}"
            )

            await service.close()

            # Finaler Commit — mit Rollback-Fallback
            try:
                await db.commit()
            except Exception as commit_err:
                logger.warning(f"Finaler Commit fehlgeschlagen (Ergebnisse koennen trotzdem gespeichert sein): {commit_err}")
                try:
                    await db.rollback()
                except Exception:
                    pass

    except Exception as e:
        logger.exception(f"Smart-Matching fehlgeschlagen: {e}")
        _matching_status["error"] = str(e)
        _matching_status["step"] = "error"
        _matching_status["detail"] = f"Fehler: {str(e)[:200]}"
    finally:
        _matching_status["running"] = False
        _matching_status["finished_at"] = datetime.now(timezone.utc).isoformat()


# ═══════════════════════════════════════════════════════════════
# STATISTIKEN & KOSTEN
# ═══════════════════════════════════════════════════════════════

@router.get("/api/smart-match/stats")
async def get_smart_match_stats(
    db: AsyncSession = Depends(get_db),
):
    """Gibt Embedding-Statistiken zurueck (wie viele Kandidaten/Jobs haben Embeddings)."""
    from app.services.embedding_service import EmbeddingService

    service = EmbeddingService(db)
    stats = await service.get_embedding_stats()
    await service.close()

    return stats


@router.get("/api/smart-match/debug")
async def smart_match_debug(
    db: AsyncSession = Depends(get_db),
):
    """Debug-Endpunkt: Prueft pgvector Extension und Embedding-Spalten."""
    from sqlalchemy import text

    checks = {}

    # 1. pgvector Extension
    try:
        result = await db.execute(
            text("SELECT extname, extversion FROM pg_extension WHERE extname = 'vector'")
        )
        row = result.first()
        if row:
            checks["pgvector_extension"] = {"installed": True, "version": row[1]}
        else:
            checks["pgvector_extension"] = {"installed": False}
    except Exception as e:
        checks["pgvector_extension"] = {"error": str(e)}

    # 2. Embedding-Spalte auf candidates
    try:
        result = await db.execute(
            text(
                "SELECT column_name, data_type, udt_name "
                "FROM information_schema.columns "
                "WHERE table_name = 'candidates' AND column_name = 'embedding'"
            )
        )
        row = result.first()
        if row:
            checks["candidates_embedding_column"] = {
                "exists": True,
                "data_type": row[1],
                "udt_name": row[2],
            }
        else:
            checks["candidates_embedding_column"] = {"exists": False}
    except Exception as e:
        checks["candidates_embedding_column"] = {"error": str(e)}

    # 3. Embedding-Spalte auf jobs
    try:
        result = await db.execute(
            text(
                "SELECT column_name, data_type, udt_name "
                "FROM information_schema.columns "
                "WHERE table_name = 'jobs' AND column_name = 'embedding'"
            )
        )
        row = result.first()
        if row:
            checks["jobs_embedding_column"] = {
                "exists": True,
                "data_type": row[1],
                "udt_name": row[2],
            }
        else:
            checks["jobs_embedding_column"] = {"exists": False}
    except Exception as e:
        checks["jobs_embedding_column"] = {"error": str(e)}

    # 4. HNSW-Indexes
    try:
        result = await db.execute(
            text(
                "SELECT indexname, tablename FROM pg_indexes "
                "WHERE indexname LIKE '%embedding%'"
            )
        )
        indexes = [{"name": r[0], "table": r[1]} for r in result.all()]
        checks["embedding_indexes"] = indexes
    except Exception as e:
        checks["embedding_indexes"] = {"error": str(e)}

    # 5. Schnelltest: Gibt es Finance-Kandidaten?
    try:
        result = await db.execute(
            text(
                "SELECT COUNT(*) FROM candidates "
                "WHERE hotlist_category = 'FINANCE' AND hidden = false AND deleted_at IS NULL"
            )
        )
        checks["finance_candidates_count"] = result.scalar()
    except Exception as e:
        checks["finance_candidates_count"] = {"error": str(e)}

    # 6. Schnelltest: Gibt es Finance-Jobs?
    try:
        result = await db.execute(
            text(
                "SELECT COUNT(*) FROM jobs "
                "WHERE hotlist_category = 'FINANCE' AND deleted_at IS NULL"
            )
        )
        checks["finance_jobs_count"] = result.scalar()
    except Exception as e:
        checks["finance_jobs_count"] = {"error": str(e)}

    return checks


@router.post("/api/smart-match/estimate")
async def estimate_smart_match_cost(
    num_jobs: int = Query(default=100, ge=1, le=5000),
    candidates_per_job: int = Query(default=10, ge=1, le=50),
):
    """Schaetzt die Kosten fuer einen Smart-Match-Lauf."""
    from app.services.smart_matching_service import SmartMatchingService

    estimate = SmartMatchingService.estimate_cost(num_jobs, candidates_per_job)
    return estimate
