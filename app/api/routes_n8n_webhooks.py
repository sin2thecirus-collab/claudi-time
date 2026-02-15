"""n8n Webhook-Routen — Inbound-API fuer n8n-Automatisierung."""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.services.ats_call_note_service import ATSCallNoteService
from app.services.ats_pipeline_service import ATSPipelineService
from app.services.ats_todo_service import ATSTodoService
from app.services.call_transcription_service import CallTranscriptionService
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
    title: str | None = None  # z.B. "Qualifizierungsgespräch (KI)"
    source: str | None = None  # "ki_transkription", "n8n", "manual"
    note_date: str | None = None  # ISO oder DD.MM.YYYY


class CandidateLastContactRequest(BaseModel):
    candidate_id: UUID
    contact_date: Optional[str] = None  # ISO-Format, None = jetzt


class CallTranscribeRequest(BaseModel):
    candidate_id: UUID
    audio_url: Optional[str] = Field(default=None, description="URL zur Audio-Datei")
    transcript_text: Optional[str] = Field(default=None, description="Bereits transkribierter Text (ueberspringt Whisper)")


class CallStoreOrAssignRequest(BaseModel):
    """Request fuer den Zwischenspeicher-Endpoint: Anruf zuordnen oder stagen."""
    phone_number: Optional[str] = None
    direction: str = "inbound"  # "inbound" / "outbound"
    call_date: Optional[str] = None  # ISO DateTime
    duration_seconds: Optional[int] = None
    transcript: Optional[str] = None
    call_summary: Optional[str] = None
    call_type: Optional[str] = None  # "qualifizierung" / "kurzer_call" / "akquise" / "sonstiges"
    extracted_data: Optional[dict] = None
    recording_topic: Optional[str] = None
    webex_recording_id: Optional[str] = None
    webex_access_token: Optional[str] = None
    mt_payload: Optional[dict] = None
    candidate_id: Optional[str] = None  # Falls n8n den Kandidaten schon kennt


class CallAssignRequest(BaseModel):
    """Request fuer manuelle Zuordnung eines gestageten Anrufs."""
    entity_type: str  # "candidate" / "contact" / "company"
    entity_id: UUID


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
    """n8n: Erstellt eine neue CandidateNote. Aktualisiert auch last_contact."""
    from app.models.candidate import Candidate
    from app.models.candidate_note import CandidateNote

    candidate = await db.get(Candidate, data.candidate_id)
    if not candidate:
        raise HTTPException(status_code=404, detail="Kandidat nicht gefunden")

    # Datum parsen
    note_date = datetime.now(timezone.utc)
    if data.note_date:
        try:
            note_date = datetime.fromisoformat(data.note_date.replace("Z", "+00:00"))
        except ValueError:
            try:
                note_date = datetime.strptime(data.note_date, "%d.%m.%Y").replace(tzinfo=timezone.utc)
            except ValueError:
                pass  # Fallback: jetzt

    # Neue CandidateNote erstellen
    note = CandidateNote(
        candidate_id=data.candidate_id,
        title=data.title or "n8n Notiz",
        content=data.notes,
        source=data.source or "n8n",
        note_date=note_date,
    )
    db.add(note)

    candidate.last_contact = datetime.now(timezone.utc)
    candidate.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(note)

    logger.info(f"n8n CandidateNote Created: {data.candidate_id} -> {note.id}")
    return {"success": True, "candidate_id": str(data.candidate_id), "note_id": str(note.id)}


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


# ── Call Transcription ────────────────────────────


@router.post("/call/transcribe")
async def n8n_call_transcribe(
    data: CallTranscribeRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_n8n_token),
):
    """n8n: Audio-Datei transkribieren + KI-Analyse → Kandidatenfelder aktualisieren.

    Akzeptiert audio_url ODER transcript_text.
    - audio_url: URL zur Audio-Datei (Whisper transkribiert)
    - transcript_text: Bereits transkribierter Text (ueberspringt Whisper)

    Pipeline:
    1. Whisper: Audio → Transkript
    2. GPT-4o-mini: Gespraechstyp klassifizieren
    3. GPT-4o-mini: Felder extrahieren
    4. DB-Update: Kandidatenfelder aktualisieren
    """
    if not data.audio_url and not data.transcript_text:
        raise HTTPException(status_code=400, detail="Entweder audio_url oder transcript_text muss gesetzt sein")

    service = CallTranscriptionService(db)
    try:
        result = await service.process_call(
            candidate_id=data.candidate_id,
            audio_url=data.audio_url,
            transcript_text=data.transcript_text,
        )

        if not result.get("success"):
            raise HTTPException(status_code=400, detail=result.get("error", "Unbekannter Fehler"))

        await db.commit()

        logger.info(
            f"n8n Call Transcribed: Kandidat={result.get('candidate_name')}, "
            f"Typ={result.get('call_type')}, Felder={len(result.get('fields_updated', []))}, "
            f"Kosten=${result.get('cost_usd', 0):.4f}"
        )

        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Call-Transkription fehlgeschlagen: {e}")
        raise HTTPException(status_code=500, detail=f"Interner Fehler: {str(e)}")
    finally:
        await service.close()


# ── Debug Endpoints ────────────────────────────────


@router.get("/debug/health")
async def n8n_debug_health(
    _: None = Depends(verify_n8n_token),
):
    """Debug: Prueft ob der n8n-Webhook-Service erreichbar ist."""
    import platform
    from datetime import datetime, timezone

    return {
        "success": True,
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "python_version": platform.python_version(),
        "service": "matching-tool",
    }


