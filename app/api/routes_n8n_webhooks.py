"""n8n Webhook-Routen — Inbound-API fuer n8n-Automatisierung."""

import logging
from dataclasses import dataclass
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.services.ats_call_note_service import ATSCallNoteService
from app.services.ats_pipeline_service import ATSPipelineService
from app.services.ats_todo_service import ATSTodoService
from app.services.interaction_analyzer_service import InteractionAnalyzerService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/n8n", tags=["n8n Webhooks"])


# ── Auth ─────────────────────────────────────────

async def verify_n8n_token(authorization: str = Header(...)):
    """Prueft den n8n API-Token."""
    if not settings.n8n_api_token:
        # Wenn kein Token konfiguriert, alles erlauben (Development)
        return
    expected = f"Bearer {settings.n8n_api_token}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="Ungueltiger n8n-Token")


# ── Pydantic Schemas ─────────────────────────────

class PipelineMoveRequest(BaseModel):
    entry_id: UUID
    stage: str


class TodoCreateRequest(BaseModel):
    title: str
    description: Optional[str] = None
    priority: Optional[str] = "wichtig"
    due_date: Optional[str] = None  # ISO date, z.B. "2025-06-15"
    company_id: Optional[UUID] = None
    candidate_id: Optional[UUID] = None
    ats_job_id: Optional[UUID] = None


class ActivityLogRequest(BaseModel):
    activity_type: str
    description: str
    ats_job_id: Optional[UUID] = None
    company_id: Optional[UUID] = None
    candidate_id: Optional[UUID] = None
    metadata: Optional[dict] = None


class EmailReceivedRequest(BaseModel):
    from_email: str
    to_email: str
    subject: str
    body: Optional[str] = None
    candidate_id: Optional[UUID] = None
    ats_job_id: Optional[UUID] = None


class CallNoteCreateRequest(BaseModel):
    call_type: str
    summary: str
    company_id: Optional[UUID] = None
    candidate_id: Optional[UUID] = None
    ats_job_id: Optional[UUID] = None
    action_items: Optional[list] = None


class EmailSentRequest(BaseModel):
    to_email: str
    from_email: str
    subject: str
    body: Optional[str] = None
    candidate_id: Optional[UUID] = None
    ats_job_id: Optional[UUID] = None


class CandidateSourceRequest(BaseModel):
    candidate_id: UUID
    source: str


class CandidateWillingnessRequest(BaseModel):
    candidate_id: UUID
    willingness: str  # "ja", "nein", "unbekannt"


class CandidateNotesRequest(BaseModel):
    candidate_id: UUID
    notes: str


class CandidateLastContactRequest(BaseModel):
    candidate_id: UUID
    contact_date: Optional[str] = None  # ISO-Format, None = jetzt


# ── Endpoints ────────────────────────────────────

@router.post("/pipeline/move")
async def n8n_pipeline_move(
    data: PipelineMoveRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_n8n_token),
):
    """n8n: Pipeline-Stage automatisch aendern."""
    service = ATSPipelineService(db)
    entry = await service.move_stage(data.entry_id, data.stage)
    if not entry:
        raise HTTPException(status_code=404, detail="Pipeline-Eintrag nicht gefunden")
    await db.commit()
    logger.info(f"n8n Pipeline Move: {data.entry_id} -> {data.stage}")
    return {"success": True, "entry_id": str(entry.id), "stage": entry.stage.value, "candidate_id": str(entry.candidate_id) if entry.candidate_id else None}


@router.post("/todo/create")
async def n8n_todo_create(
    data: TodoCreateRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_n8n_token),
):
    """n8n: Todo automatisch erstellen."""
    from datetime import date as date_type

    create_data = data.model_dump(exclude_unset=True)

    # due_date von String zu date konvertieren
    if "due_date" in create_data and create_data["due_date"]:
        try:
            create_data["due_date"] = date_type.fromisoformat(create_data["due_date"])
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="Ungueltiges due_date Format (ISO 8601 erwartet, z.B. 2025-06-15)")

    service = ATSTodoService(db)
    todo = await service.create_todo(**create_data)
    await db.commit()
    logger.info(f"n8n Todo Created: {todo.id} - {todo.title[:50]}")
    return {"success": True, "todo_id": str(todo.id), "title": todo.title}


