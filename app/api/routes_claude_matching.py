"""Claude Matching v4 — API Routes.

Endpoints (Automatisch):
  POST /claude-match/run               — Matching starten (alle Stufen, Background-Task)
  POST /claude-match/run-auto          — Alias fuer /run (n8n Cron)
  GET  /claude-match/status             — Live-Fortschritt
  GET  /claude-match/daily              — Heutige Top-Matches fuer Action Board
  POST /claude-match/{match_id}/action  — Vorstellen/Spaeter/Ablehnen
  POST /claude-match/candidate/{id}     — Ad-hoc: Jobs fuer einen Kandidaten finden

Endpoints (Kontrolliert — Stufe fuer Stufe):
  POST /claude-match/run-stufe-0       — Stufe 0: Paare laden + Session erstellen
  POST /claude-match/run-stufe-1       — Stufe 1: Quick-Check (Claude)
  POST /claude-match/run-stufe-2       — Stufe 2: Deep Assessment + Speichern
  POST /claude-match/exclude-pairs     — Paare aus Session ausschliessen
  GET  /claude-match/session/{id}      — Session-Daten abfragen
  GET  /claude-match/sessions          — Alle Sessions auflisten

Debug:
  GET  /debug/match-count               — Match-Statistiken
  GET  /debug/stufe-0-preview           — Dry-Run Stufe 0
  GET  /debug/job-health                — Job-Daten Gesundheitscheck
  GET  /debug/candidate-health          — Kandidaten-Daten Gesundheitscheck
  GET  /debug/match/{match_id}          — Match-Detail mit Claude-Input/Output
  GET  /debug/cost-report               — API-Kosten
"""

import logging
from datetime import datetime, date, timezone, timedelta
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel
import sqlalchemy as sa
from sqlalchemy import select, func, and_, case, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.match import Match, MatchStatus
from app.models.candidate import Candidate
from app.models.job import Job

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Claude Matching v4"])


# ══════════════════════════════════════════════════════════════
# Schemas
# ══════════════════════════════════════════════════════════════

class ActionRequest(BaseModel):
    """Request Body fuer Match-Aktionen."""
    action: str  # vorstellen, spaeter, ablehnen
    note: str | None = None


class ExcludePairsRequest(BaseModel):
    """Request Body fuer Paare ausschliessen."""
    session_id: str
    pairs: list[dict]  # [{"candidate_id": "...", "job_id": "..."}]


# ══════════════════════════════════════════════════════════════
# Matching-Endpoints
# ══════════════════════════════════════════════════════════════

@router.post("/claude-match/run")
async def start_matching(
    background_tasks: BackgroundTasks,
    model_quick: str = Query(default="claude-haiku-4-5-20251001", description="Modell fuer Stufe 1"),
    model_deep: str = Query(default="claude-haiku-4-5-20251001", description="Modell fuer Stufe 2"),
):
    """Startet das Claude Matching als Background-Task."""
    from app.services.claude_matching_service import get_status, run_matching

    status = get_status()
    if status["running"]:
        return {
            "status": "already_running",
            "message": "Matching laeuft bereits. Fortschritt unter /status abrufbar.",
            "progress": status["progress"],
        }

    async def _background():
        await run_matching(
            model_quick=model_quick,
            model_deep=model_deep,
        )

    background_tasks.add_task(_background)

    return {
        "status": "started",
        "message": "Claude Matching v4 gestartet. Fortschritt unter /api/v4/claude-match/status.",
    }


@router.get("/claude-match/status")
async def matching_status():
    """Gibt den aktuellen Matching-Status zurueck (Live-Fortschritt)."""
    from app.services.claude_matching_service import get_status
    return get_status()


