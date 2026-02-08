"""Matching Engine v2 — API Routes.

Sprint 1: Profile-Erstellung, Backfill, Stats.
Sprint 2: Matching, Embedding-Generierung, Batch-Matching.
"""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, BackgroundTasks, HTTPException
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


async def _run_backfill(entity_type: str, max_total: int, batch_size: int):
    """Background-Task fuer Backfill."""
    from app.database import async_session_maker

    _backfill_status["running"] = True
    _backfill_status["type"] = entity_type
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
                    )
                elif entity_type == "jobs":
                    result = await service.backfill_jobs(
                        batch_size=batch_size,
                        max_total=max_total,
                        progress_callback=on_progress,
                    )
                elif entity_type == "all":
                    # Erst Kandidaten, dann Jobs
                    result_c = await service.backfill_candidates(
                        batch_size=batch_size,
                        max_total=max_total,
                        progress_callback=on_progress,
                    )
                    result_j = await service.backfill_jobs(
                        batch_size=batch_size,
                        max_total=max_total,
                        progress_callback=on_progress,
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
):
    """Startet Backfill im Background: Alle Kandidaten/Jobs ohne v2-Profil werden profiliert.

    Args:
        entity_type: "candidates", "jobs", oder "all"
        max_total: Maximum (0 = alle)
        batch_size: Batch-Groesse fuer Commits
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

    background_tasks.add_task(_run_backfill, entity_type, max_total, batch_size)

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
                unmatched_only=True,
                progress_callback=on_match_progress,
            )
            results["matching"] = {
                "jobs_matched": match_result.jobs_matched,
                "matches_created": match_result.total_matches_created,
                "duration_ms": round(match_result.total_duration_ms, 1),
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