@router.get("/debug/openai-status")
async def n8n_debug_openai_status(
    _: None = Depends(verify_n8n_token),
):
    """Debug: Prueft ob OpenAI API-Key konfiguriert ist und funktioniert."""
    from app.config import settings

    if not settings.openai_api_key:
        return {"success": False, "error": "OPENAI_API_KEY nicht konfiguriert"}

    key_preview = settings.openai_api_key[:8] + "..." + settings.openai_api_key[-4:]

    # Test-Request an OpenAI (guenstigster Call: models list)
    try:
        async with httpx.AsyncClient(
            base_url="https://api.openai.com/v1",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            timeout=10.0,
        ) as client:
            response = await client.get("/models")
            response.raise_for_status()

            models = response.json().get("data", [])
            model_ids = [m["id"] for m in models if "whisper" in m["id"] or "gpt-4o-mini" in m["id"]]

            return {
                "success": True,
                "api_key_preview": key_preview,
                "relevant_models": sorted(model_ids),
                "total_models_available": len(models),
            }
    except httpx.HTTPStatusError as e:
        return {"success": False, "api_key_preview": key_preview, "error": f"HTTP {e.response.status_code}: {e.response.text[:200]}"}
    except Exception as e:
        return {"success": False, "api_key_preview": key_preview, "error": str(e)}


@router.get("/debug/candidate/{candidate_id}")
async def n8n_debug_candidate(
    candidate_id: UUID,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_n8n_token),
):
    """Debug: Zeigt alle Qualifizierungs-Felder eines Kandidaten."""
    from app.models.candidate import Candidate

    candidate = await db.get(Candidate, candidate_id)
    if not candidate:
        raise HTTPException(status_code=404, detail="Kandidat nicht gefunden")

    qualification_fields = {
        "desired_positions": candidate.desired_positions,
        "key_activities": candidate.key_activities,
        "home_office_days": candidate.home_office_days,
        "commute_max": candidate.commute_max,
        "commute_transport": candidate.commute_transport,
        "erp_main": candidate.erp_main,
        "employment_type": candidate.employment_type,
        "part_time_hours": candidate.part_time_hours,
        "preferred_industries": candidate.preferred_industries,
        "avoided_industries": candidate.avoided_industries,
        "open_office_ok": candidate.open_office_ok,
        "whatsapp_ok": candidate.whatsapp_ok,
        "other_recruiters": candidate.other_recruiters,
        "exclusivity_agreed": candidate.exclusivity_agreed,
        "applied_at_companies_text": candidate.applied_at_companies_text,
        "call_transcript": candidate.call_transcript[:200] + "..." if candidate.call_transcript and len(candidate.call_transcript) > 200 else candidate.call_transcript,
        "call_summary": candidate.call_summary,
        "call_date": candidate.call_date.isoformat() if candidate.call_date else None,
        "call_type": candidate.call_type,
    }

    basic_fields = {
        "salary": candidate.salary,
        "notice_period": candidate.notice_period,
        "erp": candidate.erp,
        "willingness_to_change": candidate.willingness_to_change,
        "last_contact": candidate.last_contact.isoformat() if candidate.last_contact else None,
    }

    filled_count = sum(1 for v in qualification_fields.values() if v is not None)

    return {
        "success": True,
        "candidate_id": str(candidate_id),
        "candidate_name": candidate.full_name,
        "qualification_fields": qualification_fields,
        "basic_fields": basic_fields,
        "filled_count": filled_count,
        "total_fields": len(qualification_fields),
    }


@router.post("/debug/whisper-test")
async def n8n_debug_whisper_test(
    audio_url: str = Query(..., description="URL zur Audio-Datei"),
    _: None = Depends(verify_n8n_token),
):
    """Debug: Testet NUR die Whisper-Transkription (ohne Kandidat/DB)."""
    import httpx as httpx_lib

    if not settings.openai_api_key:
        return {"success": False, "error": "OPENAI_API_KEY nicht konfiguriert"}

    # Audio herunterladen
    try:
        async with httpx_lib.AsyncClient(timeout=120.0) as client:
            dl_response = await client.get(audio_url)
            dl_response.raise_for_status()
            audio_data = dl_response.content
            audio_size = len(audio_data)
            logger.info(f"Debug Whisper: Audio heruntergeladen ({audio_size} Bytes)")
    except Exception as e:
        return {"success": False, "error": f"Audio-Download fehlgeschlagen: {str(e)}"}

    # Whisper transkribieren
    try:
        async with httpx_lib.AsyncClient(
            base_url="https://api.openai.com/v1",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            timeout=httpx_lib.Timeout(300.0),
        ) as client:
            filename = audio_url.split("/")[-1].split("?")[0] or "recording.mp3"
            files = {"file": (filename, audio_data, "audio/mpeg")}
            data = {"model": "whisper-1", "language": "de", "response_format": "text"}

            response = await client.post(
                "/audio/transcriptions",
                files=files,
                data=data,
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            )
            response.raise_for_status()
            transcript = response.text.strip()

            word_count = len(transcript.split())
            estimated_minutes = max(1, word_count / 150)

            return {
                "success": True,
                "audio_url": audio_url,
                "audio_size_bytes": audio_size,
                "transcript_length": len(transcript),
                "word_count": word_count,
                "estimated_minutes": round(estimated_minutes, 1),
                "estimated_cost_usd": round(estimated_minutes * 0.006, 4),
                "transcript_preview": transcript[:500] + ("..." if len(transcript) > 500 else ""),
                "full_transcript": transcript,
            }
    except httpx_lib.HTTPStatusError as e:
        return {"success": False, "error": f"Whisper HTTP-Fehler: {e.response.status_code} - {e.response.text[:300]}"}
    except Exception as e:
        return {"success": False, "error": f"Whisper-Fehler: {str(e)}"}