@router.get("/claude-match/daily")
async def daily_matches(
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=20, ge=1, le=100),
    include_wow: bool = Query(default=True),
    include_followups: bool = Query(default=True),
):
    """Holt heutige Top-Matches fuer das Action Board."""
    today = date.today()
    today_start = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)

    # ── Top-Matches: empfehlung="vorstellen", heute erstellt, kein Feedback ──
    top_query = (
        select(
            Match.id.label("match_id"),
            Match.candidate_id,
            Match.job_id,
            Match.v2_score.label("ai_score"),
            Match.ai_explanation,
            Match.ai_strengths,
            Match.ai_weaknesses,
            Match.empfehlung,
            Match.wow_faktor,
            Match.wow_grund,
            Match.distance_km,
            Match.drive_time_car_min,
            Match.drive_time_transit_min,
            Match.matching_method,
            Match.created_at,
            # Kandidaten-Info (NUR nicht-persoenliche Daten!)
            Candidate.city.label("candidate_city"),
            Candidate.current_position.label("candidate_position"),
            Candidate.salary.label("candidate_salary"),
            Candidate.hotlist_job_title.label("candidate_role"),
            # Job-Info
            Job.position.label("job_position"),
            Job.company_name.label("job_company"),
            Job.city.label("job_city"),
        )
        .outerjoin(Candidate, Match.candidate_id == Candidate.id)
        .outerjoin(Job, Match.job_id == Job.id)
        .where(
            and_(
                Match.matching_method == "claude_match",
                Match.empfehlung == "vorstellen",
                Match.user_feedback.is_(None),
                Match.created_at >= today_start,
            )
        )
        .order_by(Match.v2_score.desc())
        .limit(limit)
    )

    result = await db.execute(top_query)
    top_matches = [dict(row._mapping) for row in result.all()]

    # ── Wow-Matches ──
    wow_matches = []
    if include_wow:
        wow_query = (
            select(
                Match.id.label("match_id"),
                Match.candidate_id,
                Match.job_id,
                Match.v2_score.label("ai_score"),
                Match.ai_explanation,
                Match.ai_strengths,
                Match.ai_weaknesses,
                Match.empfehlung,
                Match.wow_faktor,
                Match.wow_grund,
                Match.distance_km,
                Match.drive_time_car_min,
                Match.drive_time_transit_min,
                Match.created_at,
                Candidate.city.label("candidate_city"),
                Candidate.current_position.label("candidate_position"),
                Candidate.salary.label("candidate_salary"),
                Candidate.hotlist_job_title.label("candidate_role"),
                Candidate.willingness_to_change,
                Job.position.label("job_position"),
                Job.company_name.label("job_company"),
                Job.city.label("job_city"),
            )
            .outerjoin(Candidate, Match.candidate_id == Candidate.id)
            .outerjoin(Job, Match.job_id == Job.id)
            .where(
                and_(
                    Match.matching_method == "claude_match",
                    Match.wow_faktor == True,
                    Match.user_feedback.is_(None),
                    Match.created_at >= today_start,
                )
            )
            .order_by(Match.v2_score.desc())
            .limit(10)
        )
        result = await db.execute(wow_query)
        wow_matches = [dict(row._mapping) for row in result.all()]

    # ── Follow-ups (Spaeter von gestern/vorgestern) ──
    follow_ups = []
    if include_followups:
        followup_query = (
            select(
                Match.id.label("match_id"),
                Match.candidate_id,
                Match.job_id,
                Match.v2_score.label("ai_score"),
                Match.ai_explanation,
                Match.ai_strengths,
                Match.ai_weaknesses,
                Match.empfehlung,
                Match.wow_faktor,
                Match.distance_km,
                Match.drive_time_car_min,
                Match.feedback_at,
                Candidate.city.label("candidate_city"),
                Candidate.current_position.label("candidate_position"),
                Candidate.hotlist_job_title.label("candidate_role"),
                Job.position.label("job_position"),
                Job.company_name.label("job_company"),
                Job.city.label("job_city"),
            )
            .outerjoin(Candidate, Match.candidate_id == Candidate.id)
            .outerjoin(Job, Match.job_id == Job.id)
            .where(
                and_(
                    Match.matching_method == "claude_match",
                    Match.user_feedback == "spaeter",
                    Match.feedback_at < today_start,  # Von gestern oder aelter
                )
            )
            .order_by(Match.v2_score.desc())
            .limit(10)
        )
        result = await db.execute(followup_query)
        follow_ups = [dict(row._mapping) for row in result.all()]

    # ── Naehe-Matches ──
    proximity_query = (
        select(
            Match.id.label("match_id"),
            Match.candidate_id,
            Match.job_id,
            Match.distance_km,
            Match.created_at,
            Candidate.city.label("candidate_city"),
            Candidate.hotlist_job_title.label("candidate_role"),
            Job.position.label("job_position"),
            Job.company_name.label("job_company"),
            Job.city.label("job_city"),
        )
        .outerjoin(Candidate, Match.candidate_id == Candidate.id)
        .outerjoin(Job, Match.job_id == Job.id)
        .where(
            and_(
                Match.matching_method == "proximity_match",
                Match.user_feedback.is_(None),
                Match.created_at >= today_start,
            )
        )
        .order_by(Match.distance_km.asc())
        .limit(20)
    )
    result = await db.execute(proximity_query)
    proximity_matches = [dict(row._mapping) for row in result.all()]

    # Alle UUIDs und datetimes serialisierbar machen
    def _serialize(matches: list[dict]) -> list[dict]:
        for m in matches:
            for k, v in m.items():
                if isinstance(v, UUID):
                    m[k] = str(v)
                elif isinstance(v, datetime):
                    m[k] = v.isoformat()
        return matches

    return {
        "top_matches": _serialize(top_matches),
        "wow_matches": _serialize(wow_matches),
        "follow_ups": _serialize(follow_ups),
        "proximity_matches": _serialize(proximity_matches),
        "summary": {
            "total_top": len(top_matches),
            "total_wow": len(wow_matches),
            "total_followups": len(follow_ups),
            "total_proximity": len(proximity_matches),
        },
    }