@router.post("/activity/log")
async def n8n_activity_log(
    data: ActivityLogRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_n8n_token),
):
    """n8n: Aktivitaet loggen."""
    from app.models.ats_activity import ATSActivity, ActivityType

    try:
        activity_type = ActivityType(data.activity_type)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Ungueltiger activity_type: {data.activity_type}")

    activity = ATSActivity(
        activity_type=activity_type,
        description=data.description,
        ats_job_id=data.ats_job_id,
        company_id=data.company_id,
        candidate_id=data.candidate_id,
        metadata_json=data.metadata,
    )
    db.add(activity)
    await db.commit()
    logger.info(f"n8n Activity Logged: {data.activity_type}")
    return {"success": True, "activity_id": str(activity.id)}


@router.post("/email/received")
async def n8n_email_received(
    data: EmailReceivedRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_n8n_token),
):
    """n8n: Eingehende E-Mail verarbeiten (fuer spaetere E-Mail-Pipeline)."""
    from app.models.ats_activity import ATSActivity, ActivityType

    activity = ATSActivity(
        activity_type=ActivityType.EMAIL_RECEIVED,
        description=f"E-Mail empfangen: {data.subject[:80]}",
        ats_job_id=data.ats_job_id,
        candidate_id=data.candidate_id,
        metadata_json={
            "from_email": data.from_email,
            "to_email": data.to_email,
            "subject": data.subject,
        },
    )
    db.add(activity)

    # last_contact automatisch aktualisieren
    if data.candidate_id:
        from app.models.candidate import Candidate
        candidate = await db.get(Candidate, data.candidate_id)
        if candidate:
            from datetime import datetime, timezone
            candidate.last_contact = datetime.now(timezone.utc)
            candidate.updated_at = datetime.now(timezone.utc)

    await db.commit()
    logger.info(f"n8n Email Received: {data.from_email} -> {data.subject[:50]}")
    return {"success": True, "activity_id": str(activity.id)}


@router.post("/email/sent")
async def n8n_email_sent(
    data: EmailSentRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_n8n_token),
):
    """n8n: Gesendete E-Mail verarbeiten. Aktualisiert last_contact."""
    from app.models.ats_activity import ATSActivity, ActivityType

    activity = ATSActivity(
        activity_type=ActivityType.EMAIL_SENT,
        description=f"E-Mail gesendet: {data.subject[:80]}",
        ats_job_id=data.ats_job_id,
        candidate_id=data.candidate_id,
        metadata_json={
            "from_email": data.from_email,
            "to_email": data.to_email,
            "subject": data.subject,
        },
    )
    db.add(activity)

    # last_contact automatisch aktualisieren
    if data.candidate_id:
        from app.models.candidate import Candidate
        from datetime import datetime, timezone
        candidate = await db.get(Candidate, data.candidate_id)
        if candidate:
            candidate.last_contact = datetime.now(timezone.utc)
            candidate.updated_at = datetime.now(timezone.utc)

    await db.commit()
    logger.info(f"n8n Email Sent: {data.to_email} -> {data.subject[:50]}")
    return {"success": True, "activity_id": str(activity.id)}


@router.post("/call-note/create")
async def n8n_call_note_create(
    data: CallNoteCreateRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_n8n_token),
):
    """n8n: Call-Note aus Transkription erstellen."""
    service = ATSCallNoteService(db)
    note = await service.create_call_note(**data.model_dump(exclude_unset=True))

    # Wenn Action Items vorhanden, automatisch Todos erstellen
    if data.action_items:
        await service.auto_create_todos(note.id)

    await db.commit()
    logger.info(f"n8n CallNote Created: {note.id}")
    return {"success": True, "call_note_id": str(note.id)}


# ── Phase 2: Recruiting-Daten Endpunkte (n8n-kompatibel) ──


@router.post("/candidate/source")
async def n8n_set_candidate_source(
    data: CandidateSourceRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_n8n_token),
):
    """n8n: Quelle eines Kandidaten setzen (StepStone, LinkedIn, etc.)."""
    from app.models.candidate import Candidate
    from datetime import datetime, timezone

    candidate = await db.get(Candidate, data.candidate_id)
    if not candidate:
        raise HTTPException(status_code=404, detail="Kandidat nicht gefunden")

    candidate.source = data.source
    candidate.updated_at = datetime.now(timezone.utc)
    await db.commit()

    logger.info(f"n8n Source Set: {data.candidate_id} -> {data.source}")
    return {"success": True, "candidate_id": str(data.candidate_id), "source": data.source}