@router.post("/debug/classify-test")
async def n8n_debug_classify_test(
    transcript_text: str = Query(..., description="Transkript-Text zur Klassifizierung"),
    _: None = Depends(verify_n8n_token),
):
    """Debug: Testet NUR die GPT-Klassifizierung (ohne DB)."""
    if not settings.openai_api_key:
        return {"success": False, "error": "OPENAI_API_KEY nicht konfiguriert"}

    from app.services.call_transcription_service import CLASSIFY_SYSTEM_PROMPT

    snippet = transcript_text[:3000]
    if len(transcript_text) > 3000:
        snippet += "\n\n[... Transkript gekuerzt fuer Klassifizierung ...]"

    try:
        async with httpx.AsyncClient(
            base_url="https://api.openai.com/v1",
            headers={
                "Authorization": f"Bearer {settings.openai_api_key}",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        ) as client:
            response = await client.post(
                "/chat/completions",
                json={
                    "model": "gpt-4o-mini",
                    "messages": [
                        {"role": "system", "content": CLASSIFY_SYSTEM_PROMPT},
                        {"role": "user", "content": snippet},
                    ],
                    "temperature": 0.1,
                    "max_tokens": 150,
                    "response_format": {"type": "json_object"},
                },
            )
            response.raise_for_status()
            result = response.json()

            import json
            content = result["choices"][0]["message"]["content"]
            parsed = json.loads(content)

            usage = result.get("usage", {})

            return {
                "success": True,
                "classification": parsed,
                "input_length": len(transcript_text),
                "usage": usage,
            }
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.get("/debug/db-fields")
async def n8n_debug_db_fields(
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_n8n_token),
):
    """Debug: Prueft ob alle Qualifizierungs-Felder in der DB existieren."""
    from sqlalchemy import text

    expected_fields = [
        "desired_positions", "key_activities", "home_office_days",
        "commute_max", "commute_transport", "erp_main",
        "employment_type", "part_time_hours", "preferred_industries",
        "avoided_industries", "open_office_ok", "whatsapp_ok",
        "other_recruiters", "exclusivity_agreed", "applied_at_companies_text",
        "call_transcript", "call_summary", "call_date", "call_type",
    ]

    result = await db.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'candidates' ORDER BY ordinal_position"
        )
    )
    existing = [row[0] for row in result.fetchall()]

    missing = [f for f in expected_fields if f not in existing]
    present = [f for f in expected_fields if f in existing]

    return {
        "success": len(missing) == 0,
        "expected_fields": len(expected_fields),
        "present_fields": len(present),
        "missing_fields": missing,
        "all_candidate_columns": existing,
    }


# ── Profil-PDF nach Quali-Gespraech ──────────────────


async def _generate_profile_pdf_background(
    db: AsyncSession,
    candidate_id: str,
    candidate_name: str,
) -> dict:
    """Generiert Profil-PDF, speichert es in R2 und verknuepft es mit dem Kandidaten.

    Fehler werden geloggt aber NICHT weitergegeben — die Zuordnung soll
    NIEMALS an der PDF-Generierung scheitern.

    Returns:
        Dict mit pdf_status, pdf_r2_key, pdf_size_bytes
    """
    try:
        from app.models.candidate import Candidate
        from app.services.profile_pdf_service import ProfilePdfService
        from app.services.r2_storage_service import R2StorageService

        pdf_service = ProfilePdfService(db)
        pdf_bytes = await pdf_service.generate_profile_pdf(UUID(candidate_id))

        if not pdf_bytes:
            logger.warning(f"PDF-Generierung lieferte leere Bytes fuer {candidate_name}")
            return {"pdf_status": "empty", "pdf_r2_key": None}

        # In R2 speichern
        r2 = R2StorageService()
        if r2.is_available:
            # Sicherer Dateiname: Sonderzeichen entfernen
            safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", candidate_name)
            r2_key = f"profiles/{candidate_id[:8]}_{safe_name}_profil.pdf"
            r2.upload_file(
                key=r2_key,
                file_content=pdf_bytes,
                content_type="application/pdf",
            )

            # R2-Key am Kandidaten speichern
            candidate = await db.get(Candidate, UUID(candidate_id))
            if candidate:
                candidate.profile_pdf_r2_key = r2_key
                candidate.profile_pdf_generated_at = datetime.now(timezone.utc)
                await db.commit()

            logger.info(
                f"Profil-PDF generiert + R2 + DB: {r2_key} "
                f"({len(pdf_bytes)} Bytes) fuer {candidate_name}"
            )
            return {
                "pdf_status": "generated_and_uploaded",
                "pdf_r2_key": r2_key,
                "pdf_size_bytes": len(pdf_bytes),
            }
        else:
            logger.info(
                f"Profil-PDF generiert (kein R2): "
                f"{len(pdf_bytes)} Bytes fuer {candidate_name}"
            )
            return {
                "pdf_status": "generated_no_r2",
                "pdf_r2_key": None,
                "pdf_size_bytes": len(pdf_bytes),
            }

    except Exception as e:
        logger.error(f"Profil-PDF Generierung fehlgeschlagen fuer {candidate_name}: {e}")
        return {"pdf_status": f"error: {str(e)[:200]}", "pdf_r2_key": None}


# ── Zwischenspeicher: Unzugeordnete Anrufe ──────────


