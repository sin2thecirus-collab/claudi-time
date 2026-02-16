"""Matching Engine v2 — API Routes.

Sprint 1: Profile-Erstellung, Backfill, Stats.
Sprint 2: Matching, Embedding-Generierung, Batch-Matching.
"""

import json
import logging
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.rate_limiter import RateLimitTier, rate_limit
from app.config import settings
from app.database import get_db
from app.models.candidate import Candidate
from app.models.company_contact import CompanyContact
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
    """Background-Task fuer Backfill — per-Entity DB-Sessions (Railway-safe).

    Nutzt pro Kandidat/Job eine eigene DB-Session um Railway idle-in-transaction
    Timeout (30s) zu vermeiden. Semaphore(2) fuer Parallelisierung.
    """
    import asyncio
    from datetime import date, datetime, timezone
    from sqlalchemy import or_
    from app.database import async_session_maker
    from app.models.job import Job

    _backfill_status["running"] = True
    _backfill_status["type"] = f"reprofile_{entity_type}" if force_reprofile else entity_type
    _backfill_status["processed"] = 0
    _backfill_status["total"] = 0
    _backfill_status["cost_usd"] = 0.0
    _backfill_status["result"] = None
    _backfill_status["started_at"] = datetime.now(timezone.utc).isoformat()
    _backfill_status["last_update"] = None
    _backfill_status["errors_list"] = []

    stats = {"profiled": 0, "skipped": 0, "failed": 0, "cost_usd": 0.0}
    semaphore = asyncio.Semaphore(3)

    async def _profile_one_candidate(cid):
        """Profil EINEN Kandidaten mit eigener DB-Session."""
        async with semaphore:
            try:
                async with async_session_maker() as db2:
                    service = ProfileEngineService(db2)
                    try:
                        profile = await service.create_candidate_profile(cid)
                        if profile.success:
                            await db2.commit()
                            stats["profiled"] += 1
                            stats["cost_usd"] += profile.cost_usd
                        else:
                            stats["skipped"] += 1
                    finally:
                        await service.close()
            except Exception as e:
                stats["failed"] += 1
                if len(_backfill_status["errors_list"]) < 20:
                    _backfill_status["errors_list"].append(f"{cid}: {str(e)[:150]}")
                logger.error(f"Profiling Kandidat {cid} fehlgeschlagen: {e}")
            finally:
                _backfill_status["processed"] = stats["profiled"] + stats["skipped"] + stats["failed"]
                _backfill_status["cost_usd"] = round(stats["cost_usd"], 4)
                _backfill_status["last_update"] = datetime.now(timezone.utc).isoformat()

    async def _profile_one_job(jid):
        """Profil EINEN Job mit eigener DB-Session."""
        async with semaphore:
            try:
                async with async_session_maker() as db2:
                    service = ProfileEngineService(db2)
                    try:
                        profile = await service.create_job_profile(jid)
                        if profile.success:
                            await db2.commit()
                            stats["profiled"] += 1
                            stats["cost_usd"] += profile.cost_usd
                        else:
                            stats["skipped"] += 1
                    finally:
                        await service.close()
            except Exception as e:
                stats["failed"] += 1
                if len(_backfill_status["errors_list"]) < 20:
                    _backfill_status["errors_list"].append(f"{jid}: {str(e)[:150]}")
                logger.error(f"Profiling Job {jid} fehlgeschlagen: {e}")
            finally:
                _backfill_status["processed"] = stats["profiled"] + stats["skipped"] + stats["failed"]
                _backfill_status["cost_usd"] = round(stats["cost_usd"], 4)
                _backfill_status["last_update"] = datetime.now(timezone.utc).isoformat()

    try:
        entity_types_to_process = []
        if entity_type == "all":
            entity_types_to_process = ["candidates", "jobs"]
        else:
            entity_types_to_process = [entity_type]

        for current_type in entity_types_to_process:
            # Reset stats for each entity type (for "all" mode)
            if entity_type == "all" and current_type == "jobs":
                stats = {"profiled": 0, "skipped": 0, "failed": 0, "cost_usd": 0.0}
                _backfill_status["processed"] = 0

            # 1. IDs laden (kurze Session)
            async with async_session_maker() as db:
                if current_type == "candidates":
                    from app.models.candidate import Candidate
                    from sqlalchemy import or_
                    max_age = 58
                    cutoff_date = date(date.today().year - max_age, date.today().month, date.today().day)
                    conditions = [
                        Candidate.deleted_at.is_(None),
                        Candidate.hidden == False,
                        Candidate.hotlist_category == "FINANCE",
                        or_(
                            Candidate.birth_date.is_(None),
                            Candidate.birth_date >= cutoff_date,
                        ),
                    ]
                    if not force_reprofile:
                        conditions.append(Candidate.v2_profile_created_at.is_(None))
                    result = await db.execute(
                        select(Candidate.id).where(*conditions).order_by(Candidate.created_at.asc())
                    )
                else:  # jobs
                    conditions = [
                        Job.deleted_at.is_(None),
                        Job.hotlist_category == "FINANCE",
                    ]
                    if not force_reprofile:
                        conditions.append(Job.v2_profile_created_at.is_(None))
                    result = await db.execute(
                        select(Job.id).where(*conditions).order_by(Job.created_at.asc())
                    )

                entity_ids = [row[0] for row in result.fetchall()]
                if max_total > 0:
                    entity_ids = entity_ids[:max_total]

            total = len(entity_ids)
            _backfill_status["total"] = total
            _backfill_status["type"] = f"reprofile_{current_type}" if force_reprofile else current_type
            logger.info(f"Backfill {current_type}: {total} Entitäten zu profilen (force={force_reprofile})")

            if total == 0:
                continue

            # 2. Chunks von 10 parallel, Semaphore(3), 1s Pause zwischen Chunks
            profile_fn = _profile_one_candidate if current_type == "candidates" else _profile_one_job
            for i in range(0, total, 10):
                chunk = entity_ids[i:i + 10]
                await asyncio.gather(*[profile_fn(eid) for eid in chunk])
                await asyncio.sleep(1)

            logger.info(
                f"Backfill {current_type} fertig: {stats['profiled']} profiled, "
                f"{stats['skipped']} skipped, {stats['failed']} failed, "
                f"${stats['cost_usd']:.4f}"
            )

        _backfill_status["result"] = {
            "profiled": stats["profiled"],
            "skipped": stats["skipped"],
            "failed": stats["failed"],
            "cost_usd": round(stats["cost_usd"], 4),
            "errors": _backfill_status["errors_list"][:10],
        }
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
        "started_at": _backfill_status.get("started_at"),
        "last_update": _backfill_status.get("last_update"),
        "errors_count": len(_backfill_status.get("errors_list", [])),
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