@router.post("/candidate/willingness")
async def n8n_set_candidate_willingness(
    data: CandidateWillingnessRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_n8n_token),
):
    """n8n: Wechselbereitschaft setzen (ja/nein/unbekannt). Aktualisiert auch last_contact."""
    from app.models.candidate import Candidate
    from datetime import datetime, timezone

    candidate = await db.get(Candidate, data.candidate_id)
    if not candidate:
        raise HTTPException(status_code=404, detail="Kandidat nicht gefunden")

    if data.willingness not in ("ja", "nein", "unbekannt"):
        raise HTTPException(status_code=400, detail="Wechselbereitschaft muss 'ja', 'nein' oder 'unbekannt' sein")

    candidate.willingness_to_change = data.willingness
    candidate.last_contact = datetime.now(timezone.utc)
    candidate.updated_at = datetime.now(timezone.utc)
    await db.commit()

    logger.info(f"n8n Willingness Set: {data.candidate_id} -> {data.willingness}")
    return {"success": True, "candidate_id": str(data.candidate_id), "willingness_to_change": data.willingness}


@router.post("/candidate/notes")
async def n8n_set_candidate_notes(
    data: CandidateNotesRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_n8n_token),
):
    """n8n: Notizen setzen/aktualisieren (Gespraeche, Wechselmotivation etc.). Aktualisiert auch last_contact."""
    from app.models.candidate import Candidate
    from datetime import datetime, timezone

    candidate = await db.get(Candidate, data.candidate_id)
    if not candidate:
        raise HTTPException(status_code=404, detail="Kandidat nicht gefunden")

    # Notizen anhaengen statt ueberschreiben (wenn bereits vorhanden)
    if candidate.candidate_notes and data.notes:
        timestamp = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M")
        candidate.candidate_notes = candidate.candidate_notes + f"\n\n--- {timestamp} ---\n{data.notes}"
    else:
        candidate.candidate_notes = data.notes

    candidate.last_contact = datetime.now(timezone.utc)
    candidate.updated_at = datetime.now(timezone.utc)
    await db.commit()

    logger.info(f"n8n Notes Updated: {data.candidate_id}")
    return {"success": True, "candidate_id": str(data.candidate_id)}


@router.post("/candidate/last-contact")
async def n8n_set_candidate_last_contact(
    data: CandidateLastContactRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_n8n_token),
):
    """n8n: Letzter-Kontakt-Datum manuell setzen. Ohne contact_date = jetzt."""
    from app.models.candidate import Candidate
    from datetime import datetime, timezone

    candidate = await db.get(Candidate, data.candidate_id)
    if not candidate:
        raise HTTPException(status_code=404, detail="Kandidat nicht gefunden")

    if data.contact_date:
        try:
            candidate.last_contact = datetime.fromisoformat(data.contact_date)
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="Ungueltiges Datumsformat (ISO 8601 erwartet)")
    else:
        candidate.last_contact = datetime.now(timezone.utc)

    candidate.updated_at = datetime.now(timezone.utc)
    await db.commit()

    logger.info(f"n8n Last Contact Set: {data.candidate_id}")
    return {"success": True, "candidate_id": str(data.candidate_id), "last_contact": candidate.last_contact.isoformat()}


# ── Phase 2.1: KI-Analyse + Query-Endpunkte ─────


class ProcessInteractionRequest(BaseModel):
    candidate_id: UUID
    text: str = Field(..., min_length=10, description="Transkription oder E-Mail-Text")
    interaction_type: str = Field(default="call", description="call, email_received, email_sent")


class MatchFeedbackRequest(BaseModel):
    match_id: UUID
    feedback: str = Field(..., description="good, bad_distance, bad_skills, bad_seniority, maybe")
    note: Optional[str] = None


class ProfileTriggerRequest(BaseModel):
    candidate_id: UUID