def _normalize_phone(phone: str) -> str:
    """Entfernt alles ausser Ziffern, gibt letzte 8 Stellen zurueck.

    Funktioniert mit +49, 0049, 0170, etc.
    Vergleich ueber letzte 8 Stellen deckt laenderspezifische Praefixe ab.
    """
    if not phone:
        return ""
    digits = re.sub(r"\D", "", phone)
    return digits[-8:] if len(digits) >= 8 else digits


async def _phone_lookup(db: AsyncSession, phone: str):
    """Sucht Telefonnummer in candidates, company_contacts, companies.

    Gibt (entity_type, entity_id, entity_name, company_id) zurueck oder None.
    Prioritaet: Kandidaten > Kontakte > Unternehmen.
    """
    normalized = _normalize_phone(phone)
    if not normalized or len(normalized) < 4:
        return None

    like_pattern = f"%{normalized}"

    # 1. Kandidaten-Suche
    result = await db.execute(
        text("""
            SELECT id, first_name, last_name
            FROM candidates
            WHERE REGEXP_REPLACE(COALESCE(phone, ''), '[^0-9]', '', 'g') LIKE :pattern
              AND deleted_at IS NULL
            LIMIT 1
        """),
        {"pattern": like_pattern},
    )
    row = result.fetchone()
    if row:
        name = f"{row[1] or ''} {row[2] or ''}".strip()
        return {"entity_type": "candidate", "entity_id": str(row[0]), "entity_name": name, "company_id": None}

    # 2. Company-Contact-Suche (phone + mobile)
    result = await db.execute(
        text("""
            SELECT cc.id, cc.first_name, cc.last_name, cc.company_id
            FROM company_contacts cc
            WHERE (REGEXP_REPLACE(COALESCE(cc.phone, ''), '[^0-9]', '', 'g') LIKE :pattern
                OR REGEXP_REPLACE(COALESCE(cc.mobile, ''), '[^0-9]', '', 'g') LIKE :pattern)
            LIMIT 1
        """),
        {"pattern": like_pattern},
    )
    row = result.fetchone()
    if row:
        name = f"{row[1] or ''} {row[2] or ''}".strip()
        return {"entity_type": "contact", "entity_id": str(row[0]), "entity_name": name, "company_id": str(row[3]) if row[3] else None}

    # 3. Company-Suche (Zentrale)
    result = await db.execute(
        text("""
            SELECT id, name
            FROM companies
            WHERE REGEXP_REPLACE(COALESCE(phone, ''), '[^0-9]', '', 'g') LIKE :pattern
            LIMIT 1
        """),
        {"pattern": like_pattern},
    )
    row = result.fetchone()
    if row:
        return {"entity_type": "company", "entity_id": str(row[0]), "entity_name": row[1] or "", "company_id": str(row[0])}

    return None


