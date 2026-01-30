"""Hotlisten Routes - Seiten und API-Endpoints für das Hotlisten & DeepMatch System.

3-Stufen-System:
- /hotlisten → Übersicht der Kategorien (FINANCE, ENGINEERING)
- /match-bereiche → Stadt × Beruf Grid mit Counts
- /deepmatch → Kandidaten-Auswahl + KI-Bewertung
"""

import logging
from typing import Optional
from uuid import UUID

import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func, and_, case
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db, async_session_maker
from app.models.candidate import Candidate
from app.models.job import Job
from app.models.match import Match
from sqlalchemy import text as sa_text, literal_column

from app.services.categorization_service import CategorizationService, HotlistCategory
from app.services.pre_scoring_service import PreScoringService
from app.services.deepmatch_service import DeepMatchService
from app.services.finance_classifier_service import FinanceClassifierService

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# Background-Task Status-Tracking für Finance-Klassifizierung
# ═══════════════════════════════════════════════════════════════
_classification_status: dict = {
    "running": False,
    "started_at": None,
    "target": None,
    "candidates": None,
    "jobs": None,
    "error": None,
}

router = APIRouter(tags=["Hotlisten"])
templates = Jinja2Templates(directory="app/templates")


# ════════════════════════════════════════════════════════════════
# STUFE 1: HOTLISTEN — Kategorie-Übersicht
# ════════════════════════════════════════════════════════════════