@router.post("/claude-match/{match_id}/action")
async def match_action(
    match_id: UUID,
    body: ActionRequest,
    db: AsyncSession = Depends(get_db),
):
    """Verarbeitet Dashboard-Aktionen: vorstellen, spaeter, ablehnen."""
    match = await db.get(Match, match_id)
    if not match:
        raise HTTPException(status_code=404, detail="Match nicht gefunden")

    now = datetime.now(timezone.utc)

    if body.action == "vorstellen":
        match.user_feedback = "vorstellen"
        match.feedback_at = now
        match.status = MatchStatus.PRESENTED
        match.presentation_status = "prepared"
        if body.note:
            match.feedback_note = body.note

        # ATS Integration: Kandidat in Pipeline einfuegen
        try:
            from app.models.ats_job import ATSJob
            from app.models.ats_pipeline import ATSPipelineEntry, PipelineStage

            # ATSJob fuer diesen Job suchen
            ats_job_result = await db.execute(
                select(ATSJob).where(ATSJob.source_job_id == match.job_id)
            )
            ats_job = ats_job_result.scalar_one_or_none()

            if ats_job:
                # Pruefen ob Kandidat schon in Pipeline
                existing = await db.execute(
                    select(ATSPipelineEntry).where(
                        ATSPipelineEntry.ats_job_id == ats_job.id,
                        ATSPipelineEntry.candidate_id == match.candidate_id,
                    )
                )
                if not existing.scalar_one_or_none():
                    entry = ATSPipelineEntry(
                        ats_job_id=ats_job.id,
                        candidate_id=match.candidate_id,
                        stage=PipelineStage.MATCHED,
                    )
                    db.add(entry)
                    logger.info("ATS Pipeline Entry erstellt fuer Match %s", match_id)
        except Exception as e:
            logger.warning("ATS Integration fuer Match %s: %s", match_id, e)

    elif body.action == "spaeter":
        match.user_feedback = "spaeter"
        match.feedback_at = now
        if body.note:
            match.feedback_note = body.note

    elif body.action == "job_an_kandidat":
        match.user_feedback = "job_an_kandidat"
        match.feedback_at = now
        match.status = MatchStatus.PRESENTED
        match.presentation_status = "prepared"
        if body.note:
            match.feedback_note = body.note

    elif body.action == "profil_an_kunden":
        match.user_feedback = "profil_an_kunden"
        match.feedback_at = now
        match.status = MatchStatus.PRESENTED
        match.presentation_status = "prepared"
        if body.note:
            match.feedback_note = body.note

        # ATS Integration: Kandidat in Pipeline einfuegen
        try:
            from app.models.ats_job import ATSJob
            from app.models.ats_pipeline import ATSPipelineEntry, PipelineStage

            ats_job_result = await db.execute(
                select(ATSJob).where(ATSJob.source_job_id == match.job_id)
            )
            ats_job = ats_job_result.scalar_one_or_none()

            if ats_job:
                existing = await db.execute(
                    select(ATSPipelineEntry).where(
                        ATSPipelineEntry.ats_job_id == ats_job.id,
                        ATSPipelineEntry.candidate_id == match.candidate_id,
                    )
                )
                if not existing.scalar_one_or_none():
                    entry = ATSPipelineEntry(
                        ats_job_id=ats_job.id,
                        candidate_id=match.candidate_id,
                        stage=PipelineStage.MATCHED,
                    )
                    db.add(entry)
                    logger.info("ATS Pipeline Entry erstellt fuer Match %s (profil_an_kunden)", match_id)
        except Exception as e:
            logger.warning("ATS Integration fuer Match %s: %s", match_id, e)

    elif body.action == "ablehnen":
        match.user_feedback = "ablehnen"
        match.feedback_at = now
        match.status = MatchStatus.REJECTED
        if body.note:
            match.feedback_note = body.note
            match.rejection_reason = body.note[:50]

    else:
        raise HTTPException(status_code=400, detail=f"Unbekannte Aktion: {body.action}")

    await db.commit()

    return {"success": True, "match_id": str(match_id), "action": body.action}


