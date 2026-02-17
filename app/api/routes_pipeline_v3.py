"""Matching Pipeline V3 API-Endpunkte.

Endpunkte:
- POST /api/v3/match/job/{job_id}       → Einzelnen Job matchen (sync)
- POST /api/v3/match/candidate/{id}     → Einzelnen Kandidaten matchen (sync)
- POST /api/v3/match/all                → Alle Finance-Jobs matchen (Background)
- GET  /api/v3/match/status             → Batch-Fortschritt
- POST /api/v3/match/cleanup-legacy     → Alte Matches loeschen
- GET  /api/v3/stats                    → Pipeline-Statistiken
"""

import logging
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from sqlalchemy import select, func, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.match import Match
from app.models.job import Job
from app.models.candidate import Candidate

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Pipeline-V3"])


# ═══════════════════════════════════════════════════════════════
# IN-MEMORY STATUS TRACKING
# ═══════════════════════════════════════════════════════════════

_batch_status: dict = {
    "running": False,
    "started_at": None,
    "step": None,
    "detail": None,
    "jobs_processed": 0,
    "jobs_total": 0,
    "matches_created": 0,
    "cost_usd": 0.0,
    "result": None,
    "finished_at": None,
    "error": None,
}


# ═══════════════════════════════════════════════════════════════
# EINZELNER JOB
# ═══════════════════════════════════════════════════════════════


