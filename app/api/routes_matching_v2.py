"""Matching Engine v2 — API Routes.

Profile-Erstellung, Backfill, Stats.
Matching + Learning Routes kommen in Sprint 2-3.
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