@router.get("/claude-match/{match_id}/contacts")
async def get_match_contacts(
    match_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Laedt Kontakte des Unternehmens fuer die Empfaenger-Auswahl bei 'Profil an Kunden'.

    Gibt alle CompanyContacts fuer die Firma des Jobs zurueck.
    """
    match = await db.get(Match, match_id)
    if not match:
        raise HTTPException(status_code=404, detail="Match nicht gefunden")

    # Job laden um company_id zu bekommen
    job = await db.execute(select(Job).where(Job.id == match.job_id))
    job_obj = job.scalar_one_or_none()
    if not job_obj:
        raise HTTPException(status_code=404, detail="Job nicht gefunden")

    contacts = []
    company_name = job_obj.company_name or "Unbekannt"

    # CompanyContacts laden wenn company_id vorhanden
    if job_obj.company_id:
        try:
            from app.models.company_contact import CompanyContact

            contact_result = await db.execute(
                select(CompanyContact).where(
                    CompanyContact.company_id == job_obj.company_id
                )
            )
            for c in contact_result.scalars().all():
                name_parts = []
                if c.first_name:
                    name_parts.append(c.first_name)
                if c.last_name:
                    name_parts.append(c.last_name)
                contacts.append({
                    "contact_id": str(c.id),
                    "name": " ".join(name_parts) or "Unbekannt",
                    "position": c.position or "",
                    "email": c.email or "",
                    "phone": c.phone or c.mobile or "",
                })
        except Exception as e:
            logger.warning("Kontakte laden fuer Match %s: %s", match_id, e)

    return {
        "match_id": str(match_id),
        "job_id": str(match.job_id),
        "job_position": job_obj.position or "",
        "company_name": company_name,
        "contacts": contacts,
    }


@router.post("/claude-match/candidate/{candidate_id}")
async def match_for_candidate(
    candidate_id: UUID,
    background_tasks: BackgroundTasks,
):
    """Ad-hoc: Finde passende Jobs fuer einen bestimmten Kandidaten."""
    from app.services.claude_matching_service import get_status, run_matching

    status = get_status()
    if status["running"]:
        return {
            "status": "already_running",
            "message": "Matching laeuft bereits.",
        }

    async def _background():
        await run_matching(candidate_id=str(candidate_id))

    background_tasks.add_task(_background)

    return {
        "status": "started",
        "message": f"Suche Jobs fuer Kandidat {candidate_id}...",
    }


# Alias fuer n8n Cron
@router.post("/claude-match/run-auto")
async def start_matching_auto(
    background_tasks: BackgroundTasks,
):
    """Alias fuer /run — fuer n8n Morgen-Cron (automatisch, ohne Kontrolle)."""
    from app.services.claude_matching_service import get_status, run_matching

    status = get_status()
    if status["running"]:
        return {"status": "already_running", "message": "Matching laeuft bereits."}

    async def _background():
        await run_matching()

    background_tasks.add_task(_background)
    return {"status": "started", "message": "Automatisches Matching gestartet."}


# ══════════════════════════════════════════════════════════════
# Kontrolliertes Matching — Stufe fuer Stufe
# ══════════════════════════════════════════════════════════════

@router.post("/claude-match/run-stufe-0")
async def start_stufe_0():
    """Stufe 0: Paare aus DB laden + Session erstellen. KEIN Claude-Call."""
    from app.services.claude_matching_service import run_stufe_0
    result = await run_stufe_0()
    return result


@router.post("/claude-match/run-stufe-1")
async def start_stufe_1(
    background_tasks: BackgroundTasks,
    session_id: str = Query(..., description="Session-ID von Stufe 0"),
    model_quick: str = Query(default="claude-haiku-4-5-20251001"),
):
    """Stufe 1: Quick-Check per Claude. Laeuft als Background-Task."""
    from app.services.claude_matching_service import get_status, get_session, run_stufe_1

    status = get_status()
    if status["running"]:
        return {"status": "already_running", "message": "Matching laeuft bereits."}

    session = get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session nicht gefunden")

    if session["current_stufe"] >= 1:
        raise HTTPException(status_code=400, detail="Stufe 1 wurde bereits ausgefuehrt")

    # Stufe 1 als Background-Task starten
    _stufe_1_result: dict = {}

    async def _background():
        nonlocal _stufe_1_result
        _stufe_1_result = await run_stufe_1(session_id=session_id, model_quick=model_quick)

    background_tasks.add_task(_background)

    return {
        "status": "started",
        "session_id": session_id,
        "message": "Stufe 1 (Quick-Check) gestartet. Fortschritt unter /api/v4/claude-match/status.",
    }


@router.post("/claude-match/run-stufe-2")
async def start_stufe_2(
    background_tasks: BackgroundTasks,
    session_id: str = Query(..., description="Session-ID"),
    model_deep: str = Query(default="claude-haiku-4-5-20251001"),
):
    """Stufe 2: Deep Assessment + Matches speichern. Laeuft als Background-Task."""
    from app.services.claude_matching_service import get_status, get_session, run_stufe_2

    status = get_status()
    if status["running"]:
        return {"status": "already_running", "message": "Matching laeuft bereits."}

    session = get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session nicht gefunden")

    if session["current_stufe"] < 1:
        raise HTTPException(status_code=400, detail="Stufe 1 muss zuerst ausgefuehrt werden")

    if session["current_stufe"] >= 2:
        raise HTTPException(status_code=400, detail="Stufe 2 wurde bereits ausgefuehrt")

    async def _background():
        await run_stufe_2(session_id=session_id, model_deep=model_deep)

    background_tasks.add_task(_background)

    return {
        "status": "started",
        "session_id": session_id,
        "message": "Stufe 2 (Deep Assessment) gestartet. Fortschritt unter /api/v4/claude-match/status.",
    }


@router.post("/claude-match/exclude-pairs")
async def exclude_pairs(body: ExcludePairsRequest):
    """Paare aus einer Session ausschliessen (vor Stufe 1 oder vor Stufe 2)."""
    from app.services.claude_matching_service import exclude_pairs_from_session

    result = exclude_pairs_from_session(body.session_id, body.pairs)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@router.get("/claude-match/session/{session_id}")
async def get_session_data(session_id: str):
    """Session-Daten abfragen (Paare, Ergebnisse, Status)."""
    from app.services.claude_matching_service import get_session

    session = get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session nicht gefunden")

    # Aufbereitete Daten zurueckgeben (keine vollen Paare mit Rohdaten)
    excluded_count = len(session.get("excluded_pairs", set()))

    # Display-Paare aus claude_pairs erstellen
    display_pairs = []
    excluded = session.get("excluded_pairs", set())
    for p in session.get("claude_pairs", []):
        cid = str(p["candidate_id"])
        jid = str(p["job_id"])
        is_excluded = (cid, jid) in excluded
        distance_m = p.get("distance_m")
        fn = p.get("candidate_first_name") or ""
        ln = p.get("candidate_last_name") or ""
        display_pairs.append({
            "candidate_id": cid,
            "job_id": jid,
            "candidate_name": f"{fn} {ln}".strip() or "Unbekannt",
            "candidate_role": p.get("candidate_role") or "Unbekannt",
            "candidate_position": p.get("candidate_position") or "",
            "candidate_city": p.get("candidate_city") or "Unbekannt",
            "job_position": p.get("position") or "Unbekannt",
            "job_company": p.get("company_name") or "Unbekannt",
            "job_city": p.get("job_city") or "Unbekannt",
            "distance_km": round(distance_m / 1000, 1) if distance_m else None,
            "excluded": is_excluded,
        })

    return {
        "session_id": session_id,
        "created_at": session.get("created_at"),
        "current_stufe": session.get("current_stufe"),
        "total_claude_pairs": len(session.get("claude_pairs", [])),
        "excluded_count": excluded_count,
        "active_pairs": len(session.get("claude_pairs", [])) - excluded_count,
        "claude_pairs": display_pairs,
        "passed_pairs": [
            {
                "candidate_id": str(p.get("candidate_id")),
                "job_id": str(p.get("job_id")),
                "candidate_name": f"{p.get('candidate_first_name') or ''} {p.get('candidate_last_name') or ''}".strip() or "Unbekannt",
                "candidate_role": p.get("candidate_role") or "Unbekannt",
                "candidate_city": p.get("candidate_city") or "Unbekannt",
                "job_position": p.get("position") or "Unbekannt",
                "job_company": p.get("company_name") or "Unbekannt",
                "job_city": p.get("job_city") or "Unbekannt",
                "distance_km": round(p.get("distance_m", 0) / 1000, 1) if p.get("distance_m") else None,
                "quick_reason": p.get("quick_reason", ""),
                "excluded": (str(p.get("candidate_id")), str(p.get("job_id"))) in excluded,
            }
            for p in session.get("passed_pairs", [])
        ],
        "failed_pairs": session.get("failed_pairs", []),
        "deep_results": [
            {
                "candidate_id": str(dr.get("candidate_id")),
                "job_id": str(dr.get("job_id")),
                "candidate_name": dr.get("candidate_name", "Unbekannt"),
                "score": dr.get("score"),
                "empfehlung": dr.get("empfehlung"),
                "wow_faktor": dr.get("wow_faktor"),
                "zusammenfassung": dr.get("zusammenfassung"),
                "distance_km": dr.get("distance_km"),
            }
            for dr in session.get("deep_results", [])
        ],
        "matches_saved": session.get("matches_saved", 0),
    }


@router.get("/claude-match/sessions")
async def list_sessions():
    """Alle aktiven Sessions auflisten."""
    from app.services.claude_matching_service import get_all_sessions
    return get_all_sessions()


# ══════════════════════════════════════════════════════════════
# Vergleichs-Endpoint fuer Paare OHNE Match
# ══════════════════════════════════════════════════════════════

@router.get("/claude-match/compare-pair")
async def compare_pair(
    candidate_id: UUID = Query(...),
    job_id: UUID = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """Vergleichs-Daten fuer ein Kandidat-Job-Paar (auch ohne bestehenden Match).

    Wird verwendet fuer den Vergleichs-Button in Stufe 0 und Stufe 1.
    """
    # Zuerst pruefen ob ein Match existiert
    existing_match = await db.execute(
        select(Match.id).where(
            and_(Match.candidate_id == candidate_id, Match.job_id == job_id)
        )
    )
    match_row = existing_match.scalar_one_or_none()

    if match_row:
        # Match existiert — normalen Compare-Endpoint verwenden
        from app.services.match_center_service import MatchCenterService
        service = MatchCenterService(db)
        comparison = await service.get_match_comparison(match_row)
        if comparison:
            return {
                "has_match": True,
                "match_id": str(match_row),
                "data": comparison.__dict__ if hasattr(comparison, "__dict__") else comparison,
            }

    # Kein Match — Daten direkt aus DB laden
    candidate = await db.execute(
        select(
            Candidate.id,
            Candidate.first_name,
            Candidate.last_name,
            Candidate.city,
            Candidate.postal_code,
            Candidate.street_address,
            Candidate.current_position,
            Candidate.current_company,
            Candidate.work_history,
            Candidate.education,
            Candidate.further_education,
            Candidate.languages,
            Candidate.it_skills,
            Candidate.skills,
            Candidate.hotlist_job_title,
            Candidate.salary,
        ).where(Candidate.id == candidate_id)
    )
    cand = candidate.one_or_none()
    if not cand:
        raise HTTPException(status_code=404, detail="Kandidat nicht gefunden")

    job = await db.execute(
        select(
            Job.id,
            Job.position,
            Job.company_name,
            Job.city,
            Job.postal_code,
            Job.street_address,
            Job.job_text,
        ).where(Job.id == job_id)
    )
    j = job.one_or_none()
    if not j:
        raise HTTPException(status_code=404, detail="Job nicht gefunden")

    name_parts = []
    if cand.first_name:
        name_parts.append(cand.first_name)
    if cand.last_name:
        name_parts.append(cand.last_name)

    return {
        "has_match": False,
        "match_id": None,
        "data": {
            "candidate_id": str(cand.id),
            "candidate_name": " ".join(name_parts) or "Unbekannt",
            "candidate_city": cand.city or "",
            "candidate_postal_code": cand.postal_code or "",
            "candidate_street_address": cand.street_address or "",
            "candidate_current_position": cand.current_position or "",
            "candidate_current_company": cand.current_company or "",
            "candidate_role": cand.hotlist_job_title or "",
            "candidate_salary": cand.salary or "",
            "work_history": cand.work_history,
            "education": cand.education,
            "further_education": cand.further_education,
            "languages": cand.languages,
            "it_skills": cand.it_skills,
            "skills": cand.skills,
            "job_id": str(j.id),
            "job_position": j.position or "",
            "job_company_name": j.company_name or "",
            "job_city": j.city or "",
            "job_postal_code": j.postal_code or "",
            "job_street_address": j.street_address or "",
            "job_text": j.job_text or "",
            "ai_score": None,
            "ai_explanation": None,
            "ai_strengths": None,
            "ai_weaknesses": None,
            "distance_km": None,
            "drive_time_car_min": None,
            "drive_time_transit_min": None,
        },
    }


# ══════════════════════════════════════════════════════════════
# Debug-Endpoints
# ══════════════════════════════════════════════════════════════

@router.get("/debug/match-count")
async def debug_match_count(db: AsyncSession = Depends(get_db)):
    """Match-Statistiken fuer Claude-Matches."""
    today = date.today()
    today_start = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
    week_start = today_start - timedelta(days=7)

    # Total Claude-Matches
    total = await db.execute(
        select(func.count()).where(Match.matching_method == "claude_match")
    )
    total_count = total.scalar() or 0

    # By empfehlung
    empf_query = await db.execute(
        select(Match.empfehlung, func.count())
        .where(Match.matching_method == "claude_match")
        .group_by(Match.empfehlung)
    )
    by_empfehlung = {row[0] or "none": row[1] for row in empf_query.all()}

    # By status
    status_query = await db.execute(
        select(Match.status, func.count())
        .where(Match.matching_method == "claude_match")
        .group_by(Match.status)
    )
    by_status = {row[0].value if hasattr(row[0], "value") else str(row[0]): row[1] for row in status_query.all()}

    # Wow
    wow = await db.execute(
        select(func.count()).where(
            and_(Match.matching_method == "claude_match", Match.wow_faktor == True)
        )
    )
    wow_count = wow.scalar() or 0

    # Today
    today_q = await db.execute(
        select(func.count()).where(
            and_(Match.matching_method == "claude_match", Match.created_at >= today_start)
        )
    )
    today_count = today_q.scalar() or 0

    # This week
    week_q = await db.execute(
        select(func.count()).where(
            and_(Match.matching_method == "claude_match", Match.created_at >= week_start)
        )
    )
    week_count = week_q.scalar() or 0

    # Proximity Matches
    prox = await db.execute(
        select(func.count()).where(Match.matching_method == "proximity_match")
    )
    prox_count = prox.scalar() or 0

    return {
        "total_claude_matches": total_count,
        "by_empfehlung": by_empfehlung,
        "by_status": by_status,
        "wow_matches": wow_count,
        "today": today_count,
        "this_week": week_count,
        "proximity_matches": prox_count,
    }


@router.get("/debug/stufe-0-preview")
async def debug_stufe_0_preview(db: AsyncSession = Depends(get_db)):
    """Zeigt was Stufe 0 liefern WUERDE ohne Claude-Calls (Dry-Run)."""

    # Aktive Kandidaten
    cand_count = await db.execute(
        select(func.count()).where(
            and_(
                Candidate.deleted_at.is_(None),
                Candidate.hidden == False,
                Candidate.classification_data.isnot(None),
            )
        )
    )
    total_candidates = cand_count.scalar() or 0

    # Kandidaten mit Daten fuer Claude
    cand_with_data = await db.execute(
        select(func.count()).where(
            and_(
                Candidate.deleted_at.is_(None),
                Candidate.hidden == False,
                Candidate.classification_data.isnot(None),
                (Candidate.work_history.isnot(None)) | (Candidate.cv_text.isnot(None)),
            )
        )
    )
    candidates_with_data = cand_with_data.scalar() or 0

    # Aktive Jobs
    job_count = await db.execute(
        select(func.count()).where(
            and_(
                Job.deleted_at.is_(None),
                Job.quality_score.in_(["high", "medium"]),
                Job.classification_data.isnot(None),
                (Job.expires_at.is_(None)) | (Job.expires_at > func.now()),
            )
        )
    )
    active_jobs = job_count.scalar() or 0

    # Jobs mit job_text
    jobs_with_text = await db.execute(
        select(func.count()).where(
            and_(
                Job.deleted_at.is_(None),
                Job.quality_score.in_(["high", "medium"]),
                Job.job_text.isnot(None),
                func.length(Job.job_text) > 50,
                (Job.expires_at.is_(None)) | (Job.expires_at > func.now()),
            )
        )
    )
    jobs_with_text_count = jobs_with_text.scalar() or 0

    # Existierende Matches (werden uebersprungen)
    existing = await db.execute(select(func.count()).select_from(Match))
    existing_matches = existing.scalar() or 0

    return {
        "total_candidates": total_candidates,
        "candidates_with_data": candidates_with_data,
        "candidates_without_data": total_candidates - candidates_with_data,
        "active_jobs": active_jobs,
        "jobs_with_text": jobs_with_text_count,
        "jobs_without_text": active_jobs - jobs_with_text_count,
        "existing_matches": existing_matches,
        "potential_pairs": total_candidates * active_jobs,
        "note": "Tatsaechliche Paare nach Distanzfilter sind deutlich weniger",
    }


@router.get("/debug/job-health")
async def debug_job_health(db: AsyncSession = Depends(get_db)):
    """Gesundheitscheck der Job-Daten."""
    now = datetime.now(timezone.utc)

    total = await db.execute(
        select(func.count()).where(Job.deleted_at.is_(None))
    )
    total_count = total.scalar() or 0

    # Aktiv (nicht abgelaufen)
    active = await db.execute(
        select(func.count()).where(
            and_(
                Job.deleted_at.is_(None),
                (Job.expires_at.is_(None)) | (Job.expires_at > func.now()),
            )
        )
    )
    active_count = active.scalar() or 0

    # Ohne job_text
    no_text = await db.execute(
        select(func.count()).where(
            and_(
                Job.deleted_at.is_(None),
                (Job.job_text.is_(None)) | (func.length(func.coalesce(Job.job_text, "")) <= 50),
            )
        )
    )
    no_text_count = no_text.scalar() or 0

    # Ohne Koordinaten
    no_coords = await db.execute(
        select(func.count()).where(
            and_(Job.deleted_at.is_(None), Job.location_coords.is_(None))
        )
    )
    no_coords_count = no_coords.scalar() or 0

    # Ohne Classification
    no_class = await db.execute(
        select(func.count()).where(
            and_(Job.deleted_at.is_(None), Job.classification_data.is_(None))
        )
    )
    no_class_count = no_class.scalar() or 0

    # By city (Top 10)
    city_query = await db.execute(
        select(
            func.coalesce(Job.city, Job.work_location_city, "Unbekannt").label("city"),
            func.count().label("cnt"),
        )
        .where(
            and_(
                Job.deleted_at.is_(None),
                (Job.expires_at.is_(None)) | (Job.expires_at > func.now()),
            )
        )
        .group_by("city")
        .order_by(text("cnt DESC"))
        .limit(10)
    )
    by_city = {row[0]: row[1] for row in city_query.all()}

    return {
        "total_jobs": total_count,
        "active_jobs": active_count,
        "expired_jobs": total_count - active_count,
        "no_job_text": no_text_count,
        "no_coordinates": no_coords_count,
        "no_classification": no_class_count,
        "by_city_top10": by_city,
    }


@router.get("/debug/candidate-health")
async def debug_candidate_health(db: AsyncSession = Depends(get_db)):
    """Gesundheitscheck der Kandidaten-Daten."""
    total = await db.execute(
        select(func.count()).where(
            and_(Candidate.deleted_at.is_(None), Candidate.hidden == False)
        )
    )
    total_count = total.scalar() or 0

    # Ohne work_history UND ohne cv_text
    no_data = await db.execute(
        select(func.count()).where(
            and_(
                Candidate.deleted_at.is_(None),
                Candidate.hidden == False,
                Candidate.work_history.is_(None),
                Candidate.cv_text.is_(None),
            )
        )
    )
    no_data_count = no_data.scalar() or 0

    # Ohne Koordinaten
    no_coords = await db.execute(
        select(func.count()).where(
            and_(
                Candidate.deleted_at.is_(None),
                Candidate.hidden == False,
                Candidate.address_coords.is_(None),
            )
        )
    )
    no_coords_count = no_coords.scalar() or 0

    # Ohne Classification
    no_class = await db.execute(
        select(func.count()).where(
            and_(
                Candidate.deleted_at.is_(None),
                Candidate.hidden == False,
                Candidate.classification_data.is_(None),
            )
        )
    )
    no_class_count = no_class.scalar() or 0

    # By city (Top 10)
    city_query = await db.execute(
        select(
            func.coalesce(Candidate.city, "Unbekannt").label("city"),
            func.count().label("cnt"),
        )
        .where(and_(Candidate.deleted_at.is_(None), Candidate.hidden == False))
        .group_by("city")
        .order_by(text("cnt DESC"))
        .limit(10)
    )
    by_city = {row[0]: row[1] for row in city_query.all()}

    # By role (Top 10)
    role_query = await db.execute(
        select(
            func.coalesce(Candidate.hotlist_job_title, "Unklassifiziert").label("role"),
            func.count().label("cnt"),
        )
        .where(and_(Candidate.deleted_at.is_(None), Candidate.hidden == False))
        .group_by("role")
        .order_by(text("cnt DESC"))
        .limit(10)
    )
    by_role = {row[0]: row[1] for row in role_query.all()}

    return {
        "total_candidates": total_count,
        "no_work_data": no_data_count,
        "no_coordinates": no_coords_count,
        "no_classification": no_class_count,
        "by_city_top10": by_city,
        "by_role_top10": by_role,
    }


@router.get("/debug/match/{match_id}")
async def debug_match_detail(
    match_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Detail-Ansicht eines einzelnen Matches mit Claude-Input/Output."""
    match = await db.get(Match, match_id)
    if not match:
        raise HTTPException(status_code=404, detail="Match nicht gefunden")

    return {
        "match_id": str(match.id),
        "candidate_id": str(match.candidate_id) if match.candidate_id else None,
        "job_id": str(match.job_id) if match.job_id else None,
        "matching_method": match.matching_method,
        "status": match.status.value if hasattr(match.status, "value") else str(match.status),
        "v2_score": match.v2_score,
        "ai_score": match.ai_score,
        "ai_explanation": match.ai_explanation,
        "ai_strengths": match.ai_strengths,
        "ai_weaknesses": match.ai_weaknesses,
        "empfehlung": match.empfehlung,
        "wow_faktor": match.wow_faktor,
        "wow_grund": match.wow_grund,
        "distance_km": match.distance_km,
        "drive_time_car_min": match.drive_time_car_min,
        "drive_time_transit_min": match.drive_time_transit_min,
        "user_feedback": match.user_feedback,
        "feedback_note": match.feedback_note,
        "v2_score_breakdown": match.v2_score_breakdown,
        "created_at": match.created_at.isoformat() if match.created_at else None,
        "quick_reason": match.quick_reason,
    }


@router.get("/debug/cost-report")
async def debug_cost_report(db: AsyncSession = Depends(get_db)):
    """API-Kosten Uebersicht basierend auf gespeicherten Token-Counts."""
    from app.services.claude_matching_service import get_status

    today = date.today()
    today_start = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
    week_start = today_start - timedelta(days=7)
    month_start = today_start.replace(day=1)

    # Kosten aus v2_score_breakdown aggregieren (tokens_in/tokens_out)
    # Haiku Preise: $0.80/1M input, $4.00/1M output
    async def _cost_for_period(start: datetime) -> dict:
        query = await db.execute(
            select(
                func.count().label("calls"),
                func.sum(
                    func.cast(
                        Match.v2_score_breakdown["tokens_in"].astext,
                        sa.Integer,
                    )
                ).label("tokens_in"),
                func.sum(
                    func.cast(
                        Match.v2_score_breakdown["tokens_out"].astext,
                        sa.Integer,
                    )
                ).label("tokens_out"),
            ).where(
                and_(
                    Match.matching_method == "claude_match",
                    Match.created_at >= start,
                )
            )
        )
        row = query.one()
        t_in = row.tokens_in or 0
        t_out = row.tokens_out or 0
        cost = (t_in * 0.80 + t_out * 4.0) / 1_000_000
        return {
            "matches": row.calls or 0,
            "tokens_in": t_in,
            "tokens_out": t_out,
            "cost_usd": round(cost, 4),
        }

    status = get_status()

    # Einfache Zaehlung statt komplexer JSONB-Aggregation
    today_count = await db.execute(
        select(func.count()).where(
            and_(Match.matching_method == "claude_match", Match.created_at >= today_start)
        )
    )
    week_count = await db.execute(
        select(func.count()).where(
            and_(Match.matching_method == "claude_match", Match.created_at >= week_start)
        )
    )
    month_count = await db.execute(
        select(func.count()).where(
            and_(Match.matching_method == "claude_match", Match.created_at >= month_start)
        )
    )

    return {
        "today_matches": today_count.scalar() or 0,
        "week_matches": week_count.scalar() or 0,
        "month_matches": month_count.scalar() or 0,
        "last_run": status.get("last_run"),
        "last_run_result": status.get("last_run_result"),
    }


# ══════════════════════════════════════════════════════════════
# Regional Insights (Phase 6)
# ══════════════════════════════════════════════════════════════

@router.get("/claude-match/regional-insights")
async def get_regional_insights(db: AsyncSession = Depends(get_db)):
    """Regionale Uebersicht: Kandidaten vs. Jobs pro Stadt."""
    # Kandidaten pro Stadt
    cand_query = (
        select(Candidate.city, func.count(Candidate.id))
        .where(
            Candidate.deleted_at.is_(None),
            Candidate.hidden == False,
            Candidate.city.isnot(None),
            Candidate.city != "",
        )
        .group_by(Candidate.city)
    )

    # Aktive Jobs pro Stadt
    job_query = (
        select(Job.city, func.count(Job.id))
        .where(
            Job.deleted_at.is_(None),
            (Job.expires_at.is_(None)) | (Job.expires_at > func.now()),
            Job.city.isnot(None),
            Job.city != "",
        )
        .group_by(Job.city)
    )

    cand_result = await db.execute(cand_query)
    job_result = await db.execute(job_query)

    cand_by_city = {row[0]: row[1] for row in cand_result}
    jobs_by_city = {row[0]: row[1] for row in job_result}

    all_cities = set(cand_by_city.keys()) | set(jobs_by_city.keys())
    regions = []
    for city in sorted(
        all_cities,
        key=lambda c: cand_by_city.get(c, 0) + jobs_by_city.get(c, 0),
        reverse=True,
    ):
        c = cand_by_city.get(city, 0)
        j = jobs_by_city.get(city, 0)
        if c > 5 and j > 3:
            status = "gut_abgedeckt"
        elif c > 0 and j > 0:
            status = "ausbaufaehig"
        elif j > 3 and c == 0:
            status = "sourcing_chance"
        else:
            status = "keine_abdeckung"
        regions.append({
            "city": city,
            "candidate_count": c,
            "job_count": j,
            "status": status,
        })

    return {"regions": regions[:20]}


# ══════════════════════════════════════════════════════════════
# Detailliertes Feedback (Phase 6)
# ══════════════════════════════════════════════════════════════

@router.post("/claude-match/{match_id}/detailed-feedback")
async def submit_detailed_feedback(
    match_id: UUID,
    body: dict,
    db: AsyncSession = Depends(get_db),
):
    """Detailliertes Feedback fuer Matching-Verbesserung."""
    match = await db.get(Match, match_id)
    if not match:
        raise HTTPException(status_code=404, detail="Match nicht gefunden")

    match.user_feedback = body.get("feedback", "neutral")
    match.feedback_note = body.get("note", "")
    match.rejection_reason = body.get("rejection_reason")
    match.feedback_at = datetime.now(timezone.utc)

    await db.commit()

    # Feedback-Statistiken aggregieren
    stats_query = (
        select(Match.user_feedback, func.count(Match.id))
        .where(
            Match.matching_method == "claude_match",
            Match.user_feedback.isnot(None),
        )
        .group_by(Match.user_feedback)
    )
    stats = await db.execute(stats_query)
    feedback_stats = {row[0]: row[1] for row in stats}

    return {"success": True, "feedback_stats": feedback_stats}