@router.get("/hotlisten", response_class=HTMLResponse)
async def hotlisten_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Hotlisten-Übersichtsseite: Zeigt FINANCE und ENGINEERING mit Statistiken."""
    # Kandidaten-Counts pro Kategorie
    candidates_q = await db.execute(
        select(
            Candidate.hotlist_category,
            func.count(Candidate.id),
        )
        .where(Candidate.deleted_at.is_(None))
        .where(Candidate.hotlist_category.isnot(None))
        .group_by(Candidate.hotlist_category)
    )
    candidate_counts = dict(candidates_q.all())

    # Jobs-Counts pro Kategorie
    jobs_q = await db.execute(
        select(
            Job.hotlist_category,
            func.count(Job.id),
        )
        .where(Job.deleted_at.is_(None))
        .where(Job.hotlist_category.isnot(None))
        .group_by(Job.hotlist_category)
    )
    job_counts = dict(jobs_q.all())

    categories = [
        {
            "name": "FINANCE",
            "label": "Finance & Accounting",
            "icon": "💰",
            "description": "Buchhalter, Controller, Steuerfachangestellte",
            "candidates": candidate_counts.get(HotlistCategory.FINANCE, 0),
            "jobs": job_counts.get(HotlistCategory.FINANCE, 0),
            "color": "blue",
        },
        {
            "name": "ENGINEERING",
            "label": "Engineering & Technik",
            "icon": "⚡",
            "description": "Servicetechniker, Elektriker, SHK, Mechanik",
            "candidates": candidate_counts.get(HotlistCategory.ENGINEERING, 0),
            "jobs": job_counts.get(HotlistCategory.ENGINEERING, 0),
            "color": "green",
        },
    ]

    total_uncategorized_candidates = candidate_counts.get(HotlistCategory.SONSTIGE, 0)
    total_uncategorized_jobs = job_counts.get(HotlistCategory.SONSTIGE, 0)

    return templates.TemplateResponse(
        "hotlisten.html",
        {
            "request": request,
            "categories": categories,
            "sonstige_candidates": total_uncategorized_candidates,
            "sonstige_jobs": total_uncategorized_jobs,
        },
    )


# ════════════════════════════════════════════════════════════════
# STUFE 2: MATCH-BEREICHE — Stadt × Beruf Grid
# ════════════════════════════════════════════════════════════════

@router.get("/match-bereiche", response_class=HTMLResponse)
async def match_bereiche_page(
    request: Request,
    category: str = Query(default="FINANCE"),
    db: AsyncSession = Depends(get_db),
):
    """Match-Bereiche: Stadt × Beruf Grid mit Kandidaten- und Job-Counts."""
    # Kandidaten nach Stadt gruppiert
    candidates_by_city = await db.execute(
        select(
            Candidate.hotlist_city,
            func.count(Candidate.id),
        )
        .where(
            and_(
                Candidate.hotlist_category == category,
                Candidate.deleted_at.is_(None),
                Candidate.hotlist_city.isnot(None),
            )
        )
        .group_by(Candidate.hotlist_city)
        .order_by(func.count(Candidate.id).desc())
    )
    candidate_city_counts = dict(candidates_by_city.all())

    # Jobs nach Stadt gruppiert
    jobs_by_city = await db.execute(
        select(
            Job.hotlist_city,
            func.count(Job.id),
        )
        .where(
            and_(
                Job.hotlist_category == category,
                Job.deleted_at.is_(None),
                Job.hotlist_city.isnot(None),
            )
        )
        .group_by(Job.hotlist_city)
        .order_by(func.count(Job.id).desc())
    )
    job_city_counts = dict(jobs_by_city.all())

    # Kandidaten nach Job-Title gruppiert (UNNEST für Multi-Titel-Array)
    candidates_by_title_q = await db.execute(
        sa_text("""
            SELECT unnested_title, COUNT(DISTINCT id)
            FROM candidates, LATERAL unnest(
                COALESCE(hotlist_job_titles, ARRAY[hotlist_job_title])
            ) AS unnested_title
            WHERE hotlist_category = :category
              AND deleted_at IS NULL
              AND (hotlist_job_titles IS NOT NULL OR hotlist_job_title IS NOT NULL)
            GROUP BY unnested_title
            ORDER BY COUNT(DISTINCT id) DESC
        """),
        {"category": category},
    )
    candidate_title_counts = dict(candidates_by_title_q.all())

    # Jobs nach Job-Title gruppiert (UNNEST für Multi-Titel-Array)
    jobs_by_title_q = await db.execute(
        sa_text("""
            SELECT unnested_title, COUNT(DISTINCT id)
            FROM jobs, LATERAL unnest(
                COALESCE(hotlist_job_titles, ARRAY[hotlist_job_title])
            ) AS unnested_title
            WHERE hotlist_category = :category
              AND deleted_at IS NULL
              AND (hotlist_job_titles IS NOT NULL OR hotlist_job_title IS NOT NULL)
            GROUP BY unnested_title
            ORDER BY COUNT(DISTINCT id) DESC
        """),
        {"category": category},
    )
    job_title_counts = dict(jobs_by_title_q.all())

    # Alle Städte sammeln (Union von Kandidaten und Jobs)
    all_cities = sorted(
        set(candidate_city_counts.keys()) | set(job_city_counts.keys())
    )

    # Alle Titel sammeln
    all_titles = sorted(
        set(candidate_title_counts.keys()) | set(job_title_counts.keys())
    )

    # Grid-Daten aufbauen — nach Kandidaten-Anzahl sortiert (Top zuerst)
    city_data = sorted(
        [
            {
                "name": city,
                "candidates": candidate_city_counts.get(city, 0),
                "jobs": job_city_counts.get(city, 0),
            }
            for city in all_cities
        ],
        key=lambda x: x["candidates"],
        reverse=True,
    )

    title_data = sorted(
        [
            {
                "name": title,
                "candidates": candidate_title_counts.get(title, 0),
                "jobs": job_title_counts.get(title, 0),
            }
            for title in all_titles
        ],
        key=lambda x: x["candidates"],
        reverse=True,
    )

    # Stadt × Beruf Kombinationen — Kandidaten (UNNEST für Multi-Titel)
    combo_candidates_q = await db.execute(
        sa_text("""
            SELECT hotlist_city, unnested_title, COUNT(DISTINCT id)
            FROM candidates, LATERAL unnest(
                COALESCE(hotlist_job_titles, ARRAY[hotlist_job_title])
            ) AS unnested_title
            WHERE hotlist_category = :category
              AND deleted_at IS NULL
              AND hotlist_city IS NOT NULL
              AND (hotlist_job_titles IS NOT NULL OR hotlist_job_title IS NOT NULL)
            GROUP BY hotlist_city, unnested_title
            ORDER BY hotlist_city, unnested_title
        """),
        {"category": category},
    )
    combo_cand = {(r[0], r[1]): r[2] for r in combo_candidates_q.all()}

    # Stadt × Beruf Kombinationen — Jobs (UNNEST für Multi-Titel)
    combo_jobs_q = await db.execute(
        sa_text("""
            SELECT hotlist_city, unnested_title, COUNT(DISTINCT id)
            FROM jobs, LATERAL unnest(
                COALESCE(hotlist_job_titles, ARRAY[hotlist_job_title])
            ) AS unnested_title
            WHERE hotlist_category = :category
              AND deleted_at IS NULL
              AND hotlist_city IS NOT NULL
              AND (hotlist_job_titles IS NOT NULL OR hotlist_job_title IS NOT NULL)
            GROUP BY hotlist_city, unnested_title
            ORDER BY hotlist_city, unnested_title
        """),
        {"category": category},
    )
    combo_jobs = {(r[0], r[1]): r[2] for r in combo_jobs_q.all()}

    # Alle Kombinationen sammeln, nach Stadt gruppiert
    all_combos = sorted(set(combo_cand.keys()) | set(combo_jobs.keys()))
    combo_by_city = {}
    for city, title in all_combos:
        if city not in combo_by_city:
            combo_by_city[city] = []
        cand_count = combo_cand.get((city, title), 0)
        jobs_count = combo_jobs.get((city, title), 0)
        combo_by_city[city].append({
            "title": title,
            "candidates": cand_count,
            "jobs": jobs_count,
            "can_match": cand_count > 0 and jobs_count > 0,
        })
    # Nach Kandidaten-Summe sortieren (größte Stadt zuerst)
    combo_data = sorted(
        combo_by_city.items(),
        key=lambda x: sum(t["candidates"] for t in x[1]),
        reverse=True,
    )

    # Gesamtstatistiken für Dashboard-Cards
    total_candidates = sum(candidate_city_counts.values())
    total_jobs = sum(job_city_counts.values())
    total_cities = len(all_cities)
    matchable_combos = sum(
        1 for city_titles in combo_by_city.values()
        for t in city_titles
        if t["can_match"]
    )

    return templates.TemplateResponse(
        "match_bereiche.html",
        {
            "request": request,
            "category": category,
            "category_label": "Finance & Accounting" if category == "FINANCE" else "Engineering & Technik",
            "city_data": city_data,
            "title_data": title_data,
            "combo_data": combo_data,
            "total_candidates": total_candidates,
            "total_jobs": total_jobs,
            "total_cities": total_cities,
            "matchable_combos": matchable_combos,
        },
    )


# ════════════════════════════════════════════════════════════════
# STUFE 3: DEEPMATCH — Kandidaten-Auswahl + KI-Bewertung
# ════════════════════════════════════════════════════════════════

@router.get("/deepmatch", response_class=HTMLResponse)
async def deepmatch_page(
    request: Request,
    category: str = Query(default="FINANCE"),
    city: Optional[str] = None,
    job_title: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """DeepMatch-Seite: Kandidaten mit Pre-Scores, Checkbox-Auswahl, KI-Bewertung."""
    # Matches laden: Kandidat + Job gleiche Kategorie, optional Stadt/Titel filtern
    query = (
        select(Match, Candidate, Job)
        .join(Candidate, Match.candidate_id == Candidate.id)
        .join(Job, Match.job_id == Job.id)
        .where(
            and_(
                Candidate.hotlist_category == category,
                Job.hotlist_category == category,
                Candidate.deleted_at.is_(None),
                Job.deleted_at.is_(None),
            )
        )
    )

    if city:
        query = query.where(Candidate.hotlist_city == city)
    if job_title:
        # ANY() für Array-Match: Kandidat hat job_title in seinen hotlist_job_titles
        query = query.where(
            func.coalesce(Candidate.hotlist_job_titles, func.array([Candidate.hotlist_job_title])).any(job_title)
        )

    # Sortierung: pre_score DESC (beste zuerst), dann ai_score
    query = query.order_by(
        Match.pre_score.desc().nullslast(),
        Match.ai_score.desc().nullslast(),
    )
    query = query.limit(100)  # Max 100 Matches anzeigen

    result = await db.execute(query)
    rows = result.all()

    matches_data = []
    for match, candidate, job in rows:
        matches_data.append({
            "match_id": str(match.id),
            "candidate_name": candidate.full_name,
            "candidate_position": candidate.current_position or "—",
            "candidate_city": candidate.hotlist_city or candidate.city or "—",
            "candidate_job_titles": candidate.hotlist_job_titles or ([candidate.hotlist_job_title] if candidate.hotlist_job_title else []),
            "job_position": job.position,
            "job_company": job.company_name,
            "job_city": job.hotlist_city or job.display_city,
            "job_job_titles": job.hotlist_job_titles or ([job.hotlist_job_title] if job.hotlist_job_title else []),
            "distance_km": round(match.distance_km, 1) if match.distance_km else None,
            "pre_score": round(match.pre_score, 0) if match.pre_score else None,
            "ai_score": round(match.ai_score * 100, 0) if match.ai_score else None,
            "ai_explanation": match.ai_explanation,
            "ai_strengths": match.ai_strengths or [],
            "ai_weaknesses": match.ai_weaknesses or [],
            "status": match.status.value if match.status else "new",
            "user_feedback": match.user_feedback,
        })

    return templates.TemplateResponse(
        "deepmatch.html",
        {
            "request": request,
            "category": category,
            "category_label": "Finance & Accounting" if category == "FINANCE" else "Engineering & Technik",
            "city": city,
            "job_title": job_title,
            "matches": matches_data,
            "total_matches": len(matches_data),
        },
    )


# ════════════════════════════════════════════════════════════════
# API-ENDPOINTS (JSON, für HTMX)
# ════════════════════════════════════════════════════════════════

@router.post("/api/hotlisten/categorize", tags=["Hotlisten API"])
async def trigger_categorization(
    force: bool = Query(default=False),
    db: AsyncSession = Depends(get_db),
):
    """Kategorisiert alle Kandidaten und Jobs."""
    cat_service = CategorizationService(db)
    result = await cat_service.categorize_all(force=force)
    return result


def _make_progress_callback(key: str):
    """Erstellt einen Callback der den globalen Status live aktualisiert."""
    def callback(processed: int, total: int, batch_result):
        _classification_status[key] = {
            "status": "in_progress",
            "processed": processed,
            "total": total,
            "classified": batch_result.classified,
            "skipped_leadership": getattr(batch_result, "skipped_leadership", 0),
            "skipped_no_cv": getattr(batch_result, "skipped_no_cv", 0),
            "skipped_no_role": batch_result.skipped_no_role,
            "skipped_error": batch_result.skipped_error,
            "multi_title_count": batch_result.multi_title_count,
            "roles_distribution": dict(batch_result.roles_distribution),
            "cost_usd": batch_result.cost_usd,
            "classified_candidates": list(batch_result.classified_candidates),
            "unclassified_candidates": list(batch_result.unclassified_candidates),
            "leadership_candidates_count": len(batch_result.leadership_candidates),
            "error_candidates": list(batch_result.error_candidates),
        }
    return callback


def _finalize_result(batch_result, include_leadership: bool = False) -> dict:
    """Konvertiert BatchClassificationResult in ein dict."""
    d = {
        "status": "done",
        "total": batch_result.total,
        "classified": batch_result.classified,
        "skipped_no_role": batch_result.skipped_no_role,
        "skipped_error": batch_result.skipped_error,
        "multi_title_count": batch_result.multi_title_count,
        "roles_distribution": dict(batch_result.roles_distribution),
        "cost_usd": batch_result.cost_usd,
        "duration_seconds": batch_result.duration_seconds,
        "classified_candidates": list(batch_result.classified_candidates),
        "unclassified_candidates": list(batch_result.unclassified_candidates),
        "error_candidates": list(batch_result.error_candidates),
    }
    if include_leadership:
        d["skipped_leadership"] = batch_result.skipped_leadership
        d["skipped_no_cv"] = batch_result.skipped_no_cv
        d["leadership_candidates"] = list(batch_result.leadership_candidates)
    return d


async def _run_classification_background(target: str, force: bool) -> None:
    """Background-Task: Klassifiziert FINANCE-Kandidaten/Jobs via OpenAI."""
    global _classification_status
    try:
        async with async_session_maker() as db:
            service = FinanceClassifierService(db)

            if target in ("candidates", "both"):
                cand_result = await service.classify_all_finance_candidates(
                    force=force,
                    progress_callback=_make_progress_callback("candidates"),
                )
                _classification_status["candidates"] = _finalize_result(cand_result, include_leadership=True)
                logger.info(f"Finance-Klassifizierung Kandidaten fertig: {cand_result.classified}/{cand_result.total}")

            if target in ("jobs", "both"):
                job_result = await service.classify_all_finance_jobs(force=force)
                _classification_status["jobs"] = _finalize_result(job_result)
                logger.info(f"Finance-Klassifizierung Jobs fertig: {job_result.classified}/{job_result.total}")

    except Exception as e:
        logger.error(f"Finance-Klassifizierung Fehler: {e}")
        _classification_status["error"] = str(e)
    finally:
        _classification_status["running"] = False


@router.post("/api/hotlisten/classify-finance", tags=["Hotlisten API"])
async def trigger_finance_classification(
    force: bool = Query(default=False),
    target: str = Query(default="candidates"),  # "candidates", "jobs", "both"
):
    """Startet Finance-Klassifizierung als Background-Task (kein Timeout)."""
    global _classification_status

    if _classification_status["running"]:
        return {
            "status": "already_running",
            "started_at": _classification_status["started_at"],
            "target": _classification_status["target"],
            "message": "Klassifizierung laeuft bereits. Nutze GET /api/hotlisten/classify-finance/status",
        }

    _classification_status = {
        "running": True,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "target": target,
        "candidates": None,
        "jobs": None,
        "error": None,
    }

    # Background-Task starten (kein await — läuft asynchron)
    asyncio.create_task(_run_classification_background(target, force))

    return {
        "status": "started",
        "target": target,
        "force": force,
        "message": f"Klassifizierung fuer '{target}' gestartet. Nutze GET /api/hotlisten/classify-finance/status",
    }


@router.get("/api/hotlisten/classify-finance/status", tags=["Hotlisten API"])
async def classification_status():
    """Gibt den aktuellen Status der Finance-Klassifizierung zurueck."""
    return _classification_status


@router.get("/api/hotlisten/classify-finance/compare", tags=["Hotlisten API"])
async def compare_openai_vs_rules(
    db: AsyncSession = Depends(get_db),
    limit_count: int = Query(default=0, alias="limit"),
):
    """Vergleicht OpenAI-Ergebnisse mit RulesEngine für alle FINANCE-Kandidaten.

    Zeigt: Übereinstimmungen, Abweichungen, fehlende Keywords.
    Dient zum Training des lokalen Algorithmus.
    """
    from app.services.finance_rules_engine import FinanceRulesEngine

    # Alle Kandidaten mit OpenAI classification_data laden
    query = (
        select(Candidate)
        .where(
            and_(
                Candidate.hotlist_category == "FINANCE",
                Candidate.deleted_at.is_(None),
                Candidate.classification_data.isnot(None),
            )
        )
    )
    if limit_count > 0:
        query = query.limit(limit_count)

    result = await db.execute(query)
    candidates = list(result.scalars().all())

    engine = FinanceRulesEngine()

    # Statistiken
    total = 0
    exact_match = 0
    partial_match = 0
    no_match = 0
    openai_only_roles = {}      # Rollen die nur OpenAI erkennt
    rules_only_roles = {}       # Rollen die nur RulesEngine erkennt
    mismatches = []             # Detaillierte Abweichungen
    missing_keywords = []       # Was der RulesEngine fehlt

    for candidate in candidates:
        cd = candidate.classification_data
        if not cd or not isinstance(cd, dict):
            continue

        openai_roles = set(cd.get("roles", []))
        openai_leadership = cd.get("is_leadership", False)

        # RulesEngine laufen lassen
        rules_result = engine.classify_candidate(candidate)
        rules_roles = set(rules_result.roles)
        rules_leadership = rules_result.is_leadership

        # Nur zählen wenn OpenAI eine echte Klassifizierung hatte
        if openai_leadership and rules_leadership:
            exact_match += 1
            total += 1
            continue
        if openai_leadership and not rules_leadership:
            total += 1
            mismatches.append({
                "id": str(candidate.id),
                "name": candidate.full_name,
                "position": candidate.current_position,
                "type": "leadership_missed",
                "openai": {"is_leadership": True, "reasoning": cd.get("reasoning", "")},
                "rules": {"is_leadership": False, "roles": list(rules_roles)},
            })
            no_match += 1
            continue
        if not openai_leadership and rules_leadership:
            total += 1
            mismatches.append({
                "id": str(candidate.id),
                "name": candidate.full_name,
                "position": candidate.current_position,
                "type": "leadership_false_positive",
                "openai": {"roles": list(openai_roles), "reasoning": cd.get("reasoning", "")},
                "rules": {"is_leadership": True},
            })
            no_match += 1
            continue

        total += 1

        if openai_roles == rules_roles:
            exact_match += 1
        elif openai_roles & rules_roles:
            # Teilweise Übereinstimmung
            partial_match += 1
            only_openai = openai_roles - rules_roles
            only_rules = rules_roles - openai_roles
            for r in only_openai:
                openai_only_roles[r] = openai_only_roles.get(r, 0) + 1
            for r in only_rules:
                rules_only_roles[r] = rules_only_roles.get(r, 0) + 1
            mismatches.append({
                "id": str(candidate.id),
                "name": candidate.full_name,
                "position": candidate.current_position,
                "type": "partial",
                "openai_roles": sorted(openai_roles),
                "rules_roles": sorted(rules_roles),
                "only_openai": sorted(only_openai),
                "only_rules": sorted(only_rules),
                "openai_reasoning": cd.get("reasoning", ""),
            })
        else:
            no_match += 1
            for r in openai_roles:
                openai_only_roles[r] = openai_only_roles.get(r, 0) + 1
            for r in rules_roles:
                rules_only_roles[r] = rules_only_roles.get(r, 0) + 1
            mismatches.append({
                "id": str(candidate.id),
                "name": candidate.full_name,
                "position": candidate.current_position,
                "type": "complete_mismatch",
                "openai_roles": sorted(openai_roles),
                "rules_roles": sorted(rules_roles),
                "openai_reasoning": cd.get("reasoning", ""),
                "rules_reasoning": rules_result.reasoning,
            })

    accuracy_exact = round(exact_match / total * 100, 1) if total else 0
    accuracy_partial = round((exact_match + partial_match) / total * 100, 1) if total else 0

    return {
        "summary": {
            "total_compared": total,
            "exact_match": exact_match,
            "partial_match": partial_match,
            "no_match": no_match,
            "accuracy_exact": f"{accuracy_exact}%",
            "accuracy_with_partial": f"{accuracy_partial}%",
        },
        "roles_only_openai_sees": dict(sorted(openai_only_roles.items(), key=lambda x: -x[1])),
        "roles_only_rules_sees": dict(sorted(rules_only_roles.items(), key=lambda x: -x[1])),
        "mismatches_count": len(mismatches),
        "mismatches": mismatches[:100],  # Max 100 Detaileinträge
    }


@router.post("/api/hotlisten/pre-score", tags=["Hotlisten API"])
async def trigger_pre_scoring(
    category: str = Query(default="FINANCE"),
    city: Optional[str] = None,
    force: bool = Query(default=False),
    db: AsyncSession = Depends(get_db),
):
    """Berechnet Pre-Scores für Matches einer Kategorie."""
    service = PreScoringService(db)
    result = await service.score_matches_for_category(
        category=category,
        city=city,
        force=force,
    )
    return {
        "total": result.total_matches,
        "scored": result.scored,
        "skipped": result.skipped,
        "avg_score": result.avg_score,
    }


@router.post("/api/deepmatch/evaluate", tags=["Hotlisten API"])
async def evaluate_deepmatch(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Führt DeepMatch-Bewertung für ausgewählte Matches durch."""
    body = await request.json()
    match_ids = [UUID(mid) for mid in body.get("match_ids", [])]

    if not match_ids:
        raise HTTPException(status_code=400, detail="Keine Matches ausgewählt")

    if len(match_ids) > 20:
        raise HTTPException(status_code=400, detail="Maximal 20 Matches pro Batch")

    async with DeepMatchService(db) as service:
        result = await service.evaluate_selected_matches(match_ids)

    return {
        "total_requested": result.total_requested,
        "evaluated": result.evaluated,
        "skipped_low_score": result.skipped_low_score,
        "skipped_error": result.skipped_error,
        "avg_ai_score": result.avg_ai_score,
        "total_cost_usd": result.total_cost_usd,
        "results": [
            {
                "match_id": str(r.match_id),
                "candidate_name": r.candidate_name,
                "job_position": r.job_position,
                "ai_score": r.ai_score,
                "explanation": r.explanation,
                "strengths": r.strengths,
                "weaknesses": r.weaknesses,
                "success": r.success,
            }
            for r in result.results
        ],
    }


@router.post("/api/deepmatch/feedback", tags=["Hotlisten API"])
async def save_deepmatch_feedback(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Speichert User-Feedback zu einem DeepMatch-Ergebnis."""
    body = await request.json()
    match_id = UUID(body.get("match_id"))
    feedback = body.get("feedback")  # "good", "neutral", "bad"
    note = body.get("note")

    if feedback not in ("good", "neutral", "bad"):
        raise HTTPException(status_code=400, detail="Ungültiges Feedback")

    async with DeepMatchService(db) as service:
        success = await service.save_feedback(match_id, feedback, note)

    if not success:
        raise HTTPException(status_code=404, detail="Match nicht gefunden")

    return {"success": True}