@router.post("/candidate/process-interaction")
async def n8n_process_interaction(
    data: ProcessInteractionRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_n8n_token),
):
    """n8n: Interaktion (Telefonat/E-Mail) per KI analysieren und Kandidaten-Felder aktualisieren."""
    if data.interaction_type not in ("call", "email_received", "email_sent"):
        raise HTTPException(status_code=400, detail="interaction_type muss 'call', 'email_received' oder 'email_sent' sein")

    analyzer = InteractionAnalyzerService(db)
    result = await analyzer.analyze_interaction(
        candidate_id=data.candidate_id,
        text=data.text,
        interaction_type=data.interaction_type,
    )

    if not result.get("success"):
        raise HTTPException(status_code=422, detail=result.get("error", "Analyse fehlgeschlagen"))

    await db.commit()
    logger.info(f"n8n Process Interaction: {data.candidate_id} ({data.interaction_type}) -> {result.get('fields_updated', [])}")
    return result


@router.get("/candidates/stale")
async def n8n_get_stale_candidates(
    days: int = Query(default=30, ge=1, le=365, description="Tage ohne Kontakt"),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_n8n_token),
):
    """n8n: Kandidaten die seit X Tagen keinen Kontakt hatten (fuer Follow-up Workflows)."""
    from datetime import datetime, timezone, timedelta
    from sqlalchemy import select, or_
    from app.models.candidate import Candidate

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    result = await db.execute(
        select(Candidate)
        .where(
            or_(
                Candidate.last_contact < cutoff,
                Candidate.last_contact.is_(None),
            )
        )
        .where(Candidate.willingness_to_change != "nein")  # Nur nicht-ablehnende
        .order_by(Candidate.last_contact.asc().nullsfirst())
        .limit(limit)
    )
    candidates = result.scalars().all()

    return {
        "success": True,
        "count": len(candidates),
        "days_threshold": days,
        "candidates": [
            {
                "id": str(c.id),
                "name": c.full_name,
                "current_position": c.current_position,
                "current_company": c.current_company,
                "last_contact": c.last_contact.isoformat() if c.last_contact else None,
                "willingness_to_change": c.willingness_to_change,
                "source": c.source,
            }
            for c in candidates
        ],
    }


@router.get("/candidates/willing")
async def n8n_get_willing_candidates(
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_n8n_token),
):
    """n8n: Alle wechselwilligen Kandidaten (fuer aktive Suche/Matching)."""
    from sqlalchemy import select
    from app.models.candidate import Candidate

    result = await db.execute(
        select(Candidate)
        .where(Candidate.willingness_to_change == "ja")
        .order_by(Candidate.last_contact.desc().nullslast())
        .limit(limit)
    )
    candidates = result.scalars().all()

    return {
        "success": True,
        "count": len(candidates),
        "candidates": [
            {
                "id": str(c.id),
                "name": c.full_name,
                "current_position": c.current_position,
                "current_company": c.current_company,
                "salary": c.salary,
                "notice_period": c.notice_period,
                "erp": c.erp,
                "last_contact": c.last_contact.isoformat() if c.last_contact else None,
                "source": c.source,
                "v2_seniority_level": c.v2_seniority_level,
            }
            for c in candidates
        ],
    }