@router.get("/debug/embedding-readiness")
async def debug_embedding_readiness(db: AsyncSession = Depends(get_db)):
    """Debug: Prüft warum Embedding-Generierung 0 Kandidaten findet."""
    from sqlalchemy import text
    try:
        # Verwende raw SQL um SQLAlchemy ORM zu umgehen
        profiled = (await db.execute(text(
            "SELECT COUNT(*) FROM candidates WHERE hotlist_category='FINANCE' "
            "AND deleted_at IS NULL AND v2_profile_created_at IS NOT NULL"
        ))).scalar() or 0

        no_emb = (await db.execute(text(
            "SELECT COUNT(*) FROM candidates WHERE hotlist_category='FINANCE' "
            "AND deleted_at IS NULL AND v2_profile_created_at IS NOT NULL "
            "AND v2_embedding_current IS NULL"
        ))).scalar() or 0

        # Sample: Erster Kandidat und seine v2_embedding_current Wert
        sample = (await db.execute(text(
            "SELECT id, full_name, v2_profile_created_at IS NOT NULL as has_profile, "
            "v2_embedding_current IS NULL as emb_is_null, "
            "pg_column_size(v2_embedding_current) as emb_bytes "
            "FROM candidates WHERE hotlist_category='FINANCE' AND deleted_at IS NULL "
            "AND v2_profile_created_at IS NOT NULL LIMIT 3"
        ))).all()

        samples = [{"id": str(s[0]), "name": s[1], "has_profile": s[2],
                     "emb_is_null": s[3], "emb_bytes": s[4]} for s in sample]

        return {
            "profiled_finance": profiled,
            "ready_for_embedding_null": no_emb,
            "samples": samples,
        }
    except Exception as e:
        return {"error": str(e), "type": type(e).__name__}


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


@router.post("/embeddings/reset")
async def reset_embeddings(
    entity_type: str = "all",
    db: AsyncSession = Depends(get_db),
):
    """Setzt alle Embeddings auf NULL zurueck, damit sie neu generiert werden.

    Noetig nach Re-Profiling (force_reprofile=true), weil die Embedding-Generierung
    nur Kandidaten/Jobs OHNE Embedding verarbeitet.
    Verwendet raw SQL um JSONB-NULL-Probleme zu umgehen.
    """
    from sqlalchemy import text

    results = {}

    if entity_type in ("candidates", "all"):
        cand_result = await db.execute(text(
            "UPDATE candidates SET v2_embedding_current = NULL "
            "WHERE hotlist_category = 'FINANCE' AND v2_embedding_current IS NOT NULL"
        ))
        results["candidates_reset"] = cand_result.rowcount

    if entity_type in ("jobs", "all"):
        job_result = await db.execute(text(
            "UPDATE jobs SET v2_embedding = NULL "
            "WHERE hotlist_category = 'FINANCE' AND v2_embedding IS NOT NULL"
        ))
        results["jobs_reset"] = job_result.rowcount

    await db.commit()

    # Verifiziere sofort
    verify_c = (await db.execute(text(
        "SELECT COUNT(*) FROM candidates WHERE hotlist_category='FINANCE' "
        "AND v2_profile_created_at IS NOT NULL AND v2_embedding_current IS NULL"
    ))).scalar() or 0
    results["verify_candidates_ready"] = verify_c

    return results


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
# Admin Debug: v2 Skills Inspector
# ══════════════════════════════════════════════════════════════════