@router.post("/api/v3/match/job/{job_id}")
async def match_single_job(
    job_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Fuehrt die V3-Pipeline fuer einen einzelnen Job aus (synchron).

    Phase 2: Rollen-Gated Filterung (SQL)
    Phase 3: KI Deep-Evaluation (GPT-4o-mini)

    Nur Matches mit AI-Score >= 50% werden gespeichert.
    """
    from app.services.matching_pipeline_v3 import MatchingPipelineV3

    async with MatchingPipelineV3(db) as pipeline:
        result = await pipeline.run_for_job(job_id)

    return {
        "job_id": str(result.job_id),
        "job_position": result.job_position,
        "job_company": result.job_company,
        "job_role": result.job_role,
        "phase2_candidates": result.phase2_candidates_found,
        "phase3_evaluated": result.phase3_evaluated,
        "matches_created": result.matches_created,
        "matches_updated": result.matches_updated,
        "matches_skipped_low_score": result.matches_skipped_low_score,
        "cost_usd": result.total_cost_usd,
        "duration_seconds": result.duration_seconds,
        "errors": result.errors,
    }


# ═══════════════════════════════════════════════════════════════
# EINZELNER KANDIDAT
# ═══════════════════════════════════════════════════════════════


@router.post("/api/v3/match/candidate/{candidate_id}")
async def match_single_candidate(
    candidate_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Fuehrt die V3-Reverse-Pipeline fuer einen Kandidaten aus (synchron).

    Findet alle kompatiblen Jobs und bewertet den Kandidaten per KI.
    """
    from app.services.matching_pipeline_v3 import MatchingPipelineV3

    async with MatchingPipelineV3(db) as pipeline:
        result = await pipeline.run_for_candidate(candidate_id)

    return result


# ═══════════════════════════════════════════════════════════════
# BATCH: ALLE FINANCE-JOBS
# ═══════════════════════════════════════════════════════════════


@router.post("/api/v3/match/all")
async def trigger_batch_matching(
    background_tasks: BackgroundTasks,
    force: bool = Query(
        default=False,
        description="Wenn True, auch bereits gematchte Jobs erneut matchen",
    ),
    db: AsyncSession = Depends(get_db),
):
    """Startet V3-Matching fuer alle Finance-Jobs im Hintergrund.

    Standardmaessig werden nur Jobs gematcht, die noch keine pipeline_v3
    Matches haben (inkrementell). Mit ?force=true werden ALLE Jobs gematcht.
    """
    if _batch_status["running"]:
        return {
            "status": "already_running",
            "started_at": _batch_status["started_at"],
            "detail": _batch_status["detail"],
            "jobs_processed": _batch_status["jobs_processed"],
        }

    # Status initialisieren
    _batch_status["running"] = True
    _batch_status["started_at"] = datetime.now(timezone.utc).isoformat()
    _batch_status["step"] = "starting"
    _batch_status["detail"] = "Starte V3-Matching..."
    _batch_status["jobs_processed"] = 0
    _batch_status["jobs_total"] = 0
    _batch_status["matches_created"] = 0
    _batch_status["cost_usd"] = 0.0
    _batch_status["result"] = None
    _batch_status["finished_at"] = None
    _batch_status["error"] = None

    background_tasks.add_task(_run_batch_matching, not force)

    return {
        "status": "started",
        "message": "V3-Matching gestartet (laeuft im Hintergrund)",
        "force": force,
    }


async def _run_batch_matching(skip_already_matched: bool):
    """Background-Task: V3-Pipeline fuer alle Finance-Jobs."""
    from app.database import async_session_maker
    from app.services.matching_pipeline_v3 import MatchingPipelineV3

    try:
        async with async_session_maker() as db:
            async with MatchingPipelineV3(db) as pipeline:

                def progress_cb(step: str, detail: str):
                    _batch_status["step"] = step
                    _batch_status["detail"] = detail

                result = await pipeline.run_all(
                    skip_already_matched=skip_already_matched,
                    progress_callback=progress_cb,
                )

                _batch_status["result"] = result
                _batch_status["matches_created"] = result.get(
                    "total_matches_created", 0
                )
                _batch_status["cost_usd"] = result.get("total_cost_usd", 0.0)
                _batch_status["jobs_processed"] = result.get("jobs_matched", 0)
                _batch_status["jobs_total"] = result.get("total_jobs", 0)

    except Exception as e:
        logger.error(f"V3 Batch-Fehler: {e}")
        _batch_status["error"] = str(e)
    finally:
        _batch_status["running"] = False
        _batch_status["finished_at"] = datetime.now(timezone.utc).isoformat()


@router.get("/api/v3/match/status")
async def get_batch_status():
    """Gibt den aktuellen Status des Batch-Matching-Laufs zurueck."""
    return _batch_status


# ═══════════════════════════════════════════════════════════════
# LEGACY-CLEANUP
# ═══════════════════════════════════════════════════════════════


@router.post("/api/v3/match/cleanup-legacy")
async def cleanup_legacy_matches(
    dry_run: bool = Query(default=True, description="Wenn True: nur zaehlen"),
    db: AsyncSession = Depends(get_db),
):
    """Loescht alle Matches die NICHT von Pipeline V3 erstellt wurden.

    Standardmaessig dry_run=true (nur zaehlen, nicht loeschen).
    """
    from sqlalchemy import text

    # Zaehlen
    row = (await db.execute(text("""
        SELECT
            (SELECT COUNT(*) FROM matches
             WHERE matching_method != 'pipeline_v3' OR matching_method IS NULL) as legacy,
            (SELECT COUNT(*) FROM matches
             WHERE matching_method = 'pipeline_v3') as v3
    """))).fetchone()
    legacy_count = row[0] or 0
    v3_count = row[1] or 0

    if dry_run:
        return {
            "dry_run": True,
            "legacy_matches": legacy_count,
            "v3_matches": v3_count,
            "message": f"{legacy_count} Legacy-Matches wuerden geloescht. "
            f"{v3_count} V3-Matches bleiben erhalten.",
        }

    # Loeschen
    result = await db.execute(text(
        "DELETE FROM matches WHERE matching_method != 'pipeline_v3' OR matching_method IS NULL"
    ))
    await db.commit()
    deleted = result.rowcount

    return {
        "dry_run": False,
        "deleted": deleted,
        "v3_matches_remaining": v3_count,
        "message": f"{deleted} Legacy-Matches geloescht.",
    }


# ═══════════════════════════════════════════════════════════════
# STATISTIKEN
# ═══════════════════════════════════════════════════════════════


@router.get("/api/v3/stats")
async def get_pipeline_stats(
    db: AsyncSession = Depends(get_db),
):
    """Pipeline-V3-Statistiken: Klassifizierung, Matches, Verteilung."""
    from sqlalchemy import text

    try:
        # Alle Stats mit Raw SQL fuer maximale Zuverlaessigkeit
        stats_query = text("""
            SELECT
                (SELECT COUNT(*) FROM candidates
                 WHERE hotlist_category = 'FINANCE'
                   AND classification_data IS NOT NULL
                   AND deleted_at IS NULL AND hidden = false) as cand_classified,
                (SELECT COUNT(*) FROM candidates
                 WHERE hotlist_category = 'FINANCE'
                   AND deleted_at IS NULL AND hidden = false) as cand_total,
                (SELECT COUNT(*) FROM jobs
                 WHERE hotlist_category = 'FINANCE'
                   AND classification_data IS NOT NULL
                   AND deleted_at IS NULL) as job_classified,
                (SELECT COUNT(*) FROM jobs
                 WHERE hotlist_category = 'FINANCE'
                   AND deleted_at IS NULL) as job_total,
                (SELECT COUNT(*) FROM matches
                 WHERE matching_method = 'pipeline_v3') as v3_matches,
                (SELECT COUNT(*) FROM matches
                 WHERE matching_method != 'pipeline_v3'
                    OR matching_method IS NULL) as legacy_matches
        """)
        row = (await db.execute(stats_query)).fetchone()

        # Rollen-Verteilung
        role_query = text("""
            SELECT COALESCE(hotlist_job_title, 'Nicht klassifiziert') as role,
                   COUNT(*) as cnt
            FROM candidates
            WHERE hotlist_category = 'FINANCE'
              AND classification_data IS NOT NULL
              AND deleted_at IS NULL AND hidden = false
            GROUP BY hotlist_job_title
            ORDER BY cnt DESC
        """)
        role_rows = (await db.execute(role_query)).fetchall()

        return {
            "candidates": {
                "classified": row[0] or 0,
                "total_finance": row[1] or 0,
            },
            "jobs": {
                "classified": row[2] or 0,
                "total_finance": row[3] or 0,
            },
            "matches": {
                "v3_total": row[4] or 0,
                "legacy_total": row[5] or 0,
            },
            "role_distribution": {
                r[0]: r[1] for r in role_rows
            },
        }
    except Exception as e:
        logger.error(f"V3 Stats Fehler: {e}", exc_info=True)
        return {"error": str(e)}
