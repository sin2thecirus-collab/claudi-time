"""Matching Engine v2 — API Routes.

Sprint 1: Profile-Erstellung, Backfill, Stats.
Sprint 2: Matching, Embedding-Generierung, Batch-Matching.
"""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, BackgroundTasks, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.profile_engine_service import ProfileEngineService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Matching v2"])


# ══════════════════════════════════════════════════════════════════
# Profile-Erstellung
# ══════════════════════════════════════════════════════════════════

@router.post("/profiles/candidate/{candidate_id}")
async def create_candidate_profile(
    candidate_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Erstellt ein strukturiertes v2-Profil fuer einen Kandidaten via GPT-4o-mini."""
    service = ProfileEngineService(db)
    try:
        profile = await service.create_candidate_profile(candidate_id)
        if not profile.success:
            raise HTTPException(status_code=400, detail=profile.error)

        return {
            "status": "ok",
            "candidate_id": str(candidate_id),
            "seniority_level": profile.seniority_level,
            "career_trajectory": profile.career_trajectory,
            "years_experience": profile.years_experience,
            "current_role_summary": profile.current_role_summary,
            "skills_count": len(profile.structured_skills),
            "cost_usd": profile.cost_usd,
        }
    finally:
        await service.close()


@router.post("/profiles/job/{job_id}")
async def create_job_profile(
    job_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Erstellt ein strukturiertes v2-Profil fuer einen Job via GPT-4o-mini."""
    service = ProfileEngineService(db)
    try:
        profile = await service.create_job_profile(job_id)
        if not profile.success:
            raise HTTPException(status_code=400, detail=profile.error)

        return {
            "status": "ok",
            "job_id": str(job_id),
            "seniority_level": profile.seniority_level,
            "role_summary": profile.role_summary,
            "skills_count": len(profile.required_skills),
            "cost_usd": profile.cost_usd,
        }
    finally:
        await service.close()


# ══════════════════════════════════════════════════════════════════
# Backfill (Background)
# ══════════════════════════════════════════════════════════════════

# In-memory Tracking fuer Backfill-Fortschritt
_backfill_status: dict = {
    "running": False,
    "type": None,
    "processed": 0,
    "total": 0,
    "cost_usd": 0.0,
    "result": None,
}


async def _run_backfill(entity_type: str, max_total: int, batch_size: int, force_reprofile: bool = False):
    """Background-Task fuer Backfill."""
    from app.database import async_session_maker

    _backfill_status["running"] = True
    _backfill_status["type"] = f"reprofile_{entity_type}" if force_reprofile else entity_type
    _backfill_status["processed"] = 0
    _backfill_status["total"] = 0
    _backfill_status["cost_usd"] = 0.0
    _backfill_status["result"] = None

    try:
        async with async_session_maker() as db:
            service = ProfileEngineService(db)
            try:
                def on_progress(processed, total):
                    _backfill_status["processed"] = processed
                    _backfill_status["total"] = total

                if entity_type == "candidates":
                    result = await service.backfill_candidates(
                        batch_size=batch_size,
                        max_total=max_total,
                        progress_callback=on_progress,
                        force_reprofile=force_reprofile,
                    )
                elif entity_type == "jobs":
                    result = await service.backfill_jobs(
                        batch_size=batch_size,
                        max_total=max_total,
                        progress_callback=on_progress,
                        force_reprofile=force_reprofile,
                    )
                elif entity_type == "all":
                    # Erst Kandidaten, dann Jobs
                    result_c = await service.backfill_candidates(
                        batch_size=batch_size,
                        max_total=max_total,
                        progress_callback=on_progress,
                        force_reprofile=force_reprofile,
                    )
                    result_j = await service.backfill_jobs(
                        batch_size=batch_size,
                        max_total=max_total,
                        progress_callback=on_progress,
                        force_reprofile=force_reprofile,
                    )
                    result = {
                        "candidates": {
                            "profiled": result_c.profiled,
                            "skipped": result_c.skipped,
                            "failed": result_c.failed,
                            "cost_usd": result_c.total_cost_usd,
                        },
                        "jobs": {
                            "profiled": result_j.profiled,
                            "skipped": result_j.skipped,
                            "failed": result_j.failed,
                            "cost_usd": result_j.total_cost_usd,
                        },
                        "total_cost_usd": result_c.total_cost_usd + result_j.total_cost_usd,
                    }
                    _backfill_status["result"] = result
                    _backfill_status["cost_usd"] = result_c.total_cost_usd + result_j.total_cost_usd
                    return

                _backfill_status["result"] = {
                    "profiled": result.profiled,
                    "skipped": result.skipped,
                    "failed": result.failed,
                    "cost_usd": result.total_cost_usd,
                    "errors": result.errors[:10],
                }
                _backfill_status["cost_usd"] = result.total_cost_usd
            finally:
                await service.close()
    except Exception as e:
        logger.error(f"Backfill Fehler: {e}", exc_info=True)
        _backfill_status["result"] = {"error": str(e)}
    finally:
        _backfill_status["running"] = False


@router.post("/profiles/backfill")
async def start_backfill(
    background_tasks: BackgroundTasks,
    entity_type: str = "all",  # "candidates", "jobs", "all"
    max_total: int = 0,  # 0 = alle
    batch_size: int = 50,
    force_reprofile: bool = False,  # True = alle Profile neu erstellen (v2.5 Upgrade)
):
    """Startet Backfill im Background: Alle Kandidaten/Jobs ohne v2-Profil werden profiliert.

    Args:
        entity_type: "candidates", "jobs", oder "all"
        max_total: Maximum (0 = alle)
        batch_size: Batch-Groesse fuer Commits
        force_reprofile: True = ALLE Profile neu erstellen (fuer v2.5 Upgrade, ~$1)
    """
    if _backfill_status["running"]:
        return JSONResponse(
            status_code=409,
            content={
                "status": "already_running",
                "type": _backfill_status["type"],
                "processed": _backfill_status["processed"],
                "total": _backfill_status["total"],
            },
        )

    if entity_type not in ("candidates", "jobs", "all"):
        raise HTTPException(status_code=400, detail="entity_type muss 'candidates', 'jobs' oder 'all' sein")

    background_tasks.add_task(_run_backfill, entity_type, max_total, batch_size, force_reprofile)

    return {
        "status": "started",
        "entity_type": entity_type,
        "max_total": max_total if max_total > 0 else "unbegrenzt",
        "batch_size": batch_size,
    }


@router.get("/profiles/backfill/status")
async def get_backfill_status():
    """Gibt den aktuellen Backfill-Fortschritt zurueck."""
    return {
        "running": _backfill_status["running"],
        "type": _backfill_status["type"],
        "processed": _backfill_status["processed"],
        "total": _backfill_status["total"],
        "cost_usd": round(_backfill_status["cost_usd"], 4),
        "result": _backfill_status["result"],
    }


# ══════════════════════════════════════════════════════════════════
# Stats
# ══════════════════════════════════════════════════════════════════

@router.get("/profiles/stats")
async def get_profile_stats(db: AsyncSession = Depends(get_db)):
    """Gibt Statistiken ueber den Profil-Status zurueck."""
    service = ProfileEngineService(db)
    stats = await service.get_profile_stats()
    return stats


# ══════════════════════════════════════════════════════════════════
# Scoring Weights
# ══════════════════════════════════════════════════════════════════

@router.get("/weights")
async def get_scoring_weights(db: AsyncSession = Depends(get_db)):
    """Gibt die aktuellen Scoring-Gewichte zurueck."""
    from sqlalchemy import select
    from app.models.match_v2_models import MatchV2ScoringWeight

    result = await db.execute(
        select(MatchV2ScoringWeight).order_by(MatchV2ScoringWeight.component)
    )
    weights = result.scalars().all()

    return {
        "weights": [
            {
                "component": w.component,
                "weight": w.weight,
                "default_weight": w.default_weight,
                "adjustment_count": w.adjustment_count,
                "last_adjusted_at": w.last_adjusted_at.isoformat() if w.last_adjusted_at else None,
            }
            for w in weights
        ]
    }


@router.get("/rules")
async def get_learned_rules(
    active_only: bool = True,
    db: AsyncSession = Depends(get_db),
):
    """Gibt die gelernten Matching-Regeln zurueck."""
    from sqlalchemy import select
    from app.models.match_v2_models import MatchV2LearnedRule

    query = select(MatchV2LearnedRule).order_by(MatchV2LearnedRule.confidence.desc())
    if active_only:
        query = query.where(MatchV2LearnedRule.active == True)

    result = await db.execute(query)
    rules = result.scalars().all()

    return {
        "rules": [
            {
                "id": str(r.id),
                "rule_type": r.rule_type,
                "rule_json": r.rule_json,
                "confidence": r.confidence,
                "support_count": r.support_count,
                "active": r.active,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rules
        ],
        "total": len(rules),
    }


# ══════════════════════════════════════════════════════════════════
# Matching (Sprint 2)
# ══════════════════════════════════════════════════════════════════

@router.post("/match/job/{job_id}")
async def match_single_job(
    job_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Matcht einen einzelnen Job gegen alle passenden Kandidaten.

    3-Schichten-Pipeline:
    1. Hard Filters (SQL, <5ms)
    2. Structured Scoring (7-Komponenten, <50ms)
    3. Pattern Boost (gelernte Regeln)

    Kosten: $0.00 (alles vorberechnet)
    """
    from app.services.matching_engine_v2 import MatchingEngineV2

    engine = MatchingEngineV2(db)
    try:
        result = await engine.match_job(job_id, save_to_db=True)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Match Fehler fuer Job {job_id}: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"error": str(e), "type": type(e).__name__},
        )

    return {
        "status": "ok",
        "job_id": str(job_id),
        "matches_found": len(result.matches),
        "candidates_checked": result.total_candidates_checked,
        "candidates_after_filter": result.candidates_after_filter,
        "duration_ms": result.duration_ms,
        "top_matches": [
            {
                "candidate_id": str(m.candidate_id),
                "score": m.total_score,
                "rank": m.rank,
                "breakdown": m.breakdown,
            }
            for m in result.matches[:10]  # Nur Top 10 in Response
        ],
    }


# In-memory Tracking fuer Batch-Matching
_batch_match_status: dict = {
    "running": False,
    "processed": 0,
    "total": 0,
    "matches_created": 0,
    "result": None,
}


async def _run_batch_matching(max_jobs: int, unmatched_only: bool):
    """Background-Task fuer Batch-Matching."""
    from app.database import async_session_maker
    from app.services.matching_engine_v2 import MatchingEngineV2

    _batch_match_status["running"] = True
    _batch_match_status["processed"] = 0
    _batch_match_status["total"] = 0
    _batch_match_status["matches_created"] = 0
    _batch_match_status["result"] = None

    try:
        async with async_session_maker() as db:
            engine = MatchingEngineV2(db)

            def on_progress(processed, total):
                _batch_match_status["processed"] = processed
                _batch_match_status["total"] = total

            result = await engine.match_batch(
                max_jobs=max_jobs,
                unmatched_only=unmatched_only,
                progress_callback=on_progress,
            )

            _batch_match_status["result"] = {
                "jobs_matched": result.jobs_matched,
                "total_matches_created": result.total_matches_created,
                "duration_ms": round(result.total_duration_ms, 1),
                "errors": result.errors[:10],
            }
            _batch_match_status["matches_created"] = result.total_matches_created

    except Exception as e:
        logger.error(f"Batch-Matching Fehler: {e}", exc_info=True)
        _batch_match_status["result"] = {"error": str(e)}
    finally:
        _batch_match_status["running"] = False


@router.post("/match/batch")
async def start_batch_matching(
    background_tasks: BackgroundTasks,
    max_jobs: int = 0,
    unmatched_only: bool = True,
):
    """Startet Batch-Matching: Alle profilierten Jobs gegen alle Kandidaten.

    Args:
        max_jobs: Maximum Jobs (0 = alle)
        unmatched_only: Nur Jobs ohne bestehende v2-Matches
    """
    if _batch_match_status["running"]:
        return JSONResponse(
            status_code=409,
            content={
                "status": "already_running",
                "processed": _batch_match_status["processed"],
                "total": _batch_match_status["total"],
            },
        )

    background_tasks.add_task(_run_batch_matching, max_jobs, unmatched_only)

    return {
        "status": "started",
        "max_jobs": max_jobs if max_jobs > 0 else "unbegrenzt",
        "unmatched_only": unmatched_only,
    }


@router.get("/match/batch/status")
async def get_batch_match_status():
    """Gibt den aktuellen Batch-Matching-Fortschritt zurueck."""
    return {
        "running": _batch_match_status["running"],
        "processed": _batch_match_status["processed"],
        "total": _batch_match_status["total"],
        "matches_created": _batch_match_status["matches_created"],
        "result": _batch_match_status["result"],
    }


# ══════════════════════════════════════════════════════════════════
# Embedding-Generierung (Sprint 2)
# ══════════════════════════════════════════════════════════════════

_embedding_status: dict = {
    "running": False,
    "type": None,
    "processed": 0,
    "total": 0,
    "result": None,
}


async def _run_embedding_generation(entity_type: str, max_total: int):
    """Background-Task fuer Embedding-Generierung."""
    from app.database import async_session_maker
    from app.services.matching_engine_v2 import EmbeddingGenerationService

    _embedding_status["running"] = True
    _embedding_status["type"] = entity_type
    _embedding_status["processed"] = 0
    _embedding_status["total"] = 0
    _embedding_status["result"] = None

    try:
        async with async_session_maker() as db:
            service = EmbeddingGenerationService(db)

            def on_progress(processed, total):
                _embedding_status["processed"] = processed
                _embedding_status["total"] = total

            try:
                if entity_type == "candidates":
                    result = await service.generate_candidate_embeddings(
                        max_total=max_total,
                        progress_callback=on_progress,
                    )
                elif entity_type == "jobs":
                    result = await service.generate_job_embeddings(
                        max_total=max_total,
                        progress_callback=on_progress,
                    )
                elif entity_type == "all":
                    result_c = await service.generate_candidate_embeddings(
                        max_total=max_total,
                        progress_callback=on_progress,
                    )
                    result_j = await service.generate_job_embeddings(
                        max_total=max_total,
                        progress_callback=on_progress,
                    )
                    result = {
                        "candidates": result_c,
                        "jobs": result_j,
                    }
                else:
                    result = {"error": "Unbekannter entity_type"}

                _embedding_status["result"] = result
            finally:
                await service.close()

    except Exception as e:
        logger.error(f"Embedding-Generierung Fehler: {e}", exc_info=True)
        _embedding_status["result"] = {"error": str(e)}
    finally:
        _embedding_status["running"] = False


@router.post("/embeddings/generate")
async def start_embedding_generation(
    background_tasks: BackgroundTasks,
    entity_type: str = "all",
    max_total: int = 0,
):
    """Generiert Embeddings fuer alle profilierten Kandidaten/Jobs ohne Embedding.

    Voraussetzung: Profile muessen zuerst via Backfill erstellt werden.
    Kosten: ~$0.05 fuer alle Dokumente (einmalig).

    Args:
        entity_type: "candidates", "jobs", oder "all"
        max_total: Maximum (0 = alle)
    """
    if _embedding_status["running"]:
        return JSONResponse(
            status_code=409,
            content={
                "status": "already_running",
                "type": _embedding_status["type"],
                "processed": _embedding_status["processed"],
                "total": _embedding_status["total"],
            },
        )

    if entity_type not in ("candidates", "jobs", "all"):
        raise HTTPException(status_code=400, detail="entity_type muss 'candidates', 'jobs' oder 'all' sein")

    background_tasks.add_task(_run_embedding_generation, entity_type, max_total)

    return {
        "status": "started",
        "entity_type": entity_type,
        "max_total": max_total if max_total > 0 else "unbegrenzt",
    }


@router.get("/embeddings/status")
async def get_embedding_status():
    """Gibt den aktuellen Embedding-Generierungs-Fortschritt zurueck."""
    return {
        "running": _embedding_status["running"],
        "type": _embedding_status["type"],
        "processed": _embedding_status["processed"],
        "total": _embedding_status["total"],
        "result": _embedding_status["result"],
    }


# ══════════════════════════════════════════════════════════════════
# Full Pipeline: Profile → Embedding → Match (Sprint 2)
# ══════════════════════════════════════════════════════════════════

_pipeline_status: dict = {
    "running": False,
    "phase": None,
    "processed": 0,
    "total": 0,
    "result": None,
}


async def _run_full_pipeline(max_total: int):
    """Background-Task fuer komplette Pipeline: Profile → Embeddings → Matching."""
    from app.database import async_session_maker
    from app.services.matching_engine_v2 import EmbeddingGenerationService, MatchingEngineV2

    _pipeline_status["running"] = True
    _pipeline_status["result"] = None
    results = {}

    try:
        # Phase 1: Profile Backfill
        _pipeline_status["phase"] = "profiles"
        async with async_session_maker() as db:
            service = ProfileEngineService(db)
            try:
                def on_profile_progress(p, t):
                    _pipeline_status["processed"] = p
                    _pipeline_status["total"] = t

                # Kandidaten profilieren
                result_c = await service.backfill_candidates(
                    max_total=max_total,
                    progress_callback=on_profile_progress,
                )
                # Jobs profilieren
                result_j = await service.backfill_jobs(
                    max_total=max_total,
                    progress_callback=on_profile_progress,
                )
                results["profiles"] = {
                    "candidates_profiled": result_c.profiled,
                    "jobs_profiled": result_j.profiled,
                    "cost_usd": round(result_c.total_cost_usd + result_j.total_cost_usd, 4),
                }
            finally:
                await service.close()

        # Phase 2: Embeddings
        _pipeline_status["phase"] = "embeddings"
        async with async_session_maker() as db:
            emb_service = EmbeddingGenerationService(db)
            try:
                def on_emb_progress(p, t):
                    _pipeline_status["processed"] = p
                    _pipeline_status["total"] = t

                result_ce = await emb_service.generate_candidate_embeddings(
                    max_total=max_total,
                    progress_callback=on_emb_progress,
                )
                result_je = await emb_service.generate_job_embeddings(
                    max_total=max_total,
                    progress_callback=on_emb_progress,
                )
                results["embeddings"] = {
                    "candidates": result_ce.get("generated", 0),
                    "jobs": result_je.get("generated", 0),
                }
            finally:
                await emb_service.close()

        # Phase 3: Matching
        _pipeline_status["phase"] = "matching"
        async with async_session_maker() as db:
            engine = MatchingEngineV2(db)

            def on_match_progress(p, t):
                _pipeline_status["processed"] = p
                _pipeline_status["total"] = t

            match_result = await engine.match_batch(
                max_jobs=max_total,
                unmatched_only=False,
                progress_callback=on_match_progress,
            )
            results["matching"] = {
                "jobs_matched": match_result.jobs_matched,
                "matches_created": match_result.total_matches_created,
                "duration_ms": round(match_result.total_duration_ms, 1),
                "errors": match_result.errors[:10] if match_result.errors else [],
            }

        _pipeline_status["result"] = results

    except Exception as e:
        logger.error(f"Pipeline Fehler in Phase {_pipeline_status['phase']}: {e}", exc_info=True)
        _pipeline_status["result"] = {"error": str(e), "phase": _pipeline_status["phase"], "partial_results": results}
    finally:
        _pipeline_status["running"] = False


@router.post("/pipeline/run")
async def start_full_pipeline(
    background_tasks: BackgroundTasks,
    max_total: int = 0,
):
    """Startet die komplette v2-Pipeline: Profile → Embeddings → Matching.

    Ein Klick fuer alles. Laueft im Background.

    Args:
        max_total: Maximum pro Phase (0 = alle)
    """
    if _pipeline_status["running"]:
        return JSONResponse(
            status_code=409,
            content={
                "status": "already_running",
                "phase": _pipeline_status["phase"],
                "processed": _pipeline_status["processed"],
                "total": _pipeline_status["total"],
            },
        )

    background_tasks.add_task(_run_full_pipeline, max_total)

    return {
        "status": "started",
        "phases": ["profiles", "embeddings", "matching"],
        "max_total": max_total if max_total > 0 else "unbegrenzt",
    }


@router.get("/pipeline/status")
async def get_pipeline_status():
    """Gibt den aktuellen Pipeline-Fortschritt zurueck."""
    return {
        "running": _pipeline_status["running"],
        "phase": _pipeline_status["phase"],
        "processed": _pipeline_status["processed"],
        "total": _pipeline_status["total"],
        "result": _pipeline_status["result"],
    }


@router.get("/pipeline/debug-match/{job_id}")
async def debug_match_job(job_id: UUID, db: AsyncSession = Depends(get_db)):
    """Debug: Matcht einen Job und gibt detaillierte Fehler zurueck."""
    import traceback
    try:
        from app.services.matching_engine_v2 import MatchingEngineV2
        engine = MatchingEngineV2(db)
        result = await engine.match_job(job_id, save_to_db=False)
        return {
            "status": "ok",
            "matches_found": len(result.matches),
            "candidates_after_filter": result.candidates_after_filter,
            "duration_ms": result.duration_ms,
        }
    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "type": type(e).__name__,
            "traceback": traceback.format_exc()[-2000:],
        }


@router.get("/pipeline/debug")
async def debug_pipeline(db: AsyncSession = Depends(get_db)):
    """Debug-Endpoint: Zeigt warum Matching 0 Matches erzeugt."""
    from sqlalchemy import select, func, text
    from app.models.candidate import Candidate
    from app.models.job import Job
    from app.models.match import Match

    # FINANCE-Jobs mit v2-Profil
    r1 = await db.execute(
        select(func.count(Job.id)).where(
            Job.v2_profile_created_at.isnot(None),
            Job.deleted_at.is_(None),
            Job.hotlist_category == "FINANCE",
        )
    )
    finance_jobs_profiled = r1.scalar() or 0

    # FINANCE-Kandidaten mit v2-Profil
    r2 = await db.execute(
        select(func.count(Candidate.id)).where(
            Candidate.v2_profile_created_at.isnot(None),
            Candidate.deleted_at.is_(None),
            Candidate.hotlist_category == "FINANCE",
        )
    )
    finance_cands_profiled = r2.scalar() or 0

    # Kandidaten mit Seniority-Level
    r3 = await db.execute(
        select(func.count(Candidate.id)).where(
            Candidate.v2_profile_created_at.isnot(None),
            Candidate.v2_seniority_level.isnot(None),
            Candidate.hotlist_category == "FINANCE",
        )
    )
    cands_with_seniority = r3.scalar() or 0

    # Kandidaten mit Embeddings
    r4 = await db.execute(
        select(func.count(Candidate.id)).where(
            Candidate.v2_profile_created_at.isnot(None),
            Candidate.v2_embedding_current.isnot(None),
            Candidate.hotlist_category == "FINANCE",
        )
    )
    cands_with_embedding = r4.scalar() or 0

    # Jobs mit Geo-Koordinaten
    r5 = await db.execute(
        select(func.count(Job.id)).where(
            Job.v2_profile_created_at.isnot(None),
            Job.hotlist_category == "FINANCE",
            Job.location_coords.isnot(None),
        )
    )
    jobs_with_coords = r5.scalar() or 0

    # Jobs mit Seniority-Level
    r6 = await db.execute(
        select(func.count(Job.id)).where(
            Job.v2_profile_created_at.isnot(None),
            Job.hotlist_category == "FINANCE",
            Job.v2_seniority_level.isnot(None),
        )
    )
    jobs_with_seniority = r6.scalar() or 0

    # Stichprobe: Erster FINANCE-Job
    r7 = await db.execute(
        select(Job.id, Job.position, Job.company_name, Job.v2_seniority_level, Job.hotlist_category)
        .where(
            Job.v2_profile_created_at.isnot(None),
            Job.hotlist_category == "FINANCE",
        )
        .limit(3)
    )
    sample_jobs = [
        {"id": str(r.id), "position": r.position, "company": r.company_name,
         "seniority": r.v2_seniority_level, "category": r.hotlist_category}
        for r in r7.all()
    ]

    # Existierende Matches
    r8 = await db.execute(select(func.count(Match.id)))
    total_matches = r8.scalar() or 0

    r9 = await db.execute(
        select(func.count(Match.id)).where(Match.v2_matched_at.isnot(None))
    )
    v2_matches = r9.scalar() or 0

    return {
        "finance_jobs_profiled": finance_jobs_profiled,
        "finance_cands_profiled": finance_cands_profiled,
        "cands_with_seniority": cands_with_seniority,
        "cands_with_embedding": cands_with_embedding,
        "jobs_with_coords": jobs_with_coords,
        "jobs_with_seniority": jobs_with_seniority,
        "sample_jobs": sample_jobs,
        "total_matches_in_db": total_matches,
        "v2_matches_in_db": v2_matches,
    }


# ══════════════════════════════════════════════════════════════════
# Feedback & Learning (Sprint 3)
# ══════════════════════════════════════════════════════════════════

@router.post("/feedback/{match_id}")
async def submit_feedback(
    match_id: UUID,
    outcome: str,  # "good" / "bad" / "neutral"
    note: str = "",
    db: AsyncSession = Depends(get_db),
):
    """Speichert Feedback fuer einen Match und passt Gewichte an.

    3 Stufen des Lernens:
    - 0-20 Feedbacks: Nur speichern (Cold Start)
    - 20-80: Micro-Adjustment (Gewichte verschieben sich ±0.8%)
    - 80+: Korrelations-basierte Optimierung

    Args:
        match_id: UUID des Matches
        outcome: "good" (guter Match), "bad" (schlechter Match), "neutral"
        note: Optionale Notiz
    """
    from app.services.matching_learning_service import MatchingLearningService

    service = MatchingLearningService(db)
    try:
        result = await service.record_feedback(
            match_id=match_id,
            outcome=outcome,
            note=note if note else None,
        )
        return {
            "status": "ok",
            "match_id": str(result.match_id),
            "outcome": result.outcome,
            "weights_adjusted": result.weights_adjusted,
            "adjustments": result.adjustments,
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/feedback/{match_id}/placed")
async def record_placement(
    match_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Markiert einen Match als erfolgreich platziert (staerkstes positives Signal)."""
    from app.services.matching_learning_service import MatchingLearningService

    service = MatchingLearningService(db)
    try:
        result = await service.record_placement(match_id)
        return {
            "status": "ok",
            "match_id": str(result.match_id),
            "outcome": "good",
            "source": "placed",
            "weights_adjusted": result.weights_adjusted,
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/learn/stats")
async def get_learning_stats(db: AsyncSession = Depends(get_db)):
    """Gibt umfassende Lern-Statistiken zurueck.

    Zeigt:
    - Anzahl Feedbacks (good/bad/neutral)
    - Aktuelle Lern-Stufe (cold_start → micro_adjustment → correlation → mature)
    - Welche Scoring-Komponenten am besten gut/schlecht trennen
    - Anzahl gelernter Regeln
    """
    from app.services.matching_learning_service import MatchingLearningService

    service = MatchingLearningService(db)
    stats = await service.get_learning_stats()

    return {
        "total_feedbacks": stats.total_feedbacks,
        "good": stats.good_feedbacks,
        "bad": stats.bad_feedbacks,
        "neutral": stats.neutral_feedbacks,
        "learning_stage": stats.learning_stage,
        "total_rules": stats.total_rules,
        "active_rules": stats.active_rules,
        "total_weight_adjustments": stats.total_weight_adjustments,
        "component_performance": stats.top_performing_components,
    }


@router.get("/learn/stats/extended")
async def get_extended_learning_stats(db: AsyncSession = Depends(get_db)):
    """Erweiterte Lern-Statistiken mit Details.

    Zeigt:
    - Ablehnungsgruende-Verteilung (bad_distance, bad_skills, bad_seniority)
    - Aufschluesselung pro Job-Kategorie (gut/schlecht/neutral)
    - Gewichts-Veraenderungen pro Kategorie und Komponente
    - Komponenten-Trennkraft (welche Scores trennen gut/schlecht)
    - Letzte 20 Feedbacks mit Details
    """
    from app.services.matching_learning_service import MatchingLearningService

    service = MatchingLearningService(db)
    return await service.get_extended_stats()


@router.get("/learn/weights")
async def get_current_weights_detailed(db: AsyncSession = Depends(get_db)):
    """Gibt die aktuellen Gewichte mit Aenderungshistorie zurueck."""
    from app.services.matching_learning_service import MatchingLearningService

    service = MatchingLearningService(db)
    return await service.get_current_weights()


@router.post("/learn/reset-weights")
async def reset_weights(db: AsyncSession = Depends(get_db)):
    """Setzt alle Gewichte auf die Default-Werte zurueck.

    ACHTUNG: Alle gelernten Gewichts-Anpassungen gehen verloren!
    Training-Daten bleiben erhalten.
    """
    from app.services.matching_learning_service import MatchingLearningService

    service = MatchingLearningService(db)
    return await service.reset_weights()


@router.get("/learn/history")
async def get_feedback_history(
    limit: int = 50,
    outcome: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Gibt die letzten Feedbacks zurueck.

    Args:
        limit: Max. Anzahl (default 50)
        outcome: Optional filtern nach "good"/"bad"/"neutral"
    """
    from app.services.matching_learning_service import MatchingLearningService

    service = MatchingLearningService(db)
    history = await service.get_feedback_history(limit=limit, outcome_filter=outcome)
    return {"feedbacks": history, "total": len(history)}


# ═══════════════════════════════════════════════════════
# ADMIN — Reset & Re-Match
# ═══════════════════════════════════════════════════════


@router.post("/admin/reset-matches")
async def reset_all_matches(
    confirm: str = Query(..., pattern="^YES_DELETE_ALL$"),
    db: AsyncSession = Depends(get_db),
):
    """ADMIN: Loescht ALLE v2-Matches und Training-Daten fuer einen sauberen Neustart.

    ACHTUNG: Unwiderruflich! Alle Matches, Feedbacks und Training-Daten werden geloescht.
    Gewichte bleiben erhalten (koennen separat zurueckgesetzt werden).

    Erfordert ?confirm=YES_DELETE_ALL als Sicherheitsabfrage.
    """
    from app.models.match import Match
    from app.models.match_v2_models import MatchV2TrainingData
    from sqlalchemy import delete

    # 1. Training-Daten loeschen
    td_result = await db.execute(delete(MatchV2TrainingData))
    td_count = td_result.rowcount

    # 2. Alle v2-Matches loeschen (nur die mit v2_matched_at)
    m_result = await db.execute(
        delete(Match).where(Match.v2_matched_at.isnot(None))
    )
    m_count = m_result.rowcount

    await db.commit()

    logger.info(f"ADMIN RESET: {m_count} Matches + {td_count} Training-Daten geloescht")

    return {
        "status": "ok",
        "deleted_matches": m_count,
        "deleted_training_data": td_count,
        "message": f"{m_count} Matches und {td_count} Training-Daten geloescht. Jetzt neu matchen mit POST /api/v2/match/batch",
    }


@router.get("/admin/debug-match/{match_id}")
async def debug_match(
    match_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """ADMIN DEBUG: Zeigt Match-Details inkl. distance_km und Breakdown."""
    from app.models.match import Match
    from app.models.job import Job
    from app.models import Candidate
    from sqlalchemy import select

    match = await db.get(Match, match_id)
    if not match:
        raise HTTPException(404, "Match not found")

    job = await db.get(Job, match.job_id) if match.job_id else None
    cand = await db.get(Candidate, match.candidate_id) if match.candidate_id else None

    return {
        "match_id": str(match.id),
        "distance_km": match.distance_km,
        "v2_score": match.v2_score,
        "status": match.status.value if match.status else None,
        "v2_score_breakdown": match.v2_score_breakdown,
        "job": {
            "id": str(job.id) if job else None,
            "location_coords_present": job.location_coords is not None if job else None,
            "city": job.city if job else None,
            "work_location_city": job.work_location_city if job else None,
        } if job else None,
        "candidate": {
            "id": str(cand.id) if cand else None,
            "address_coords_present": cand.address_coords is not None if cand else None,
            "city": cand.city if cand else None,
        } if cand else None,
    }


@router.post("/admin/geocode-test/{job_id}")
async def geocode_test_job(
    job_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """ADMIN DEBUG: Testet Geocoding-Fallback fuer einen einzelnen Job."""
    from app.models.job import Job
    from app.models.company import Company
    from app.services.geocoding_service import GeocodingService

    job = await db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "Job not found")

    geo = GeocodingService(db)
    log = []

    try:
        # 1) Company Check
        if job.company_id:
            company = await db.get(Company, job.company_id)
            log.append(f"Company: {company.name if company else 'NOT FOUND'}, coords: {company.location_coords is not None if company else False}")
        else:
            log.append("Company: keine company_id")

        # 2) Build address variants (same logic as geocode_job)
        full_addr = geo._build_address(street=job.street_address, postal_code=job.postal_code, city=job.city)
        log.append(f"Full: '{full_addr}'")

        fallback_addr = geo._build_address(street=None, postal_code=job.postal_code, city=job.city)
        log.append(f"Fallback (PLZ+Stadt): '{fallback_addr}'")

        # 3) Test each variant with Nominatim
        for label, addr in [("Full", full_addr), ("Fallback", fallback_addr)]:
            if addr:
                result = await geo.geocode(addr)
                log.append(f"Nominatim({label}): {'OK' if result else 'None'} → {addr}")
            else:
                log.append(f"Nominatim({label}): skipped (no address)")

        # 4) Actually run geocode_job() with a FRESH GeocodingService (no cache)
        geo2 = GeocodingService(db)
        try:
            ok = await geo2.geocode_job(job)
            log.append(f"geocode_job(): {ok}")
            if ok:
                await db.commit()
                log.append("COMMITTED")
        finally:
            await geo2.close()

        return {
            "job_id": str(job.id),
            "fields": {
                "city": job.city,
                "work_location_city": job.work_location_city,
                "street_address": job.street_address,
                "postal_code": job.postal_code,
            },
            "location_coords_now": job.location_coords is not None,
            "log": log,
        }
    finally:
        await geo.close()


@router.post("/admin/geocode-sync")
async def geocode_sync(
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    """ADMIN: Synchrones Geocoding — wartet auf Ergebnis statt Background-Task.

    Geocodiert bis zu `limit` Eintraege (Jobs + Kandidaten) und gibt das
    Ergebnis direkt zurueck. Ideal zum Debuggen und fuer schrittweises Geocoding.
    """
    from app.services.geocoding_service import GeocodingService

    geo = GeocodingService(db)
    try:
        # --- Jobs ohne Koordinaten (mit Stadt) ---
        from app.models.job import Job
        from app.models import Candidate
        from sqlalchemy import select, and_, func

        jobs_q = await db.execute(
            select(Job).where(
                Job.location_coords.is_(None),
                Job.deleted_at.is_(None),
                Job.city.isnot(None),
                Job.city != "",
            ).limit(limit)
        )
        jobs = jobs_q.scalars().all()

        jobs_ok = 0
        jobs_skip = 0
        jobs_fail = 0
        for job in jobs:
            try:
                if await geo.geocode_job(job):
                    jobs_ok += 1
                else:
                    jobs_skip += 1
            except Exception as e:
                jobs_fail += 1
                logger.error(f"Geocode Job {job.id}: {e}")

        if jobs:
            await db.commit()

        # --- Kandidaten ohne Koordinaten (mit Stadt) ---
        remaining = limit - len(jobs)
        cands_ok = 0
        cands_skip = 0
        cands_fail = 0

        if remaining > 0:
            cands_q = await db.execute(
                select(Candidate).where(
                    Candidate.address_coords.is_(None),
                    Candidate.hidden == False,
                    Candidate.city.isnot(None),
                    Candidate.city != "",
                ).limit(remaining)
            )
            candidates = cands_q.scalars().all()

            for cand in candidates:
                try:
                    if await geo.geocode_candidate(cand):
                        cands_ok += 1
                    else:
                        cands_skip += 1
                except Exception as e:
                    cands_fail += 1
                    logger.error(f"Geocode Kandidat {cand.id}: {e}")

            if candidates:
                await db.commit()

        # --- Zaehle aktuelle Totals ---
        jobs_total = (await db.execute(
            select(func.count()).select_from(Job).where(Job.deleted_at.is_(None), Job.city.isnot(None))
        )).scalar() or 0
        jobs_with_coords = (await db.execute(
            select(func.count()).select_from(Job).where(Job.location_coords.isnot(None), Job.deleted_at.is_(None))
        )).scalar() or 0
        cands_total = (await db.execute(
            select(func.count()).select_from(Candidate).where(Candidate.hidden == False, Candidate.city.isnot(None))
        )).scalar() or 0
        cands_with_coords = (await db.execute(
            select(func.count()).select_from(Candidate).where(Candidate.address_coords.isnot(None), Candidate.hidden == False)
        )).scalar() or 0

        return {
            "status": "ok",
            "this_run": {
                "jobs": {"ok": jobs_ok, "skip": jobs_skip, "fail": jobs_fail},
                "candidates": {"ok": cands_ok, "skip": cands_skip, "fail": cands_fail},
            },
            "totals": {
                "jobs": f"{jobs_with_coords}/{jobs_total} geocodiert",
                "candidates": f"{cands_with_coords}/{cands_total} geocodiert",
            },
        }
    finally:
        await geo.close()