@router.post("/match/feedback")
async def n8n_match_feedback(
    data: MatchFeedbackRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_n8n_token),
):
    """n8n: Match-Feedback loggen — gleiche Werte wie im MT.

    Feedback-Werte:
    - good: Guter Match
    - bad_distance: Distanz passt nicht
    - bad_skills: Taetigkeiten passen nicht
    - bad_seniority: Seniority passt nicht
    - maybe: Vielleicht / neutral
    """
    from app.models.match import Match, MatchStatus
    from app.services.matching_learning_service import MatchingLearningService
    from datetime import datetime, timezone

    valid_feedback = ("good", "bad_distance", "bad_skills", "bad_seniority", "maybe")
    if data.feedback not in valid_feedback:
        raise HTTPException(
            status_code=400,
            detail=f"feedback muss einer von {valid_feedback} sein",
        )

    # Match laden
    match = await db.get(Match, data.match_id)
    if not match:
        raise HTTPException(status_code=404, detail="Match nicht gefunden")

    # Outcome bestimmen (gleiche Logik wie Match Center)
    is_bad = data.feedback.startswith("bad_")
    outcome = "bad" if is_bad else ("good" if data.feedback == "good" else "neutral")
    rejection_reason = data.feedback if is_bad else None

    # 1. Feedback in Match speichern
    match.user_feedback = data.feedback
    match.feedback_note = data.note
    match.feedback_at = datetime.now(timezone.utc)
    match.rejection_reason = rejection_reason

    # 2. Bei negativem Feedback: Status auf REJECTED
    if is_bad:
        match.status = MatchStatus.REJECTED

    await db.flush()

    # 3. Job-Kategorie ermitteln (fuer pro-Kategorie-Lernen)
    job_category = None
    if match.job_id:
        from app.models.job import Job
        job = await db.get(Job, match.job_id)
        if job:
            job_category = job.hotlist_job_title or job.position

    # 4. Learning Service aufrufen
    learning_info = {}
    try:
        learning = MatchingLearningService(db)
        lr = await learning.record_feedback(
            match_id=data.match_id,
            outcome=outcome,
            note=data.note,
            source="user_feedback",
            rejection_reason=rejection_reason,
            job_category=job_category,
        )
        learning_info = {
            "weights_adjusted": lr.weights_adjusted,
            "learning_stage": lr.learning_stage if hasattr(lr, "learning_stage") else None,
        }
    except Exception as le:
        logger.warning(f"n8n Learning Service Fehler (Feedback trotzdem gespeichert): {le}")

    # 5. last_contact aktualisieren
    if match.candidate_id:
        from app.models.candidate import Candidate
        candidate = await db.get(Candidate, match.candidate_id)
        if candidate:
            candidate.last_contact = datetime.now(timezone.utc)
            candidate.updated_at = datetime.now(timezone.utc)

    await db.commit()
    logger.info(f"n8n Match Feedback: match={data.match_id}, feedback={data.feedback}, outcome={outcome}")
    return {
        "success": True,
        "match_id": str(data.match_id),
        "feedback": data.feedback,
        "outcome": outcome,
        "rejected": is_bad,
        "learning": learning_info,
    }


@router.post("/candidate/profile-trigger")
async def n8n_profile_trigger(
    data: ProfileTriggerRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_n8n_token),
):
    """n8n: v2-Profiling fuer einen einzelnen Kandidaten triggern."""
    from app.services.profile_engine_service import ProfileEngineService

    service = ProfileEngineService(db)
    try:
        profile = await service.create_candidate_profile(data.candidate_id)
        await db.commit()
        logger.info(f"n8n Profile Trigger: {data.candidate_id} -> Level {profile.seniority_level}")
        return {
            "success": True,
            "candidate_id": str(data.candidate_id),
            "seniority_level": profile.seniority_level,
            "years_experience": profile.years_experience,
            "career_trajectory": profile.career_trajectory,
            "current_role_summary": profile.current_role_summary,
        }
    except Exception as e:
        logger.error(f"n8n Profile Trigger Fehler: {e}")
        raise HTTPException(status_code=422, detail=str(e))


# ── Phase 2.1: Kandidaten-Antwort-System ──────────


class JobResponseRequest(BaseModel):
    candidate_id: UUID
    ats_job_id: Optional[UUID] = None
    response_type: str  # rejection, needs_info, wants_call, not_looking, already_presented, already_applied, follow_up_later, interested
    follow_up_date: Optional[str] = None  # ISO date, fuer follow_up_later / not_looking
    note: Optional[str] = None
    email_subject: Optional[str] = None
    company_name: Optional[str] = None  # Fuer already_presented / already_applied


class PresentedAtRequest(BaseModel):
    candidate_id: UUID
    company_name: str
    entry_type: str = "presented"  # "presented", "applied" oder "pdl"
    note: Optional[str] = None


@dataclass
class ResponseTypeConfig:
    """Konfiguration fuer einen Kandidaten-Antwort-Typ."""
    label: str
    willingness: str | None = None  # "ja" / "nein" / "unbekannt" / None
    todo_title: str | None = None
    todo_priority: str = "wichtig"
    use_follow_up_date: bool = False  # due_date aus Request nutzen
    default_follow_up_days: int = 90  # Default Follow-up wenn kein Datum angegeben
    move_to_feedback: bool = False  # Pipeline nach "feedback" verschieben
    add_note: bool = False  # note an candidate_notes anhaengen
    add_presented_at: bool = False  # Unternehmen in presented_at_companies eintragen
    notify_whatsapp: bool = False  # WhatsApp-Benachrichtigung an Recruiter


