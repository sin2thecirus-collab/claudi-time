"""n8n Webhook-Routen — Inbound-API fuer n8n-Automatisierung."""

import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.services.ats_call_note_service import ATSCallNoteService
from app.services.ats_pipeline_service import ATSPipelineService
from app.services.ats_todo_service import ATSTodoService

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
    priority: Optional[str] = "normal"
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
    service = ATSTodoService(db)
    todo = await service.create_todo(**data.model_dump(exclude_unset=True))
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