async def _auto_assign_to_candidate(
    db: AsyncSession,
    candidate_id: str,
    data: CallStoreOrAssignRequest,
):
    """Ordnet Anrufdaten automatisch einem Kandidaten zu.

    Aktualisiert: call_transcript, call_summary, call_date, call_type, last_contact
    + Qualifizierungsfelder aus extracted_data.
    Erstellt: ATSCallNote + ATSActivity.
    """
    from app.models.ats_activity import ATSActivity, ActivityType
    from app.models.ats_call_note import ATSCallNote, CallDirection, CallType
    from app.models.candidate import Candidate

    # Kandidat laden
    result = await db.execute(
        select(Candidate).where(Candidate.id == candidate_id)
    )
    candidate = result.scalar_one_or_none()
    if not candidate:
        logger.warning(f"Auto-Assign: Kandidat {candidate_id} nicht gefunden")
        return {"success": False, "error": f"Kandidat {candidate_id} nicht gefunden"}

    # Kandidatenfelder updaten
    now = datetime.now(timezone.utc)
    if data.transcript:
        candidate.call_transcript = data.transcript
    if data.call_summary:
        candidate.call_summary = data.call_summary
    if data.call_date:
        try:
            candidate.call_date = datetime.fromisoformat(data.call_date.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            candidate.call_date = now
    else:
        candidate.call_date = now
    candidate.call_type = data.call_type or "kurzer_call"
    candidate.last_contact = now

    # Qualifizierungsfelder aus extracted_data / mt_payload
    ext = data.extracted_data or data.mt_payload or {}
    field_mappings = {
        "desired_positions": "desired_positions",
        "key_activities": "key_activities",
        "home_office_days": "home_office_days",
        "commute_max": "commute_max",
        "commute_transport": "commute_transport",
        "erp_main": "erp_main",
        "employment_type": "employment_type",
        "part_time_hours": "part_time_hours",
        "preferred_industries": "preferred_industries",
        "avoided_industries": "avoided_industries",
        "salary": "salary",
        "notice_period": "notice_period",
    }
    fields_updated = []
    for src_key, dest_key in field_mappings.items():
        val = ext.get(src_key) or ext.get(f"call_{src_key}")
        if val is not None and val != "" and val != []:
            # Array-Felder als kommaseparierten String speichern falls noetig
            if isinstance(val, list):
                val = ", ".join(str(v) for v in val)
            # Alle Werte zu String konvertieren (GPT liefert z.B. Integer 3 statt "3")
            val = str(val)
            if hasattr(candidate, dest_key):
                setattr(candidate, dest_key, val)
                fields_updated.append(dest_key)

    # Willingness aus extracted_data
    willingness = ext.get("willingness_to_change") or ext.get("call_willingness_to_change")
    if willingness in ("ja", "nein", "unbekannt", "unklar"):
        if willingness == "unklar":
            willingness = "unbekannt"
        candidate.willingness_to_change = willingness
        fields_updated.append("willingness_to_change")

    # ATSCallNote erstellen — call_type korrekt mappen
    direction_val = CallDirection.INBOUND if data.direction == "inbound" else CallDirection.OUTBOUND
    n8n_type = (data.call_type or "").lower()
    call_type_map = {
        "qualifizierung": CallType.QUALIFICATION,
        "kurzer_call": CallType.CANDIDATE_CALL,
        "akquise": CallType.ACQUISITION,
        "sonstiges": CallType.CANDIDATE_CALL,
        "followup": CallType.FOLLOWUP,
    }
    mapped_call_type = call_type_map.get(n8n_type, CallType.CANDIDATE_CALL)

    # Action Items aus extracted_data extrahieren
    action_items = ext.get("action_items") or ext.get("follow_ups") or ext.get("tasks")

    # Transkript (raw_notes) NUR bei Qualifizierung speichern
    raw_notes = None
    if mapped_call_type == CallType.QUALIFICATION and data.transcript:
        raw_notes = data.transcript[:5000]

    call_note = ATSCallNote(
        candidate_id=candidate.id,
        call_type=mapped_call_type,
        direction=direction_val,
        summary=data.call_summary or "Anruf ohne Zusammenfassung",
        raw_notes=raw_notes,
        duration_minutes=(data.duration_seconds // 60) if data.duration_seconds else None,
        called_at=candidate.call_date or now,
        action_items=action_items if isinstance(action_items, list) else None,
    )
    db.add(call_note)
    await db.flush()  # flush damit call_note.id verfuegbar ist

    # Activity loggen
    activity = ATSActivity(
        activity_type=ActivityType.CALL_LOGGED,
        description=f"Anruf zugeordnet ({mapped_call_type.value}): {data.call_summary[:100] if data.call_summary else 'Gespraech'}",
        candidate_id=candidate.id,
        metadata_json={
            "source": "webex_auto_assign",
            "direction": data.direction,
            "duration_seconds": data.duration_seconds,
            "fields_updated": fields_updated,
            "phone_number": data.phone_number,
        },
    )
    db.add(activity)

    # Automatisch ATSTodos aus action_items erstellen
    todos_created = await _create_todos_from_action_items(
        db, call_note, candidate_id=candidate.id,
    )

    return {
        "success": True,
        "candidate_name": f"{candidate.first_name or ''} {candidate.last_name or ''}".strip(),
        "fields_updated": fields_updated,
        "call_note_id": str(call_note.id),
        "todos_created": todos_created,
    }


async def _auto_assign_to_contact_or_company(
    db: AsyncSession,
    entity_type: str,
    entity_id: str,
    company_id: str | None,
    data: CallStoreOrAssignRequest,
):
    """Ordnet Anrufdaten einem Kontakt oder Unternehmen zu (ATSCallNote + Activity)."""
    from app.models.ats_activity import ATSActivity, ActivityType
    from app.models.ats_call_note import ATSCallNote, CallDirection, CallType

    now = datetime.now(timezone.utc)
    direction_val = CallDirection.INBOUND if data.direction == "inbound" else CallDirection.OUTBOUND

    # call_type korrekt mappen
    n8n_type = (data.call_type or "").lower()
    call_type_map = {
        "qualifizierung": CallType.QUALIFICATION,
        "kurzer_call": CallType.CANDIDATE_CALL,
        "akquise": CallType.ACQUISITION,
        "sonstiges": CallType.CANDIDATE_CALL,
        "followup": CallType.FOLLOWUP,
    }
    mapped_call_type = call_type_map.get(n8n_type, CallType.ACQUISITION)

    # Action Items aus extracted_data extrahieren
    ext = data.extracted_data or data.mt_payload or {}
    action_items = ext.get("action_items") or ext.get("follow_ups") or ext.get("tasks")

    # Transkript (raw_notes) NUR bei Qualifizierung speichern
    raw_notes = None
    if mapped_call_type == CallType.QUALIFICATION and data.transcript:
        raw_notes = data.transcript[:5000]

    call_note = ATSCallNote(
        call_type=mapped_call_type,
        direction=direction_val,
        summary=data.call_summary or "Anruf ohne Zusammenfassung",
        raw_notes=raw_notes,
        duration_minutes=(data.duration_seconds // 60) if data.duration_seconds else None,
        called_at=now,
        action_items=action_items if isinstance(action_items, list) else None,
    )

    if entity_type == "contact":
        call_note.contact_id = entity_id
        if company_id:
            call_note.company_id = company_id
    elif entity_type == "company":
        call_note.company_id = entity_id

    db.add(call_note)
    await db.flush()  # flush damit call_note.id verfuegbar ist

    # Activity loggen
    activity = ATSActivity(
        activity_type=ActivityType.CALL_LOGGED,
        description=f"Anruf automatisch zugeordnet ({entity_type}): {data.call_summary[:100] if data.call_summary else 'Gespraech'}",
        company_id=company_id or (entity_id if entity_type == "company" else None),
        metadata_json={
            "source": "webex_auto_assign",
            "entity_type": entity_type,
            "entity_id": entity_id,
            "direction": data.direction,
            "phone_number": data.phone_number,
        },
    )
    db.add(activity)

    # Automatisch ATSTodos aus action_items erstellen
    resolved_company_id = company_id or (entity_id if entity_type == "company" else None)
    todos_created = await _create_todos_from_action_items(
        db, call_note, company_id=resolved_company_id,
    )

    return {
        "success": True,
        "call_note_id": str(call_note.id),
        "todos_created": todos_created,
    }


async def _create_todos_from_action_items(
    db: AsyncSession,
    call_note,
    candidate_id=None,
    company_id=None,
) -> int:
    """Erstellt ATSTodo-Records aus action_items einer CallNote.

    GPT liefert action_items als Liste von Dicts:
    [{title: str, due_date: str (ISO), priority: str (hoch/mittel/niedrig)}]
    Gibt Anzahl erstellter Todos zurueck.
    """
    from datetime import date as d_date
    from app.models.ats_todo import ATSTodo, TodoPriority
    from app.models.ats_activity import ATSActivity, ActivityType

    items = call_note.action_items
    if not items or not isinstance(items, list):
        return 0

    # Prioritaet-Mapping: GPT-Werte → TodoPriority
    priority_map = {
        "hoch": TodoPriority.DRINGEND,
        "mittel": TodoPriority.WICHTIG,
        "niedrig": TodoPriority.MITTELMAESSIG,
        "high": TodoPriority.DRINGEND,
        "medium": TodoPriority.WICHTIG,
        "low": TodoPriority.MITTELMAESSIG,
    }

    created = 0
    for item in items:
        # item kann ein Dict oder ein String sein
        if isinstance(item, dict):
            title = (item.get("title") or "").strip()
            due_date_str = item.get("due_date") or item.get("dueDate")
            priority_str = (item.get("priority") or "mittel").lower()
        elif isinstance(item, str):
            title = item.strip()
            due_date_str = None
            priority_str = "mittel"
        else:
            continue

        if not title:
            continue

        # due_date parsen
        parsed_due_date = None
        if due_date_str:
            try:
                parsed_due_date = d_date.fromisoformat(due_date_str[:10])
            except (ValueError, TypeError):
                parsed_due_date = None

        priority = priority_map.get(priority_str, TodoPriority.WICHTIG)

        todo = ATSTodo(
            title=title[:500],
            priority=priority,
            due_date=parsed_due_date,
            candidate_id=candidate_id,
            company_id=company_id,
            call_note_id=call_note.id,
        )
        db.add(todo)
        created += 1

        # Activity loggen
        activity = ATSActivity(
            activity_type=ActivityType.TODO_CREATED,
            description=f"Aufgabe aus Anruf: {title[:80]}",
            candidate_id=candidate_id,
            company_id=company_id,
        )
        db.add(activity)

    if created > 0:
        await db.flush()
        logger.info(f"Auto-Todos: {created} Aufgaben aus CallNote {call_note.id} erstellt")

    return created


async def _delete_webex_recording(recording_id: str, access_token: str) -> str:
    """Loescht ein Webex-Recording via API. Gibt Status zurueck."""
    if not recording_id or not access_token:
        return "skipped_no_credentials"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.delete(
                f"https://webexapis.com/v1/convergedRecordings/{recording_id}",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if resp.status_code in (200, 204):
                logger.info(f"Webex Recording {recording_id} geloescht")
                return "deleted"
            else:
                logger.warning(f"Webex Delete fehlgeschlagen: {resp.status_code} {resp.text[:200]}")
                return f"failed_{resp.status_code}"
    except Exception as e:
        logger.error(f"Webex Delete Error: {e}")
        return f"error_{str(e)[:100]}"


@router.post("/call/store-or-assign")
async def n8n_call_store_or_assign(
    data: CallStoreOrAssignRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_n8n_token),
):
    """n8n: Anruf zuordnen oder im Zwischenspeicher ablegen.

    1. Falls candidate_id mitgegeben → direkt Auto-Assign
    2. Sonst: Phone-Lookup in candidates, contacts, companies
    3. Match gefunden → Auto-Assign + Webex Recording loeschen
    4. Kein Match → In unassigned_calls Tabelle speichern (Staging)
    """
    from app.models.unassigned_call import UnassignedCall

    actions_taken = []

    # ── Pfad 1: candidate_id direkt mitgegeben ──
    if data.candidate_id:
        result = await _auto_assign_to_candidate(db, data.candidate_id, data)
        if result.get("success"):
            actions_taken.append(f"candidate_updated: {result.get('candidate_name')}")
            actions_taken.append(f"fields: {result.get('fields_updated')}")

            # Webex Recording loeschen
            if data.webex_recording_id and data.webex_access_token:
                delete_status = await _delete_webex_recording(
                    data.webex_recording_id, data.webex_access_token,
                )
                actions_taken.append(f"webex_delete: {delete_status}")

            await db.commit()

            # Profil-PDF NUR bei Qualifizierungsgespraech generieren
            pdf_result = None
            if (data.call_type or "").lower() == "qualifizierung":
                pdf_result = await _generate_profile_pdf_background(
                    db, data.candidate_id, result.get("candidate_name", ""),
                )
                actions_taken.append(f"pdf: {pdf_result.get('pdf_status')}")
            else:
                actions_taken.append(f"pdf: skipped (call_type={data.call_type})")

            return {
                "status": "auto_assigned",
                "entity_type": "candidate",
                "entity_id": data.candidate_id,
                "entity_name": result.get("candidate_name"),
                "actions": actions_taken,
                "pdf": pdf_result,
            }
        else:
            raise HTTPException(status_code=404, detail=result.get("error"))

    # ── Pfad 2: Phone-Lookup ──
    if data.phone_number:
        lookup = await _phone_lookup(db, data.phone_number)

        if lookup:
            entity_type = lookup["entity_type"]
            entity_id = lookup["entity_id"]
            entity_name = lookup["entity_name"]

            logger.info(
                f"Phone-Lookup Match: {data.phone_number} → {entity_type} "
                f"'{entity_name}' ({entity_id})"
            )

            if entity_type == "candidate":
                result = await _auto_assign_to_candidate(db, entity_id, data)
                if result.get("success"):
                    actions_taken.append(f"phone_matched: {entity_name}")
                    actions_taken.append(f"candidate_updated: {entity_id}")
                    actions_taken.append(f"fields: {result.get('fields_updated')}")
            else:
                result = await _auto_assign_to_contact_or_company(
                    db, entity_type, entity_id, lookup.get("company_id"), data,
                )
                if result.get("success"):
                    actions_taken.append(f"phone_matched: {entity_name} ({entity_type})")
                    actions_taken.append(f"call_note_created: {result.get('call_note_id')}")

            # Webex Recording loeschen bei Auto-Assign
            if data.webex_recording_id and data.webex_access_token:
                delete_status = await _delete_webex_recording(
                    data.webex_recording_id, data.webex_access_token,
                )
                actions_taken.append(f"webex_delete: {delete_status}")

            await db.commit()

            # Profil-PDF NUR bei Qualifizierungsgespraech generieren
            pdf_result = None
            if entity_type == "candidate" and (data.call_type or "").lower() == "qualifizierung":
                pdf_result = await _generate_profile_pdf_background(
                    db, entity_id, entity_name,
                )
                actions_taken.append(f"pdf: {pdf_result.get('pdf_status')}")
            elif entity_type == "candidate":
                actions_taken.append(f"pdf: skipped (call_type={data.call_type})")

            return {
                "status": "auto_assigned",
                "entity_type": entity_type,
                "entity_id": entity_id,
                "entity_name": entity_name,
                "actions": actions_taken,
                "pdf": pdf_result,
            }

    # ── Pfad 3: Kein Match → Staging ──
    logger.info(f"Phone-Lookup kein Match: {data.phone_number} → Staging")

    call_date = None
    if data.call_date:
        try:
            call_date = datetime.fromisoformat(data.call_date.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            call_date = datetime.now(timezone.utc)

    staged_call = UnassignedCall(
        phone_number=data.phone_number,
        direction=data.direction,
        call_date=call_date or datetime.now(timezone.utc),
        duration_seconds=data.duration_seconds,
        transcript=data.transcript,
        call_summary=data.call_summary,
        extracted_data=data.extracted_data,
        recording_topic=data.recording_topic,
        webex_recording_id=data.webex_recording_id,
        mt_payload=data.mt_payload,
        assigned=False,
    )
    db.add(staged_call)
    await db.commit()
    await db.refresh(staged_call)

    return {
        "status": "staged",
        "unassigned_call_id": str(staged_call.id),
        "phone_number": data.phone_number,
        "message": "Anruf im Zwischenspeicher abgelegt. Keine passende Telefonnummer in MT gefunden.",
    }


@router.post("/call/assign/{unassigned_call_id}")
async def n8n_call_assign(
    unassigned_call_id: UUID,
    data: CallAssignRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_n8n_token),
):
    """Manuelle Zuordnung eines gestageten Anrufs zu einem Kandidaten/Kontakt/Unternehmen.

    Wird vom Frontend aufgerufen wenn der User einen Anruf aus dem
    Zwischenspeicher zuordnet.
    """
    from app.models.unassigned_call import UnassignedCall

    # Staged Call laden
    result = await db.execute(
        select(UnassignedCall).where(UnassignedCall.id == unassigned_call_id)
    )
    call = result.scalar_one_or_none()
    if not call:
        raise HTTPException(status_code=404, detail="Unzugeordneter Anruf nicht gefunden")

    if call.assigned:
        raise HTTPException(status_code=400, detail="Anruf wurde bereits zugeordnet")

    # Daten als CallStoreOrAssignRequest aufbereiten
    assign_data = CallStoreOrAssignRequest(
        phone_number=call.phone_number,
        direction=call.direction or "inbound",
        call_date=call.call_date.isoformat() if call.call_date else None,
        duration_seconds=call.duration_seconds,
        transcript=call.transcript,
        call_summary=call.call_summary,
        extracted_data=call.extracted_data,
        recording_topic=call.recording_topic,
        webex_recording_id=call.webex_recording_id,
        mt_payload=call.mt_payload,
    )

    actions_taken = []

    # Zuordnung durchfuehren
    if data.entity_type == "candidate":
        result = await _auto_assign_to_candidate(db, str(data.entity_id), assign_data)
        if result.get("success"):
            actions_taken.append(f"candidate_updated: {result.get('candidate_name')}")
            actions_taken.append(f"fields: {result.get('fields_updated')}")
        else:
            raise HTTPException(status_code=404, detail=result.get("error"))
    elif data.entity_type in ("contact", "company"):
        result = await _auto_assign_to_contact_or_company(
            db, data.entity_type, str(data.entity_id), None, assign_data,
        )
        if result.get("success"):
            actions_taken.append(f"call_note_created: {result.get('call_note_id')}")
    else:
        raise HTTPException(status_code=400, detail=f"Ungueltiger entity_type: {data.entity_type}")

    # Staging-Record als zugeordnet markieren
    call.assigned = True
    call.assigned_to_type = data.entity_type
    call.assigned_to_id = data.entity_id
    call.assigned_at = datetime.now(timezone.utc)

    await db.commit()

    return {
        "success": True,
        "status": "assigned",
        "entity_type": data.entity_type,
        "entity_id": str(data.entity_id),
        "actions": actions_taken,
        "message": f"Anruf erfolgreich zugeordnet ({data.entity_type})",
    }


@router.delete("/call/unassigned/{unassigned_call_id}")
async def n8n_call_delete_unassigned(
    unassigned_call_id: UUID,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_n8n_token),
):
    """Loescht einen gestageten Anruf (Gespraech war irrelevant/Muell).

    Hard Delete — Anruf wird komplett entfernt.
    """
    from app.models.unassigned_call import UnassignedCall

    result = await db.execute(
        select(UnassignedCall).where(UnassignedCall.id == unassigned_call_id)
    )
    call = result.scalar_one_or_none()
    if not call:
        raise HTTPException(status_code=404, detail="Unzugeordneter Anruf nicht gefunden")

    call_id_str = str(call.id)
    phone = call.phone_number

    # Hard Delete
    await db.delete(call)
    await db.commit()

    logger.info(f"Unassigned Call geloescht: {call_id_str} (Phone: {phone})")

    return {
        "success": True,
        "deleted_id": call_id_str,
        "message": "Anruf aus Zwischenspeicher geloescht",
    }


@router.get("/call/unassigned")
async def n8n_call_list_unassigned(
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_n8n_token),
    search: Optional[str] = Query(default=None, description="Suche in Telefonnummer oder Summary"),
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
):
    """Listet alle unzugeordneten Anrufe (fuer Frontend-Seite).

    Sortiert nach Datum absteigend (neueste zuerst).
    Optional: Suche in phone_number oder call_summary.
    """
    from app.models.unassigned_call import UnassignedCall

    query = select(UnassignedCall).where(
        UnassignedCall.assigned == False  # noqa: E712
    )

    if search:
        search_pattern = f"%{search}%"
        query = query.where(
            UnassignedCall.phone_number.ilike(search_pattern)
            | UnassignedCall.call_summary.ilike(search_pattern)
            | UnassignedCall.recording_topic.ilike(search_pattern)
        )

    # Count
    from sqlalchemy import func
    count_query = select(func.count(UnassignedCall.id)).where(
        UnassignedCall.assigned == False  # noqa: E712
    )
    total_count = await db.scalar(count_query)

    # Results
    query = query.order_by(UnassignedCall.created_at.desc()).offset(offset).limit(limit)
    result = await db.execute(query)
    calls = result.scalars().all()

    return {
        "success": True,
        "total": total_count or 0,
        "calls": [
            {
                "id": str(c.id),
                "phone_number": c.phone_number,
                "direction": c.direction,
                "call_date": c.call_date.isoformat() if c.call_date else None,
                "duration_seconds": c.duration_seconds,
                "call_summary": c.call_summary,
                "extracted_data": c.extracted_data,
                "recording_topic": c.recording_topic,
                "webex_recording_id": c.webex_recording_id,
                "transcript_preview": c.transcript[:500] if c.transcript else None,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in calls
        ],
    }


@router.post("/debug/simulate-call")
async def n8n_debug_simulate_call(
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_n8n_token),
    phone_number: str = Query(default="0170 1234567", description="Telefonnummer zum Testen"),
    direction: str = Query(default="inbound", description="inbound oder outbound"),
    candidate_id: Optional[str] = Query(default=None, description="Kandidaten-ID (ueberspringt Phone-Lookup)"),
):
    """Debug: Simuliert einen Anruf mit Fake-Transkript.

    Testet die gesamte store-or-assign Logik:
    - Phone-Lookup
    - Auto-Assign (bei Match)
    - Staging (bei keinem Match)

    Kein Webex, kein Whisper, kein GPT — nur DB-Logik.
    """
    fake_data = CallStoreOrAssignRequest(
        phone_number=phone_number,
        direction=direction,
        call_date=datetime.now(timezone.utc).isoformat(),
        duration_seconds=300,
        transcript=(
            "Das ist ein simuliertes Testtranskript. Der Kandidat arbeitet seit 3 Jahren "
            "als Bilanzbuchhalter bei der DATEV in Muenchen. Er sucht eine neue Herausforderung "
            "im Bereich Konzernbuchhaltung und wuenscht 2 Tage Home Office pro Woche. "
            "Gehaltsvorstellung liegt bei 65.000 EUR brutto jaehrlich. "
            "Kuendigungsfrist: 3 Monate zum Quartalsende."
        ),
        call_summary=(
            "Kandidat ist Bilanzbuchhalter bei DATEV in Muenchen (3 Jahre). "
            "Sucht Konzernbuchhaltung, 2 Tage HO, 65k Gehalt, 3 Monate Kuendigungsfrist."
        ),
        extracted_data={
            "desired_positions": ["Bilanzbuchhalter", "Konzernbuchhalter"],
            "key_activities": ["Bilanzierung", "Monatsabschluesse", "Konzernkonsolidierung"],
            "home_office_days": 2,
            "commute_max": "45 Minuten",
            "commute_transport": "Auto",
            "erp_main": "DATEV",
            "employment_type": "Vollzeit",
            "part_time_hours": None,
            "preferred_industries": ["Industrie", "IT"],
            "avoided_industries": ["Einzelhandel"],
            "salary": "65.000 EUR brutto jaehrlich",
            "notice_period": "3 Monate zum Quartalsende",
            "willingness_to_change": "ja",
            "call_summary": "Simuliertes Testgespraech",
        },
        recording_topic="DEBUG: Simulierter Anruf",
        webex_recording_id=None,  # Kein echtes Recording
        webex_access_token=None,
        mt_payload=None,
        candidate_id=candidate_id,
    )

    # Die gleiche Logik wie store-or-assign aufrufen
    return await n8n_call_store_or_assign(data=fake_data, db=db, _=_)