RESPONSE_TYPE_CONFIGS: dict[str, ResponseTypeConfig] = {
    "rejection": ResponseTypeConfig(
        label="Absage",
        willingness="nein",
        todo_title="Follow-up: Kandidat nochmals kontaktieren (nach Absage)",
        todo_priority="mittelmaessig",
        use_follow_up_date=True,
        default_follow_up_days=90,
        add_note=True,
    ),
    "needs_info": ResponseTypeConfig(
        label="Braucht mehr Infos",
        todo_title="Weitere Infos senden + Terminvorschlag fuer Telefonat",
        todo_priority="dringend",
        notify_whatsapp=True,
    ),
    "wants_call": ResponseTypeConfig(
        label="Moechte Telefonat",
        todo_title="Kandidat anrufen",
        todo_priority="sehr_dringend",
        notify_whatsapp=True,
    ),
    "not_looking": ResponseTypeConfig(
        label="Aktuell nicht auf Suche",
        willingness="nein",
        todo_title="Follow-up: Kandidat nochmals kontaktieren",
        todo_priority="mittelmaessig",
        use_follow_up_date=True,
        default_follow_up_days=90,
        add_note=True,
    ),
    "already_presented": ResponseTypeConfig(
        label="Bereits vorgestellt (anderer Recruiter)",
        add_note=True,
        add_presented_at=True,
    ),
    "already_applied": ResponseTypeConfig(
        label="Eigenstaendige Bewerbung",
        add_note=True,
        add_presented_at=True,
    ),
    "follow_up_later": ResponseTypeConfig(
        label="Spaeter kontaktieren",
        willingness="unbekannt",
        todo_title="Follow-up: Kandidat kontaktieren",
        todo_priority="wichtig",
        use_follow_up_date=True,
        default_follow_up_days=90,
        add_note=True,
    ),
    "interested": ResponseTypeConfig(
        label="Interessiert",
        willingness="ja",
        todo_title="Kandidat kontaktieren — Vorstellung vorbereiten",
        todo_priority="sehr_dringend",
        move_to_feedback=True,
        notify_whatsapp=True,
    ),
}