@router.get("/debug/v2-skills/{entity_type}/{entity_id}")
async def debug_v2_skills(
    entity_type: str,
    entity_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """TEMP DEBUG: Zeigt v2_structured_skills/v2_required_skills fuer ein Entity."""
    from sqlalchemy import select
    from app.models.candidate import Candidate
    from app.models.job import Job

    if entity_type == "candidate":
        r = await db.execute(
            select(
                Candidate.id,
                Candidate.first_name,
                Candidate.last_name,
                Candidate.current_position,
                Candidate.v2_seniority_level,
                Candidate.v2_structured_skills,
                Candidate.v2_certifications,
                Candidate.v2_industries,
                Candidate.v2_current_role_summary,
                Candidate.v2_years_experience,
                Candidate.v2_profile_created_at,
            ).where(Candidate.id == entity_id)
        )
        row = r.first()
        if not row:
            return {"error": "Candidate not found"}
        return {
            "entity": "candidate",
            "id": str(row.id),
            "name": f"{row.first_name} {row.last_name}",
            "current_position": row.current_position,
            "v2_seniority_level": row.v2_seniority_level,
            "v2_structured_skills": row.v2_structured_skills,
            "v2_certifications": row.v2_certifications,
            "v2_industries": row.v2_industries,
            "v2_current_role_summary": row.v2_current_role_summary,
            "v2_years_experience": row.v2_years_experience,
            "v2_profile_created_at": str(row.v2_profile_created_at) if row.v2_profile_created_at else None,
        }
    elif entity_type == "job":
        r = await db.execute(
            select(
                Job.id,
                Job.position,
                Job.company_name,
                Job.hotlist_job_title,
                Job.v2_seniority_level,
                Job.v2_required_skills,
                Job.v2_role_summary,
                Job.v2_profile_created_at,
            ).where(Job.id == entity_id)
        )
        row = r.first()
        if not row:
            return {"error": "Job not found"}
        return {
            "entity": "job",
            "id": str(row.id),
            "position": row.position,
            "company": row.company_name,
            "hotlist_job_title": row.hotlist_job_title,
            "v2_seniority_level": row.v2_seniority_level,
            "v2_required_skills": row.v2_required_skills,
            "v2_role_summary": row.v2_role_summary,
            "v2_profile_created_at": str(row.v2_profile_created_at) if row.v2_profile_created_at else None,
        }
    return {"error": "entity_type must be 'candidate' or 'job'"}


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


@router.get("/admin/geocoding-status")
async def admin_geocoding_coverage(
    db: AsyncSession = Depends(get_db),
):
    """Status-Endpoint: Zeigt Geocoding-Abdeckung und listet Jobs/Kandidaten ohne Koordinaten.

    Nuetzlich um zu sehen welche Eintraege keine geocodierbare Adresse hatten
    und welche den Unternehmens-Standort als Fallback nutzen.
    """
    from app.models.job import Job
    from app.models import Candidate
    from app.models.company import Company
    from sqlalchemy import select, func, and_, or_

    # --- Jobs ---
    jobs_total = (await db.execute(
        select(func.count()).select_from(Job).where(Job.deleted_at.is_(None))
    )).scalar() or 0

    jobs_with_coords = (await db.execute(
        select(func.count()).select_from(Job).where(
            Job.location_coords.isnot(None), Job.deleted_at.is_(None)
        )
    )).scalar() or 0

    # Jobs OHNE Koordinaten aber MIT Company die Koordinaten hat (Fallback moeglich)
    jobs_company_fallback = (await db.execute(
        select(func.count()).select_from(Job)
        .join(Company, Job.company_id == Company.id)
        .where(
            Job.location_coords.is_(None),
            Job.deleted_at.is_(None),
            Company.location_coords.isnot(None),
        )
    )).scalar() or 0

    # Jobs komplett OHNE Koordinaten und OHNE Company-Fallback
    jobs_no_coords_q = await db.execute(
        select(
            Job.id,
            Job.position,
            Job.company_name,
            Job.city,
            Job.work_location_city,
            Job.street_address,
            Job.postal_code,
        )
        .outerjoin(Company, Job.company_id == Company.id)
        .where(
            Job.location_coords.is_(None),
            Job.deleted_at.is_(None),
            or_(
                Job.company_id.is_(None),
                Company.location_coords.is_(None),
            ),
        )
        .limit(50)
    )
    jobs_without = [
        {
            "id": str(r[0]),
            "position": r[1],
            "company": r[2],
            "city": r[3],
            "work_location_city": r[4],
            "street": r[5],
            "plz": r[6],
        }
        for r in jobs_no_coords_q.all()
    ]

    # --- Kandidaten ---
    cands_total = (await db.execute(
        select(func.count()).select_from(Candidate).where(Candidate.hidden == False)
    )).scalar() or 0

    cands_with_coords = (await db.execute(
        select(func.count()).select_from(Candidate).where(
            Candidate.address_coords.isnot(None), Candidate.hidden == False
        )
    )).scalar() or 0

    cands_no_city = (await db.execute(
        select(func.count()).select_from(Candidate).where(
            Candidate.address_coords.is_(None),
            Candidate.hidden == False,
            or_(Candidate.city.is_(None), Candidate.city == ""),
        )
    )).scalar() or 0

    return {
        "jobs": {
            "total": jobs_total,
            "with_coords": jobs_with_coords,
            "company_fallback_available": jobs_company_fallback,
            "without_coords_or_fallback": len(jobs_without),
            "coverage_percent": round(jobs_with_coords / jobs_total * 100, 1) if jobs_total else 0,
            "effective_coverage_percent": round(
                (jobs_with_coords + jobs_company_fallback) / jobs_total * 100, 1
            ) if jobs_total else 0,
        },
        "candidates": {
            "total": cands_total,
            "with_coords": cands_with_coords,
            "without_coords": cands_total - cands_with_coords,
            "no_city_at_all": cands_no_city,
            "coverage_percent": round(cands_with_coords / cands_total * 100, 1) if cands_total else 0,
        },
        "jobs_without_coords_or_fallback": jobs_without,
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


@router.post("/admin/geocode-debug/{candidate_id}")
async def geocode_debug_candidate(
    candidate_id: str,
    db: AsyncSession = Depends(get_db),
):
    """DEBUG: Zeigt exakt welche Geocoding-Varianten fuer einen Kandidaten versucht werden."""
    import re
    from sqlalchemy import select
    from app.models import Candidate
    from app.services.geocoding_service import GeocodingService

    result = await db.execute(
        select(Candidate).where(Candidate.id == candidate_id)
    )
    cand = result.scalar_one_or_none()
    if not cand:
        return {"error": "Kandidat nicht gefunden"}

    geo = GeocodingService(db)
    try:
        # Alle Varianten aufbauen (gleiche Logik wie geocode_candidate)
        # Reihenfolge: 1) Voll, 2) Nur PLZ, 3) PLZ+Stadt, 4) Stadt bereinigt, 5) Nur Stadt
        address_variants = []
        full_addr = geo._build_address(street=cand.street_address, postal_code=cand.postal_code, city=cand.city)
        if full_addr:
            address_variants.append(full_addr)
        if cand.postal_code:
            plz_only = f"{cand.postal_code}, Deutschland"
            if plz_only not in address_variants:
                address_variants.append(plz_only)
        if cand.street_address:
            fallback = geo._build_address(street=None, postal_code=cand.postal_code, city=cand.city)
            if fallback and fallback not in address_variants:
                address_variants.append(fallback)
        if cand.city:
            clean_city = re.sub(r'\s*(?:OT|bei|Ortsteil)\s+\S+.*', '', cand.city).strip()
            if clean_city != cand.city:
                clean_fb = geo._build_address(street=None, postal_code=cand.postal_code, city=clean_city)
                if clean_fb and clean_fb not in address_variants:
                    address_variants.append(clean_fb)
        if cand.city:
            city_only = geo._build_address(street=None, postal_code=None, city=cand.city)
            if city_only and city_only not in address_variants:
                address_variants.append(city_only)

        # Jede Variante testen
        results = []
        first_success = None
        for addr in address_variants:
            geo_result = await geo.geocode(addr)
            results.append({
                "address": addr,
                "found": geo_result is not None,
                "lat": geo_result.latitude if geo_result else None,
                "lon": geo_result.longitude if geo_result else None,
                "display": geo_result.display_name if geo_result else None,
            })
            if geo_result and not first_success:
                first_success = geo_result

        # Auch direkt geocode_candidate() aufrufen und Ergebnis speichern
        applied = False
        if first_success and cand.address_coords is None:
            from sqlalchemy import func as sa_func
            cand.address_coords = sa_func.ST_SetSRID(
                sa_func.ST_MakePoint(first_success.longitude, first_success.latitude),
                4326,
            )
            await db.commit()
            applied = True

        # Auch geocode_candidate testen (um zu sehen ob es bei der regulaeren Funktion klappt)
        geo2 = GeocodingService(db)
        try:
            gc_result = await geo2.geocode_candidate(cand)
        except Exception as e:
            gc_result = f"ERROR: {e}"
        finally:
            await geo2.close()

        return {
            "candidate_id": str(cand.id),
            "name": f"{cand.first_name} {cand.last_name}",
            "street": cand.street_address,
            "postal_code": cand.postal_code,
            "city": cand.city,
            "had_coordinates": cand.address_coords is not None,
            "coords_applied": applied,
            "geocode_candidate_result": gc_result,
            "variants_tried": len(address_variants),
            "results": results,
        }
    finally:
        await geo.close()


# ══════════════════════════════════════════════════════════════════
# Profiling Status & Sync
# ══════════════════════════════════════════════════════════════════


@router.get("/admin/profiling-status")
async def admin_profiling_status(
    db: AsyncSession = Depends(get_db),
):
    """Status-Endpoint: Zeigt Profiling-Abdeckung fuer Jobs und Kandidaten."""
    from app.models.job import Job
    from app.models import Candidate
    from sqlalchemy import select, func

    # --- Jobs ---
    jobs_total = (await db.execute(
        select(func.count()).select_from(Job).where(Job.deleted_at.is_(None))
    )).scalar() or 0

    jobs_with_profile = (await db.execute(
        select(func.count()).select_from(Job).where(
            Job.v2_profile_created_at.isnot(None),
            Job.deleted_at.is_(None),
        )
    )).scalar() or 0

    jobs_finance_total = (await db.execute(
        select(func.count()).select_from(Job).where(
            Job.hotlist_category == "FINANCE",
            Job.deleted_at.is_(None),
        )
    )).scalar() or 0

    jobs_finance_with_profile = (await db.execute(
        select(func.count()).select_from(Job).where(
            Job.hotlist_category == "FINANCE",
            Job.v2_profile_created_at.isnot(None),
            Job.deleted_at.is_(None),
        )
    )).scalar() or 0

    jobs_finance_missing = (await db.execute(
        select(func.count()).select_from(Job).where(
            Job.hotlist_category == "FINANCE",
            Job.v2_profile_created_at.is_(None),
            Job.deleted_at.is_(None),
        )
    )).scalar() or 0

    # Top 50 FINANCE-Jobs ohne Profil (fuer Debugging)
    missing_jobs_q = await db.execute(
        select(
            Job.id, Job.position, Job.company_name, Job.city,
            Job.hotlist_category, Job.created_at,
        )
        .where(
            Job.hotlist_category == "FINANCE",
            Job.v2_profile_created_at.is_(None),
            Job.deleted_at.is_(None),
        )
        .order_by(Job.created_at.desc())
        .limit(50)
    )
    missing_jobs = [
        {
            "id": str(r[0]),
            "position": r[1],
            "company": r[2],
            "city": r[3],
            "category": r[4],
            "created_at": r[5].isoformat() if r[5] else None,
        }
        for r in missing_jobs_q.all()
    ]

    # --- Kandidaten (gleiche Filter wie backfill_candidates: hidden=False + deleted_at=NULL) ---
    cands_total = (await db.execute(
        select(func.count()).select_from(Candidate).where(
            Candidate.hidden == False,
            Candidate.deleted_at.is_(None),
        )
    )).scalar() or 0

    cands_with_profile = (await db.execute(
        select(func.count()).select_from(Candidate).where(
            Candidate.v2_profile_created_at.isnot(None),
            Candidate.hidden == False,
            Candidate.deleted_at.is_(None),
        )
    )).scalar() or 0

    cands_finance_total = (await db.execute(
        select(func.count()).select_from(Candidate).where(
            Candidate.hotlist_category == "FINANCE",
            Candidate.hidden == False,
            Candidate.deleted_at.is_(None),
        )
    )).scalar() or 0

    cands_finance_with_profile = (await db.execute(
        select(func.count()).select_from(Candidate).where(
            Candidate.hotlist_category == "FINANCE",
            Candidate.v2_profile_created_at.isnot(None),
            Candidate.hidden == False,
            Candidate.deleted_at.is_(None),
        )
    )).scalar() or 0

    return {
        "jobs": {
            "total": jobs_total,
            "with_profile": jobs_with_profile,
            "coverage_percent": round(jobs_with_profile / jobs_total * 100, 1) if jobs_total else 0,
            "finance": {
                "total": jobs_finance_total,
                "with_profile": jobs_finance_with_profile,
                "missing_profile": jobs_finance_missing,
                "coverage_percent": round(
                    jobs_finance_with_profile / jobs_finance_total * 100, 1
                ) if jobs_finance_total else 0,
            },
        },
        "candidates": {
            "total": cands_total,
            "with_profile": cands_with_profile,
            "coverage_percent": round(cands_with_profile / cands_total * 100, 1) if cands_total else 0,
            "finance": {
                "total": cands_finance_total,
                "with_profile": cands_finance_with_profile,
                "missing_profile": cands_finance_total - cands_finance_with_profile,
                "coverage_percent": round(
                    cands_finance_with_profile / cands_finance_total * 100, 1
                ) if cands_finance_total else 0,
            },
        },
        "finance_jobs_missing_profile": missing_jobs,
    }


@router.post("/admin/profile-sync")
async def admin_profile_sync(
    limit: int = Query(10, ge=1, le=100),
    entity_type: str = Query("jobs"),
    db: AsyncSession = Depends(get_db),
):
    """ADMIN: Synchrones Profiling — profilt FINANCE-Jobs/-Kandidaten ohne Profil.

    Aehnlich wie geocode-sync: wartet auf Ergebnis statt Background-Task.
    entity_type: 'jobs', 'candidates' oder 'all'
    """
    service = ProfileEngineService(db)

    jobs_result_data = {"profiled": 0, "skipped": 0, "failed": 0, "cost_usd": 0.0}
    cands_result_data = {"profiled": 0, "skipped": 0, "failed": 0, "cost_usd": 0.0}

    # --- Jobs profilen ---
    if entity_type in ("jobs", "all"):
        job_result = await service.backfill_jobs(
            batch_size=limit,
            max_total=limit,
        )
        jobs_result_data = {
            "profiled": job_result.profiled,
            "skipped": job_result.skipped,
            "failed": job_result.failed,
            "cost_usd": round(job_result.total_cost_usd, 6),
        }

    # --- Kandidaten profilen ---
    remaining = limit - jobs_result_data["profiled"] if entity_type == "all" else limit
    if entity_type in ("candidates", "all") and remaining > 0:
        cand_result = await service.backfill_candidates(
            batch_size=remaining,
            max_total=remaining,
        )
        cands_result_data = {
            "profiled": cand_result.profiled,
            "skipped": cand_result.skipped,
            "failed": cand_result.failed,
            "cost_usd": round(cand_result.total_cost_usd, 6),
        }

    return {
        "status": "ok",
        "jobs": jobs_result_data,
        "candidates": cands_result_data,
        "total_cost_usd": round(
            jobs_result_data["cost_usd"] + cands_result_data["cost_usd"], 6
        ),
    }


# ══════════════════════════════════════════════════════════════════
# Admin: Bulk Gender-Klassifizierung per GPT-4o-mini
# ══════════════════════════════════════════════════════════════════

GENDER_SYSTEM_PROMPT = """Du bist ein Namensexperte. Bestimme fuer jeden deutschen Vornamen das Geschlecht.

Regeln:
- "Herr" fuer maennliche Vornamen (Thomas, Max, Stefan, Andreas, ...)
- "Frau" fuer weibliche Vornamen (Anna, Petra, Sabine, Julia, ...)
- null fuer unklare/neutrale Namen (Kim, Robin, Sascha, Andrea, ...)
- Bei zusammengesetzten Namen (Hans-Peter) den ersten Teil nehmen
- Deutsche und internationale Namen beruecksichtigen

Antworte NUR als JSON-Array mit Objekten: [{"name": "...", "gender": "Herr"|"Frau"|null}]
Keine Erklaerungen, kein Markdown — nur valides JSON."""


@router.post("/admin/gender-sync")
@rate_limit(RateLimitTier.ADMIN)
async def admin_gender_sync(
    request: Request,
    db: AsyncSession = Depends(get_db),
    batch_size: int = Query(default=50, ge=10, le=200),
    max_total: int = Query(default=5000, ge=1, le=25000),
):
    """Bulk-Klassifizierung: GPT-4o-mini bestimmt Herr/Frau aus Vornamen.

    Verarbeitet alle Kandidaten ohne gender-Feld in Batches.
    ~$0.001 pro 50 Namen (~$0.40 fuer 20.000 Kandidaten).
    """
    # Kandidaten ohne Gender laden
    result = await db.execute(
        select(Candidate.id, Candidate.first_name)
        .where(Candidate.gender.is_(None))
        .where(Candidate.first_name.isnot(None))
        .where(Candidate.first_name != "")
        .where(Candidate.deleted_at.is_(None))
        .limit(max_total)
    )
    candidates = result.all()

    if not candidates:
        return {"status": "ok", "message": "Keine Kandidaten ohne Anrede gefunden", "updated": 0}

    total_updated = 0
    total_skipped = 0
    total_failed = 0
    errors = []

    # In Batches verarbeiten
    for i in range(0, len(candidates), batch_size):
        batch = candidates[i : i + batch_size]
        names_list = [{"name": c.first_name.strip()} for c in batch]

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {settings.openai_api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "gpt-4o-mini",
                        "temperature": 0.0,
                        "messages": [
                            {"role": "system", "content": GENDER_SYSTEM_PROMPT},
                            {"role": "user", "content": json.dumps(names_list, ensure_ascii=False)},
                        ],
                    },
                )
                resp.raise_for_status()
                data = resp.json()

            content = data["choices"][0]["message"]["content"].strip()
            # JSON parsen (manchmal in ```json ... ``` gewrappt)
            if content.startswith("```"):
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
            gpt_results = json.loads(content)

            # Name→Gender Mapping erstellen
            gender_map = {}
            for item in gpt_results:
                name = item.get("name", "").strip()
                gender = item.get("gender")
                if gender in ("Herr", "Frau"):
                    gender_map[name.lower()] = gender

            # Kandidaten updaten
            for cand_id, first_name in batch:
                mapped_gender = gender_map.get(first_name.strip().lower())
                if mapped_gender:
                    await db.execute(
                        update(Candidate)
                        .where(Candidate.id == cand_id)
                        .values(gender=mapped_gender)
                    )
                    total_updated += 1
                else:
                    total_skipped += 1

            await db.commit()

        except Exception as e:
            total_failed += len(batch)
            errors.append(f"Batch {i // batch_size + 1}: {str(e)[:200]}")
            logger.error(f"Gender-Sync Batch {i // batch_size + 1} fehlgeschlagen: {e}")
            await db.rollback()

    return {
        "status": "ok",
        "total_candidates": len(candidates),
        "updated": total_updated,
        "skipped": total_skipped,
        "failed": total_failed,
        "errors": errors[:5] if errors else [],
    }


@router.post("/admin/gender-sync-contacts")
@rate_limit(RateLimitTier.ADMIN)
async def admin_gender_sync_contacts(
    request: Request,
    db: AsyncSession = Depends(get_db),
    batch_size: int = Query(default=50, ge=10, le=200),
    max_total: int = Query(default=5000, ge=1, le=25000),
):
    """Bulk-Klassifizierung fuer CompanyContacts: GPT-4o-mini bestimmt Herr/Frau aus Vornamen.

    Verarbeitet alle Kontakte ohne salutation-Feld in Batches.
    """
    result = await db.execute(
        select(CompanyContact.id, CompanyContact.first_name)
        .where(CompanyContact.salutation.is_(None))
        .where(CompanyContact.first_name.isnot(None))
        .where(CompanyContact.first_name != "")
        .limit(max_total)
    )
    contacts = result.all()

    if not contacts:
        return {"status": "ok", "message": "Keine Kontakte ohne Anrede gefunden", "updated": 0}

    total_updated = 0
    total_skipped = 0
    total_failed = 0
    errors = []

    for i in range(0, len(contacts), batch_size):
        batch = contacts[i : i + batch_size]
        names_list = [{"name": c.first_name.strip()} for c in batch]

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {settings.openai_api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "gpt-4o-mini",
                        "temperature": 0.0,
                        "messages": [
                            {"role": "system", "content": GENDER_SYSTEM_PROMPT},
                            {"role": "user", "content": json.dumps(names_list, ensure_ascii=False)},
                        ],
                    },
                )
                resp.raise_for_status()
                data = resp.json()

            content = data["choices"][0]["message"]["content"].strip()
            if content.startswith("```"):
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
            gpt_results = json.loads(content)

            gender_map = {}
            for item in gpt_results:
                name = item.get("name", "").strip()
                gender = item.get("gender")
                if gender in ("Herr", "Frau"):
                    gender_map[name.lower()] = gender

            for contact_id, first_name in batch:
                mapped_gender = gender_map.get(first_name.strip().lower())
                if mapped_gender:
                    await db.execute(
                        update(CompanyContact)
                        .where(CompanyContact.id == contact_id)
                        .values(salutation=mapped_gender)
                    )
                    total_updated += 1
                else:
                    total_skipped += 1

            await db.commit()

        except Exception as e:
            total_failed += len(batch)
            errors.append(f"Batch {i // batch_size + 1}: {str(e)[:200]}")
            logger.error(f"Contact Gender-Sync Batch {i // batch_size + 1} fehlgeschlagen: {e}")
            await db.rollback()

    return {
        "status": "ok",
        "total_contacts": len(contacts),
        "updated": total_updated,
        "skipped": total_skipped,
        "failed": total_failed,
        "errors": errors[:5] if errors else [],
    }