@router.post("/candidate/job-response")
async def n8n_candidate_job_response(
    data: JobResponseRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_n8n_token),
):
    """n8n: Kandidaten-Antwort auf Job-Vorschlag verarbeiten.

    Klassifizierte Antwort von n8n. Fuehrt automatisch alle passenden
    Aktionen aus (Willingness, Todos, Pipeline, Notes, Activity, WhatsApp).
    WICHTIG: n8n soll KEINE persoenlichen Daten an LLMs senden — immer candidate_number als Referenz.
    """
    from datetime import date as date_type, datetime, timezone, timedelta
    from app.models.candidate import Candidate
    from app.models.ats_activity import ATSActivity, ActivityType

    # 1. Config laden
    config = RESPONSE_TYPE_CONFIGS.get(data.response_type)
    if not config:
        valid = list(RESPONSE_TYPE_CONFIGS.keys())
        raise HTTPException(
            status_code=400,
            detail=f"Ungueltiger response_type: {data.response_type}. Erlaubt: {valid}",
        )

    # 2. follow_up_date validieren
    follow_up_date: date_type | None = None
    if config.use_follow_up_date:
        if data.follow_up_date:
            try:
                follow_up_date = date_type.fromisoformat(data.follow_up_date)
            except (ValueError, TypeError):
                raise HTTPException(
                    status_code=400,
                    detail="Ungueltiges follow_up_date (ISO 8601 erwartet, z.B. 2025-06-15)",
                )
        else:
            follow_up_date = (datetime.now(timezone.utc) + timedelta(days=config.default_follow_up_days)).date()

    # 3. Kandidat laden
    candidate = await db.get(Candidate, data.candidate_id)
    if not candidate:
        raise HTTPException(status_code=404, detail="Kandidat nicht gefunden")

    actions_taken = []

    # 4. Willingness aktualisieren
    if config.willingness:
        candidate.willingness_to_change = config.willingness
        actions_taken.append(f"willingness={config.willingness}")

    # 5. last_contact aktualisieren (immer)
    candidate.last_contact = datetime.now(timezone.utc)
    candidate.updated_at = datetime.now(timezone.utc)

    # 6. Note anhaengen
    if config.add_note and data.note:
        timestamp = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M")
        note_prefix = f"[{config.label}]"
        note_text = f"{note_prefix} {data.note}"
        if data.email_subject:
            note_text = f"{note_prefix} (Betreff: {data.email_subject}) {data.note}"

        if candidate.candidate_notes:
            candidate.candidate_notes += f"\n\n--- {timestamp} ---\n{note_text}"
        else:
            candidate.candidate_notes = f"--- {timestamp} ---\n{note_text}"
        actions_taken.append("note_added")

    # 7. presented_at_companies eintragen (already_presented / already_applied)
    if config.add_presented_at and data.company_name:
        from datetime import date as date_type_import
        entry = {
            "company": data.company_name,
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "type": "applied" if data.response_type == "already_applied" else "presented",
        }
        if data.note:
            entry["note"] = data.note
        if candidate.presented_at_companies:
            candidate.presented_at_companies = candidate.presented_at_companies + [entry]
        else:
            candidate.presented_at_companies = [entry]
        actions_taken.append(f"presented_at={data.company_name}")

    # 8. Todo erstellen
    todo_id = None
    if config.todo_title:
        service = ATSTodoService(db)
        todo = await service.create_todo(
            title=config.todo_title,
            description=f"Automatisch erstellt: Kandidat hat auf Job-Vorschlag geantwortet ({config.label})"
                + (f"\nBetreff: {data.email_subject}" if data.email_subject else "")
                + (f"\nNotiz: {data.note}" if data.note else ""),
            priority=config.todo_priority,
            due_date=follow_up_date,
            candidate_id=data.candidate_id,
            ats_job_id=data.ats_job_id,
        )
        todo_id = str(todo.id)
        actions_taken.append(f"todo_created={config.todo_title}")
        if follow_up_date:
            actions_taken.append(f"due_date={follow_up_date.isoformat()}")

    # 9. Pipeline verschieben (nur bei "interested" + ats_job_id)
    pipeline_moved = False
    if config.move_to_feedback and data.ats_job_id:
        pipeline_service = ATSPipelineService(db)
        entries = await pipeline_service.get_entries_for_candidate(data.candidate_id)
        for entry in entries:
            if entry.ats_job_id == data.ats_job_id:
                await pipeline_service.move_stage(entry.id, "feedback")
                pipeline_moved = True
                actions_taken.append("pipeline_moved=feedback")
                break

    # 10. Activity loggen
    description = f"Kandidaten-Antwort: {config.label}"
    if data.email_subject:
        description += f" (Betreff: {data.email_subject[:60]})"

    activity = ATSActivity(
        activity_type=ActivityType.CANDIDATE_RESPONSE,
        description=description,
        ats_job_id=data.ats_job_id,
        candidate_id=data.candidate_id,
        metadata_json={
            "response_type": data.response_type,
            "label": config.label,
            "email_subject": data.email_subject,
            "actions_taken": actions_taken,
            "follow_up_date": follow_up_date.isoformat() if follow_up_date else None,
            "candidate_number": candidate.candidate_number,
        },
    )
    db.add(activity)

    await db.commit()
    logger.info(f"n8n Job Response: {data.candidate_id} -> {data.response_type} ({actions_taken})")

    return {
        "success": True,
        "candidate_id": str(data.candidate_id),
        "candidate_number": candidate.candidate_number,
        "response_type": data.response_type,
        "label": config.label,
        "actions_taken": actions_taken,
        "todo_id": todo_id,
        "pipeline_moved": pipeline_moved,
        "follow_up_date": follow_up_date.isoformat() if follow_up_date else None,
        "notify_whatsapp": config.notify_whatsapp,
        "whatsapp_message": (
            f"📩 {config.label}: {candidate.full_name} (#{candidate.candidate_number})"
            + (f"\nBetreff: {data.email_subject}" if data.email_subject else "")
            + (f"\n💬 {data.note[:100]}" if data.note else "")
        ) if config.notify_whatsapp else None,
    }


@router.get("/candidates/by-email")
async def n8n_get_candidate_by_email(
    email: str = Query(..., description="E-Mail-Adresse des Kandidaten"),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_n8n_token),
):
    """n8n: Kandidat per E-Mail-Adresse suchen (fuer Sender-Aufloesung)."""
    from sqlalchemy import select, func
    from app.models.candidate import Candidate

    result = await db.execute(
        select(Candidate)
        .where(func.lower(Candidate.email) == func.lower(email.strip()))
        .where(Candidate.deleted_at.is_(None))
        .limit(1)
    )
    candidate = result.scalar_one_or_none()

    if not candidate:
        raise HTTPException(status_code=404, detail=f"Kein Kandidat mit E-Mail '{email}' gefunden")

    return {
        "success": True,
        "candidate_id": str(candidate.id),
        "candidate_number": candidate.candidate_number,
        "full_name": candidate.full_name,
        "email": candidate.email,
    }