# ══════════════════════════════════════════════════════════════════
# DRIVE TIME BACKFILL — Phase 10 Optimierung
# ══════════════════════════════════════════════════════════════════

_drive_time_backfill_status = {
    "running": False,
    "processed": 0,
    "total": 0,
    "skipped": 0,
    "errors": 0,
    "started_at": None,
    "finished_at": None,
}


@router.post("/drive-times/backfill")
@rate_limit(RateLimitTier.ADMIN)
async def drive_time_backfill(
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    min_score: float | None = Query(None, description="Minimum Score für Fahrzeit-Berechnung (Default: aus Einstellungen)"),
    force: bool = Query(False, description="Auch Matches mit bestehender Fahrzeit neu berechnen"),
):
    """Backfill: Google Maps Fahrzeit für bestehende Matches mit Score >= min_score.

    Läuft im Hintergrund. Status abfragbar über GET /drive-times/backfill/status.
    Wenn min_score nicht angegeben, wird der Wert aus den System-Einstellungen gelesen.
    """
    from app.models.match import Match
    from app.models.job import Job
    from app.api.routes_settings import get_drive_time_threshold

    # Default aus DB lesen wenn nicht explizit angegeben
    if min_score is None:
        min_score = float(await get_drive_time_threshold(db))

    if _drive_time_backfill_status["running"]:
        return JSONResponse(
            status_code=409,
            content={"error": "Backfill läuft bereits", "status": _drive_time_backfill_status},
        )

    # Zähle betroffene Matches
    eff = func.coalesce(Match.v2_score, Match.ai_score * 100, 0)
    count_query = (
        select(func.count(Match.id))
        .where(eff >= min_score)
        .where(Match.candidate_id.isnot(None))
    )
    if not force:
        count_query = count_query.where(Match.drive_time_car_min.is_(None))

    total = (await db.execute(count_query)).scalar() or 0

    if total == 0:
        return {"status": "nothing_to_do", "message": "Keine Matches ohne Fahrzeit mit Score >= {min_score}"}

    _drive_time_backfill_status.update({
        "running": True,
        "processed": 0,
        "total": total,
        "skipped": 0,
        "errors": 0,
        "started_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "finished_at": None,
    })

    background_tasks.add_task(
        _run_drive_time_backfill, min_score=min_score, force=force
    )

    return {
        "status": "started",
        "total_matches": total,
        "min_score": min_score,
        "force": force,
        "message": f"Backfill gestartet für {total} Matches",
    }


async def _run_drive_time_backfill(min_score: float = 70.0, force: bool = False):
    """Hintergrund-Task: Berechnet Fahrzeit für Matches ohne drive_time.

    WICHTIG: Eigene DB-Session pro Job-Batch wegen Railway 30s idle-in-transaction Timeout.
    Während Google Maps API-Calls laufen darf KEINE DB-Session offen sein!
    """
    import asyncio
    from datetime import datetime, timezone
    from app.database import async_session_maker as async_session_factory
    from app.models.match import Match
    from app.models.job import Job
    from app.models.candidate import Candidate
    from app.services.distance_matrix_service import distance_matrix_service
    from sqlalchemy import func as sa_func

    status = _drive_time_backfill_status

    logger.info(f"Drive Time Backfill gestartet: has_api_key={distance_matrix_service.has_api_key}")

    if not distance_matrix_service.has_api_key:
        status.update({"running": False, "finished_at": datetime.now(timezone.utc).isoformat()})
        logger.error("Drive Time Backfill: Kein Google Maps API Key konfiguriert")
        return

    try:
        # ── Schritt 1: Matches laden (eigene Session, sofort schließen) ──
        logger.info("Drive Time Backfill: Lade Matches aus DB...")
        matches_by_job: dict[str, list[dict]] = {}
        async with async_session_factory() as db:
            eff = func.coalesce(Match.v2_score, Match.ai_score * 100, 0)
            query = (
                select(
                    Match.id,
                    Match.job_id,
                    Match.candidate_id,
                )
                .where(eff >= min_score)
                .where(Match.candidate_id.isnot(None))
            )
            if not force:
                query = query.where(Match.drive_time_car_min.is_(None))

            query = query.order_by(Match.job_id)
            result = await db.execute(query)
            matches = result.all()

            if not matches:
                status.update({"running": False, "finished_at": datetime.now(timezone.utc).isoformat()})
                return

            status["total"] = len(matches)

            # Gruppiere nach Job — als reine Dicts (keine ORM-Objekte!)
            for m in matches:
                jid = str(m.job_id)
                if jid not in matches_by_job:
                    matches_by_job[jid] = []
                matches_by_job[jid].append({
                    "match_id": m.id,
                    "job_id": m.job_id,
                    "candidate_id": m.candidate_id,
                })
        # DB-Session hier geschlossen!

        logger.info(f"Drive Time Backfill: {len(matches)} Matches in {len(matches_by_job)} Jobs geladen")

        # ── Schritt 2: Pro Job eigene Session → API-Call → eigene Session ──
        job_counter = 0
        for job_id_str, job_matches in matches_by_job.items():
            job_counter += 1
            job_id = job_matches[0]["job_id"]
            try:
                # 2a: Job- und Kandidaten-Koordinaten laden (eigene Session)
                job_lat = job_lng = job_plz = None
                cands_with_coords = []

                async with async_session_factory() as db2:
                    # Job-Koordinaten via Column-Reference + ST_GeomFromWKB
                    coord_result = await db2.execute(
                        select(
                            sa_func.ST_Y(sa_func.ST_GeomFromWKB(Job.location_coords)).label("lat"),
                            sa_func.ST_X(sa_func.ST_GeomFromWKB(Job.location_coords)).label("lng"),
                            Job.postal_code,
                        )
                        .where(Job.id == job_id)
                        .where(Job.location_coords.isnot(None))
                    )
                    coord_row = coord_result.first()
                    if not coord_row or not coord_row.lat or not coord_row.lng:
                        status["skipped"] += len(job_matches)
                        status["processed"] += len(job_matches)
                        continue

                    job_lat, job_lng, job_plz = coord_row.lat, coord_row.lng, coord_row.postal_code

                    # Kandidaten-Koordinaten laden
                    cand_ids = [m["candidate_id"] for m in job_matches]
                    cand_result = await db2.execute(
                        select(
                            Candidate.id,
                            Candidate.postal_code,
                            sa_func.ST_Y(sa_func.ST_GeomFromWKB(Candidate.address_coords)).label("lat"),
                            sa_func.ST_X(sa_func.ST_GeomFromWKB(Candidate.address_coords)).label("lng"),
                        )
                        .where(Candidate.id.in_(cand_ids))
                        .where(Candidate.address_coords.isnot(None))
                    )
                    cand_rows = cand_result.all()

                    cands_with_coords = [
                        {
                            "candidate_id": str(cr.id),
                            "lat": cr.lat,
                            "lng": cr.lng,
                            "plz": cr.postal_code,
                        }
                        for cr in cand_rows
                        if cr.lat is not None and cr.lng is not None
                    ]
                # DB-Session geschlossen BEVOR Google Maps API-Call!

                if not cands_with_coords:
                    status["skipped"] += len(job_matches)
                    status["processed"] += len(job_matches)
                    continue

                # 2b: Google Maps API aufrufen (KEINE DB-Session offen!)
                logger.info(f"Drive Time Backfill Job {job_counter}/{len(matches_by_job)}: "
                           f"{len(cands_with_coords)} Kandidaten, calling Google Maps...")
                drive_times = await distance_matrix_service.batch_drive_times(
                    job_lat=job_lat,
                    job_lng=job_lng,
                    job_plz=job_plz,
                    candidates=cands_with_coords,
                )

                # 2c: Ergebnisse in DB schreiben (eigene Session)
                async with async_session_factory() as db3:
                    for m in job_matches:
                        dt = drive_times.get(str(m["candidate_id"]))
                        if dt:
                            match_obj = await db3.get(Match, m["match_id"])
                            if match_obj:
                                match_obj.drive_time_car_min = dt.car_min
                                match_obj.drive_time_transit_min = dt.transit_min
                        else:
                            status["skipped"] += 1
                        status["processed"] += 1
                    await db3.commit()
                # DB-Session geschlossen!

            except Exception as e:
                status["errors"] += 1
                logger.error(f"Drive Time Backfill Job {job_id}: {e}")
                status["processed"] += len(job_matches)

            # Rate-Limit: Kurze Pause zwischen Jobs
            await asyncio.sleep(0.3)

    except Exception as e:
        logger.error(f"Drive Time Backfill Fehler: {e}")
        status["errors"] += 1
    finally:
        status["running"] = False
        status["finished_at"] = datetime.now(timezone.utc).isoformat()
        logger.info(
            f"Drive Time Backfill fertig: {status['processed']} verarbeitet, "
            f"{status['skipped']} übersprungen, {status['errors']} Fehler"
        )