@router.post("/candidate/presented-at")
async def n8n_add_presented_at(
    data: PresentedAtRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_n8n_token),
):
    """n8n: Unternehmen zur 'vorgestellt bei / beworben bei' Liste hinzufuegen."""
    from datetime import datetime, timezone
    from app.models.candidate import Candidate

    candidate = await db.get(Candidate, data.candidate_id)
    if not candidate:
        raise HTTPException(status_code=404, detail="Kandidat nicht gefunden")

    entry = {
        "company": data.company_name,
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "type": data.entry_type,
    }
    if data.note:
        entry["note"] = data.note

    if candidate.presented_at_companies:
        candidate.presented_at_companies = candidate.presented_at_companies + [entry]
    else:
        candidate.presented_at_companies = [entry]

    candidate.updated_at = datetime.now(timezone.utc)
    await db.commit()

    logger.info(f"n8n Presented At: {data.candidate_id} -> {data.company_name} ({data.entry_type})")
    return {
        "success": True,
        "candidate_id": str(data.candidate_id),
        "candidate_number": candidate.candidate_number,
        "company": data.company_name,
        "type": data.entry_type,
        "total_entries": len(candidate.presented_at_companies),
    }


@router.get("/daily-report")
async def n8n_daily_report(
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_n8n_token),
):
    """n8n: Tagesbericht — alle Aktivitaeten der letzten 24h fuer den Morgen-Report.

    Gibt eine Zusammenfassung zurueck die n8n per WhatsApp/E-Mail senden kann.
    WICHTIG: Nur candidate_number als Referenz, keine persoenlichen Daten.
    """
    from datetime import datetime, timezone, timedelta
    from sqlalchemy import select, func
    from app.models.ats_activity import ATSActivity, ActivityType, ACTIVITY_TYPE_LABELS
    from app.models.ats_todo import ATSTodo

    since = datetime.now(timezone.utc) - timedelta(hours=24)

    # 1. Aktivitaeten der letzten 24h zaehlen nach Typ
    result = await db.execute(
        select(ATSActivity.activity_type, func.count())
        .where(ATSActivity.created_at >= since)
        .group_by(ATSActivity.activity_type)
    )
    activity_counts = {row[0].value: row[1] for row in result.all()}

    # 2. Kandidaten-Antworten im Detail (ohne PII — nur candidate_number)
    result = await db.execute(
        select(ATSActivity)
        .where(ATSActivity.created_at >= since)
        .where(ATSActivity.activity_type == ActivityType.CANDIDATE_RESPONSE)
        .order_by(ATSActivity.created_at.desc())
    )
    responses = result.scalars().all()

    response_details = []
    for r in responses:
        meta = r.metadata_json or {}
        response_details.append({
            "response_type": meta.get("response_type"),
            "label": meta.get("label"),
            "candidate_number": meta.get("candidate_number"),
            "actions_taken": meta.get("actions_taken", []),
            "time": r.created_at.strftime("%H:%M") if r.created_at else None,
        })

    # 3. Offene Todos (faellig heute oder ueberfaellig)
    from datetime import date as date_type
    today = date_type.today()
    result = await db.execute(
        select(func.count())
        .select_from(ATSTodo)
        .where(ATSTodo.status.in_(["open", "in_progress"]))
        .where(ATSTodo.due_date <= today)
    )
    overdue_count = result.scalar() or 0

    # 4. Gesamt offene Todos
    result = await db.execute(
        select(func.count())
        .select_from(ATSTodo)
        .where(ATSTodo.status.in_(["open", "in_progress"]))
    )
    open_todos_count = result.scalar() or 0

    total_activities = sum(activity_counts.values())

    return {
        "success": True,
        "period": "24h",
        "since": since.isoformat(),
        "summary": {
            "total_activities": total_activities,
            "activity_breakdown": {
                ACTIVITY_TYPE_LABELS.get(ActivityType(k), k): v
                for k, v in activity_counts.items()
            },
            "candidate_responses": len(response_details),
            "response_details": response_details,
            "todos_overdue": overdue_count,
            "todos_open_total": open_todos_count,
        },
    }