@router.get("/drive-times/backfill/status")
async def drive_time_backfill_status(request: Request):
    """Status des Drive Time Backfill-Prozesses."""
    status = _drive_time_backfill_status.copy()
    if status["total"] > 0:
        status["percent"] = round(status["processed"] / status["total"] * 100, 1)
    else:
        status["percent"] = 0
    return status


@router.get("/drive-times/debug")
async def drive_time_debug(request: Request, db: AsyncSession = Depends(get_db)):
    """Debug-Endpoint für Drive Time Konfiguration."""
    from app.services.distance_matrix_service import distance_matrix_service
    from app.config import settings
    from app.models.match import Match
    from app.models.job import Job
    from sqlalchemy import func as sa_func

    # Teste eine einzelne Koordinaten-Abfrage
    test_job_coords = None
    try:
        coord_result = await db.execute(
            select(
                Job.id,
                Job.title,
                Job.postal_code,
                sa_func.ST_Y(sa_func.ST_GeomFromWKB(Job.location_coords)).label("lat"),
                sa_func.ST_X(sa_func.ST_GeomFromWKB(Job.location_coords)).label("lng"),
            )
            .where(Job.location_coords.isnot(None))
            .limit(1)
        )
        row = coord_result.first()
        if row:
            test_job_coords = {
                "job_id": str(row.id),
                "title": row.title,
                "postal_code": row.postal_code,
                "lat": row.lat,
                "lng": row.lng,
            }
    except Exception as e:
        test_job_coords = {"error": str(e)}

    # Zähle Jobs mit Koordinaten
    jobs_with_coords = 0
    try:
        result = await db.execute(
            select(func.count(Job.id)).where(Job.location_coords.isnot(None))
        )
        jobs_with_coords = result.scalar() or 0
    except Exception as e:
        jobs_with_coords = f"error: {e}"

    return {
        "has_api_key": distance_matrix_service.has_api_key,
        "api_key_preview": settings.google_maps_api_key[:10] + "..." if settings.google_maps_api_key else "(empty)",
        "api_key_length": len(settings.google_maps_api_key),
        "cache_stats": distance_matrix_service.get_cache_stats(),
        "backfill_status": _drive_time_backfill_status,
        "test_job_coords": test_job_coords,
        "jobs_with_coords": jobs_with_coords,
    }
