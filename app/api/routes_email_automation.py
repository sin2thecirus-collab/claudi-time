"""API-Endpoints fuer E-Mail-Automatisierung.

Build: 2026-02-20-v1 - Instantly Webhook Endpoints (email_sent + bounce)

Alle Endpoints fuer:
- E-Mail-Logging (gesendet + empfangen)
- Kandidaten-Aufgaben (aus GPT-Antwort-Analyse)
- Outreach/Rundmail (Tages-Batches, Instantly)
- Sequenz-Kontrolle (aktive, Status, Stop)
- Instantly Events (email_sent → log, email_bounced → status update)
- System-Health + Debug
"""

import logging
from datetime import date, datetime
from typing import Optional
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.candidate import Candidate
from app.models.candidate_email import CandidateEmail
from app.models.candidate_task import CandidateTask
from app.models.outreach_batch import OutreachBatch
from app.models.outreach_item import OutreachItem

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/email-automation", tags=["Email-Automatisierung"])


# ══════════════════════════════════════════════════════════════════
# Schemas (Pydantic)
# ══════════════════════════════════════════════════════════════════

class EmailLogCreate(BaseModel):
    subject: str
    body_text: Optional[str] = None
    body_html: Optional[str] = None
    direction: str = "outbound"
    channel: str = "ionos"
    sequence_type: Optional[str] = None
    sequence_step: Optional[int] = None
    from_address: Optional[str] = None
    to_address: Optional[str] = None
    message_id: Optional[str] = None
    in_reply_to: Optional[str] = None
    instantly_lead_id: Optional[str] = None
    instantly_campaign_id: Optional[str] = None
    send_error: Optional[str] = None


class TaskCreate(BaseModel):
    title: str
    description: Optional[str] = None
    task_type: str = "manual"
    priority: str = "normal"
    due_date: Optional[date] = None
    source: str = "system"
    source_email_id: Optional[UUID] = None


class TaskUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    priority: Optional[str] = None
    due_date: Optional[date] = None


class StatusUpdate(BaseModel):
    contact_status: str


class SourceUpdate(BaseModel):
    source_override: str


class SendRequest(BaseModel):
    item_ids: list[UUID]
    max_per_mailbox: int = 30


# ══════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════

def _serialize_email(e: CandidateEmail) -> dict:
    return {
        "id": str(e.id),
        "candidate_id": str(e.candidate_id),
        "subject": e.subject,
        "body_text": e.body_text,
        "body_html": e.body_html,
        "direction": e.direction,
        "channel": e.channel,
        "sequence_type": e.sequence_type,
        "sequence_step": e.sequence_step,
        "from_address": e.from_address,
        "to_address": e.to_address,
        "message_id": e.message_id,
        "in_reply_to": e.in_reply_to,
        "instantly_lead_id": e.instantly_lead_id,
        "send_error": e.send_error,
        "created_at": e.created_at.isoformat() if e.created_at else None,
    }


def _serialize_task(t: CandidateTask) -> dict:
    return {
        "id": str(t.id),
        "candidate_id": str(t.candidate_id),
        "title": t.title,
        "description": t.description,
        "task_type": t.task_type,
        "status": t.status,
        "priority": t.priority,
        "due_date": t.due_date.isoformat() if t.due_date else None,
        "completed_at": t.completed_at.isoformat() if t.completed_at else None,
        "source": t.source,
        "source_email_id": str(t.source_email_id) if t.source_email_id else None,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
    }


def _serialize_batch(b: OutreachBatch) -> dict:
    return {
        "id": str(b.id),
        "batch_date": b.batch_date.isoformat() if b.batch_date else None,
        "total_candidates": b.total_candidates,
        "approved_count": b.approved_count,
        "sent_count": b.sent_count,
        "skipped_count": b.skipped_count,
        "status": b.status,
        "max_per_mailbox": b.max_per_mailbox,
        "created_at": b.created_at.isoformat() if b.created_at else None,
    }


def _serialize_item(i: OutreachItem) -> dict:
    cand = i.candidate
    return {
        "id": str(i.id),
        "batch_id": str(i.batch_id),
        "candidate_id": str(i.candidate_id),
        "candidate_name": f"{cand.first_name} {cand.last_name}" if cand else None,
        "candidate_email": cand.email if cand else None,
        "candidate_city": cand.city if cand else None,
        "candidate_gender": cand.gender if cand else None,
        "candidate_source": cand.source if cand else None,
        "campaign_type": i.campaign_type,
        "source_override": i.source_override,
        "status": i.status,
        "instantly_lead_id": i.instantly_lead_id,
        "sent_at": i.sent_at.isoformat() if i.sent_at else None,
        "send_error": i.send_error,
        "created_at": i.created_at.isoformat() if i.created_at else None,
    }


# ══════════════════════════════════════════════════════════════════
# 1. Nicht-Erreicht + Status
# ══════════════════════════════════════════════════════════════════

@router.post("/candidates/{candidate_id}/not-reached")
async def mark_not_reached(candidate_id: UUID, db: AsyncSession = Depends(get_db)):
    """Markiert Kandidat als 'nicht erreicht' und startet E-Mail-Sequenz.

    Setzt Status auf 'email_sequenz_aktiv' und triggert den n8n Webhook
    serverseitig (vermeidet CORS-Probleme im Browser).
    """
    candidate = await db.get(Candidate, candidate_id)
    if not candidate:
        return {"error": "Kandidat nicht gefunden"}, 404

    candidate.contact_status = "email_sequenz_aktiv"
    candidate.last_contact = datetime.utcnow()
    await db.flush()

    logger.info(f"Kandidat {candidate.first_name} {candidate.last_name} ({candidate_id}) als 'nicht erreicht' markiert")

    # n8n Webhook serverseitig triggern (kein CORS-Problem)
    n8n_webhook_url = "https://n8n-production-aa9c.up.railway.app/webhook/nicht-erreicht"
    webhook_payload = {
        "candidate_id": str(candidate_id),
        "first_name": candidate.first_name,
        "last_name": candidate.last_name,
        "email": candidate.email,
        "gender": candidate.gender or "",
        "source": candidate.source or "einem Jobportal",
        "city": candidate.city or "",
    }
    n8n_ok = False
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(n8n_webhook_url, json=webhook_payload)
            n8n_ok = resp.status_code < 400
            if not n8n_ok:
                logger.error(f"n8n Webhook fehlgeschlagen: HTTP {resp.status_code} - {resp.text[:200]}")
    except Exception as exc:
        logger.error(f"n8n Webhook Fehler: {exc}")

    return {
        "candidate_id": str(candidate_id),
        "contact_status": "email_sequenz_aktiv",
        "first_name": candidate.first_name,
        "last_name": candidate.last_name,
        "email": candidate.email,
        "gender": candidate.gender,
        "source": candidate.source,
        "n8n_triggered": n8n_ok,
        "message": f"Sequenz gestartet fuer {candidate.first_name} {candidate.last_name}",
    }


@router.patch("/candidates/{candidate_id}/status")
async def update_contact_status(
    candidate_id: UUID,
    data: StatusUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Setzt contact_status fuer einen Kandidaten (n8n / manuell)."""
    candidate = await db.get(Candidate, candidate_id)
    if not candidate:
        return {"error": "Kandidat nicht gefunden"}, 404

    old_status = getattr(candidate, "contact_status", None)
    candidate.contact_status = data.contact_status
    await db.flush()

    return {
        "candidate_id": str(candidate_id),
        "previous_status": old_status,
        "new_status": data.contact_status,
    }


# ══════════════════════════════════════════════════════════════════
# 2. E-Mail-Log
# ══════════════════════════════════════════════════════════════════

@router.post("/candidates/{candidate_id}/emails")
async def log_email(
    candidate_id: UUID,
    data: EmailLogCreate,
    db: AsyncSession = Depends(get_db),
):
    """Loggt eine E-Mail (gesendet oder empfangen) fuer einen Kandidaten."""
    candidate = await db.get(Candidate, candidate_id)
    if not candidate:
        return {"error": "Kandidat nicht gefunden"}, 404

    email = CandidateEmail(
        candidate_id=candidate_id,
        subject=data.subject,
        body_text=data.body_text,
        body_html=data.body_html,
        direction=data.direction,
        channel=data.channel,
        sequence_type=data.sequence_type,
        sequence_step=data.sequence_step,
        from_address=data.from_address,
        to_address=data.to_address,
        message_id=data.message_id,
        in_reply_to=data.in_reply_to,
        instantly_lead_id=data.instantly_lead_id,
        instantly_campaign_id=data.instantly_campaign_id,
        send_error=data.send_error,
    )
    db.add(email)
    await db.flush()

    logger.info(f"E-Mail geloggt: {data.direction} fuer Kandidat {candidate_id}, Betreff: {data.subject}")

    return _serialize_email(email)


@router.get("/candidates/{candidate_id}/emails")
async def list_emails(
    candidate_id: UUID,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    direction: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """Listet E-Mails fuer einen Kandidaten (paginiert)."""
    query = (
        select(CandidateEmail)
        .where(CandidateEmail.candidate_id == candidate_id)
        .order_by(CandidateEmail.created_at.desc())
    )
    if direction:
        query = query.where(CandidateEmail.direction == direction)

    query = query.offset(offset).limit(limit)
    result = await db.execute(query)
    emails = result.scalars().all()

    # Total count
    count_q = select(func.count()).select_from(CandidateEmail).where(
        CandidateEmail.candidate_id == candidate_id
    )
    if direction:
        count_q = count_q.where(CandidateEmail.direction == direction)
    total = (await db.execute(count_q)).scalar() or 0

    return {
        "items": [_serialize_email(e) for e in emails],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/candidates/{candidate_id}/has-reply")
async def has_reply(
    candidate_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Prueft ob ein Kandidat eine eingehende E-Mail (Antwort) gesendet hat."""
    count_q = (
        select(func.count())
        .select_from(CandidateEmail)
        .where(
            CandidateEmail.candidate_id == candidate_id,
            CandidateEmail.direction == "inbound",
        )
    )
    count = (await db.execute(count_q)).scalar() or 0
    return {"has_reply": count > 0, "reply_count": count}


# ══════════════════════════════════════════════════════════════════
# 3. Aufgaben
# ══════════════════════════════════════════════════════════════════

@router.post("/candidates/{candidate_id}/tasks")
async def create_task(
    candidate_id: UUID,
    data: TaskCreate,
    db: AsyncSession = Depends(get_db),
):
    """Erstellt eine Aufgabe fuer einen Kandidaten."""
    candidate = await db.get(Candidate, candidate_id)
    if not candidate:
        return {"error": "Kandidat nicht gefunden"}, 404

    task = CandidateTask(
        candidate_id=candidate_id,
        title=data.title,
        description=data.description,
        task_type=data.task_type,
        priority=data.priority,
        due_date=data.due_date,
        source=data.source,
        source_email_id=data.source_email_id,
    )
    db.add(task)
    await db.flush()

    logger.info(f"Aufgabe erstellt: '{data.title}' fuer Kandidat {candidate_id}")

    return _serialize_task(task)


@router.get("/candidates/{candidate_id}/tasks")
async def list_tasks(
    candidate_id: UUID,
    status: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Listet Aufgaben fuer einen Kandidaten."""
    query = (
        select(CandidateTask)
        .where(CandidateTask.candidate_id == candidate_id)
        .order_by(CandidateTask.created_at.desc())
        .limit(limit)
    )
    if status:
        query = query.where(CandidateTask.status == status)

    result = await db.execute(query)
    tasks = result.scalars().all()
    return {"items": [_serialize_task(t) for t in tasks], "total": len(tasks)}


@router.patch("/tasks/{task_id}")
async def update_task(
    task_id: UUID,
    data: TaskUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Aktualisiert eine Aufgabe (erledigen, Datum aendern, etc.)."""
    task = await db.get(CandidateTask, task_id)
    if not task:
        return {"error": "Aufgabe nicht gefunden"}, 404

    if data.title is not None:
        task.title = data.title
    if data.description is not None:
        task.description = data.description
    if data.status is not None:
        task.status = data.status
        if data.status == "done":
            task.completed_at = datetime.utcnow()
    if data.priority is not None:
        task.priority = data.priority
    if data.due_date is not None:
        task.due_date = data.due_date

    await db.flush()
    return _serialize_task(task)


# ══════════════════════════════════════════════════════════════════
# 4. Outreach / Rundmail
# ══════════════════════════════════════════════════════════════════

@router.post("/outreach/prepare-daily")
async def prepare_daily_batch(
    max_candidates: int = Query(120, ge=1, le=500),
    max_per_mailbox: int = Query(30, ge=10, le=60),
    db: AsyncSession = Depends(get_db),
):
    """Bereitet den Tages-Batch fuer die Rundmail vor.

    Wird von n8n taeglich um 6:00 aufgerufen.
    Laedt die naechsten X Finance-Kandidaten die noch nicht kontaktiert wurden.
    """
    today = date.today()

    # Pruefen ob heute schon ein Batch existiert
    existing = await db.execute(
        select(OutreachBatch).where(OutreachBatch.batch_date == today)
    )
    if existing.scalars().first():
        return {"error": "Batch fuer heute existiert bereits", "batch_date": today.isoformat()}, 400

    # Finance-Kandidaten laden die noch nicht kontaktiert wurden
    # Filter: hat E-Mail, hat classification_data, kein aktiver Kontakt
    query = text("""
        SELECT id, first_name, last_name, email, gender, source, city,
               classification_data->>'primary_role' as primary_role
        FROM candidates
        WHERE email IS NOT NULL
          AND email != ''
          AND deleted_at IS NULL
          AND hidden = FALSE
          AND classification_data IS NOT NULL
          AND classification_data->>'primary_role' IS NOT NULL
          AND (contact_status IS NULL OR contact_status NOT IN (
              'email_sequenz_aktiv', 'kein_interesse', 'geantwortet',
              'rundmail_gesendet', 'abgemeldet', 'bounce'
          ))
          AND id NOT IN (SELECT candidate_id FROM outreach_items)
        ORDER BY created_at ASC
        LIMIT :max_candidates
    """)
    result = await db.execute(query, {"max_candidates": max_candidates})
    candidates = result.fetchall()

    if not candidates:
        return {"message": "Keine neuen Finance-Kandidaten fuer Rundmail", "total": 0}

    # Batch erstellen
    batch = OutreachBatch(
        batch_date=today,
        total_candidates=len(candidates),
        max_per_mailbox=max_per_mailbox,
    )
    db.add(batch)
    await db.flush()

    # Items erstellen
    for row in candidates:
        source = row.source or ""
        campaign_type = "bekannt" if source.lower() == "bestand" else "erstkontakt"

        item = OutreachItem(
            batch_id=batch.id,
            candidate_id=row.id,
            campaign_type=campaign_type,
            source_override=source if source else None,
        )
        db.add(item)

    await db.flush()
    logger.info(f"Tages-Batch erstellt: {len(candidates)} Kandidaten fuer {today}")

    return {
        "batch_id": str(batch.id),
        "batch_date": today.isoformat(),
        "total_candidates": len(candidates),
        "max_per_mailbox": max_per_mailbox,
    }


@router.get("/outreach/daily")
async def get_daily_report(
    batch_date: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """Tages-Report abrufen (fuer MT Frontend Rundmail-Tab)."""
    target_date = date.fromisoformat(batch_date) if batch_date else date.today()

    # Batch laden
    result = await db.execute(
        select(OutreachBatch).where(OutreachBatch.batch_date == target_date)
    )
    batch = result.scalars().first()
    if not batch:
        return {"batch": None, "items": [], "total": 0}

    # Items mit Kandidaten-Daten laden
    items_result = await db.execute(
        select(OutreachItem)
        .where(OutreachItem.batch_id == batch.id)
        .order_by(OutreachItem.created_at.asc())
    )
    items = items_result.scalars().all()

    return {
        "batch": _serialize_batch(batch),
        "items": [_serialize_item(i) for i in items],
        "total": len(items),
    }


@router.patch("/outreach/items/{item_id}/source")
async def update_item_source(
    item_id: UUID,
    data: SourceUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Quelle fuer ein Outreach-Item aktualisieren (Dropdown im Frontend)."""
    item = await db.get(OutreachItem, item_id)
    if not item:
        return {"error": "Item nicht gefunden"}, 404

    item.source_override = data.source_override
    # Kampagnen-Typ automatisch ableiten
    item.campaign_type = "bekannt" if data.source_override.lower() == "bestand" else "erstkontakt"
    await db.flush()

    return {
        "id": str(item.id),
        "source_override": item.source_override,
        "campaign_type": item.campaign_type,
    }


@router.post("/outreach/send")
async def send_selected(
    data: SendRequest,
    db: AsyncSession = Depends(get_db),
):
    """Markiert Items als 'approved' und triggert n8n Webhook fuer Instantly-Versand."""
    approved_count = 0
    approved_items = []

    for item_id in data.item_ids:
        item = await db.get(OutreachItem, item_id)
        if item and item.status == "prepared":
            item.status = "approved"
            approved_count += 1
            approved_items.append(item)

    await db.flush()
    logger.info(f"Outreach: {approved_count} Items als 'approved' markiert")

    # Batch-ID ermitteln + approved_count aktualisieren
    batch_id = str(approved_items[0].batch_id) if approved_items else ""
    if approved_items:
        batch = await db.get(OutreachBatch, approved_items[0].batch_id)
        if batch:
            batch.approved_count = approved_count
            batch.max_per_mailbox = data.max_per_mailbox
            await db.flush()

    # Kandidaten-Daten fuer n8n sammeln
    candidates_payload = []
    for item in approved_items:
        cand = item.candidate
        if cand:
            candidates_payload.append({
                "candidate_id": str(cand.id),
                "email": cand.email,
                "first_name": cand.first_name or "",
                "last_name": cand.last_name or "",
                "gender": cand.gender or "",
                "source": item.source_override or cand.source or "",
                "campaign_type": item.campaign_type,
            })

    # n8n Webhook triggern (async, blockiert nicht den Response)
    n8n_ok = False
    if candidates_payload:
        n8n_webhook_url = "https://n8n-production-aa9c.up.railway.app/webhook/rundmail-senden"
        webhook_payload = {
            "batch_id": batch_id,
            "candidates": candidates_payload,
            "max_per_mailbox": data.max_per_mailbox,
        }
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(n8n_webhook_url, json=webhook_payload)
                n8n_ok = resp.status_code < 400
                if not n8n_ok:
                    logger.error(f"n8n Rundmail-Webhook fehlgeschlagen: HTTP {resp.status_code} - {resp.text[:200]}")
        except Exception as exc:
            logger.error(f"n8n Rundmail-Webhook Fehler: {exc}")

    return {
        "approved_count": approved_count,
        "total_requested": len(data.item_ids),
        "n8n_triggered": n8n_ok,
    }


@router.post("/outreach/skip-daily")
async def skip_daily(db: AsyncSession = Depends(get_db)):
    """Heutige Rundmail ueberspringen — Batch als 'cancelled' markieren."""
    from datetime import date

    today = date.today()
    result = await db.execute(
        text("SELECT id FROM outreach_batches WHERE batch_date = :today ORDER BY created_at DESC LIMIT 1"),
        {"today": today},
    )
    batch_row = result.fetchone()

    if not batch_row:
        return {"skipped": False, "reason": "Kein Batch fuer heute vorhanden"}

    batch = await db.get(OutreachBatch, batch_row[0])
    if batch:
        batch.status = "cancelled"
        await db.flush()
        logger.info(f"Outreach: Tages-Batch {batch.id} uebersprungen")

    return {"skipped": True, "batch_date": str(today)}


@router.post("/outreach/mark-sent")
async def mark_batch_sent(
    data: dict,
    db: AsyncSession = Depends(get_db),
):
    """Markiert Items eines Batches als 'sent' — wird von n8n nach Instantly-Upload aufgerufen.

    Body: {"batch_id": "uuid"}
    Setzt:
    - Alle approved Items auf status='sent' + sent_at=now()
    - Kandidaten contact_status auf 'rundmail_gesendet'
    - Batch sent_count + status='sent'
    """
    batch_id = data.get("batch_id")
    if not batch_id:
        return JSONResponse(status_code=400, content={"error": "batch_id fehlt"})

    # Batch laden
    batch = await db.get(OutreachBatch, batch_id)
    if not batch:
        return JSONResponse(status_code=404, content={"error": "Batch nicht gefunden"})

    # Alle approved Items dieses Batches auf 'sent' setzen
    now = datetime.utcnow()
    items_result = await db.execute(
        select(OutreachItem).where(
            OutreachItem.batch_id == batch.id,
            OutreachItem.status == "approved",
        )
    )
    items = items_result.scalars().all()

    sent_count = 0
    candidate_ids = []
    for item in items:
        item.status = "sent"
        item.sent_at = now
        sent_count += 1
        candidate_ids.append(item.candidate_id)

    # Kandidaten contact_status auf 'rundmail_gesendet' setzen
    if candidate_ids:
        await db.execute(
            update(Candidate)
            .where(Candidate.id.in_(candidate_ids))
            .values(contact_status="rundmail_gesendet")
        )

    # Batch aktualisieren
    batch.sent_count = (batch.sent_count or 0) + sent_count

    # Status: "partial" wenn noch prepared Items uebrig, sonst "sent"
    remaining_result = await db.execute(
        select(func.count()).where(
            OutreachItem.batch_id == batch.id,
            OutreachItem.status == "prepared",
        )
    )
    remaining_prepared = remaining_result.scalar() or 0
    batch.status = "partial" if remaining_prepared > 0 else "sent"
    await db.flush()

    logger.info(f"Outreach mark-sent: {sent_count} Items als 'sent', {len(candidate_ids)} Kandidaten auf 'rundmail_gesendet'")

    return {
        "batch_id": str(batch.id),
        "sent_count": sent_count,
        "candidates_updated": len(candidate_ids),
    }


# ══════════════════════════════════════════════════════════════════
# 5. Outreach Stats + Batches + Progress
# ══════════════════════════════════════════════════════════════════

@router.get("/outreach/stats")
async def outreach_stats(db: AsyncSession = Depends(get_db)):
    """Gesamtstatistiken der Rundmail-Aktion."""
    result = await db.execute(text("""
        SELECT
            COUNT(*) as total_batches,
            COALESCE(SUM(total_candidates), 0) as total_processed,
            COALESCE(SUM(sent_count), 0) as total_sent,
            COALESCE(SUM(approved_count), 0) as total_approved,
            COALESCE(SUM(skipped_count), 0) as total_skipped,
            MAX(batch_date) as latest_batch_date
        FROM outreach_batches
    """))
    row = result.fetchone()

    # Verbleibende Kandidaten
    remaining = await db.execute(text("""
        SELECT COUNT(*) FROM candidates
        WHERE email IS NOT NULL AND email != ''
          AND deleted_at IS NULL AND hidden = FALSE
          AND classification_data IS NOT NULL
          AND classification_data->>'primary_role' IS NOT NULL
          AND (contact_status IS NULL OR contact_status NOT IN (
              'email_sequenz_aktiv', 'kein_interesse', 'geantwortet',
              'rundmail_gesendet', 'abgemeldet', 'bounce'
          ))
          AND id NOT IN (SELECT candidate_id FROM outreach_items)
    """))
    remaining_count = remaining.scalar() or 0

    return {
        "total_batches": row[0] if row else 0,
        "total_candidates_processed": row[1] if row else 0,
        "total_sent": row[2] if row else 0,
        "total_approved": row[3] if row else 0,
        "total_skipped": row[4] if row else 0,
        "candidates_remaining": remaining_count,
        "latest_batch_date": row[5].isoformat() if row and row[5] else None,
    }


@router.get("/outreach/batches")
async def list_batches(
    limit: int = Query(30, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Alle bisherigen Tages-Batches."""
    result = await db.execute(
        select(OutreachBatch)
        .order_by(OutreachBatch.batch_date.desc())
        .limit(limit)
    )
    batches = result.scalars().all()
    return {"batches": [_serialize_batch(b) for b in batches], "total": len(batches)}


@router.get("/outreach/progress")
async def outreach_progress(db: AsyncSession = Depends(get_db)):
    """Fortschritt der gesamten Rundmail-Aktion."""
    result = await db.execute(text("""
        SELECT
            COUNT(*) FILTER (WHERE classification_data IS NOT NULL AND classification_data->>'primary_role' IS NOT NULL) as total_finance,
            COUNT(*) FILTER (WHERE classification_data IS NOT NULL AND classification_data->>'primary_role' IS NOT NULL AND email IS NOT NULL AND email != '') as with_email,
            COUNT(*) FILTER (WHERE classification_data IS NOT NULL AND classification_data->>'primary_role' IS NOT NULL AND gender IS NOT NULL) as with_gender,
            COUNT(*) FILTER (WHERE classification_data IS NOT NULL AND classification_data->>'primary_role' IS NOT NULL AND gender IS NULL) as without_gender,
            COUNT(*) FILTER (WHERE contact_status = 'email_sequenz_aktiv') as sequenz_aktiv,
            COUNT(*) FILTER (WHERE contact_status = 'kein_interesse') as kein_interesse,
            COUNT(*) FILTER (WHERE contact_status = 'geantwortet') as geantwortet,
            COUNT(*) FILTER (WHERE contact_status = 'rundmail_gesendet') as rundmail_gesendet
        FROM candidates
        WHERE deleted_at IS NULL AND hidden = FALSE
    """))
    row = result.fetchone()

    total_finance = row[0] if row else 0
    already_contacted = (row[4] or 0) + (row[5] or 0) + (row[6] or 0) + (row[7] or 0)

    return {
        "total_finance_candidates": total_finance,
        "candidates_with_email": row[1] if row else 0,
        "candidates_with_gender": row[2] if row else 0,
        "candidates_without_gender": row[3] if row else 0,
        "candidates_sequenz_aktiv": row[4] if row else 0,
        "candidates_kein_interesse": row[5] if row else 0,
        "candidates_geantwortet": row[6] if row else 0,
        "candidates_rundmail_gesendet": row[7] if row else 0,
        "candidates_already_contacted": already_contacted,
        "candidates_remaining": total_finance - already_contacted,
        "progress_percentage": round((already_contacted / total_finance * 100), 1) if total_finance > 0 else 0,
    }


# ══════════════════════════════════════════════════════════════════
# 6. Sequenz-Kontrolle
# ══════════════════════════════════════════════════════════════════

@router.get("/sequences/active")
async def active_sequences(db: AsyncSession = Depends(get_db)):
    """Alle Kandidaten mit aktiver E-Mail-Sequenz."""
    result = await db.execute(
        select(Candidate)
        .where(Candidate.contact_status == "email_sequenz_aktiv")
        .order_by(Candidate.last_contact.desc())
    )
    candidates = result.scalars().all()

    items = []
    for c in candidates:
        # Letzte E-Mail zaehlen
        email_count = await db.execute(
            select(func.count()).select_from(CandidateEmail).where(
                CandidateEmail.candidate_id == c.id,
                CandidateEmail.direction == "outbound",
            )
        )
        count = email_count.scalar() or 0

        last_email = await db.execute(
            select(CandidateEmail)
            .where(CandidateEmail.candidate_id == c.id, CandidateEmail.direction == "outbound")
            .order_by(CandidateEmail.created_at.desc())
            .limit(1)
        )
        last = last_email.scalars().first()

        items.append({
            "candidate_id": str(c.id),
            "first_name": c.first_name,
            "last_name": c.last_name,
            "email": c.email,
            "contact_status": c.contact_status,
            "emails_sent": count,
            "last_email_at": last.created_at.isoformat() if last else None,
        })

    return {"active_count": len(items), "candidates": items}


@router.get("/sequences/{candidate_id}")
async def sequence_status(candidate_id: UUID, db: AsyncSession = Depends(get_db)):
    """Detaillierter Sequenz-Status fuer einen Kandidaten."""
    candidate = await db.get(Candidate, candidate_id)
    if not candidate:
        return {"error": "Kandidat nicht gefunden"}, 404

    # E-Mails laden
    emails_result = await db.execute(
        select(CandidateEmail)
        .where(CandidateEmail.candidate_id == candidate_id)
        .order_by(CandidateEmail.created_at.asc())
    )
    emails = emails_result.scalars().all()

    # Aufgaben laden
    tasks_result = await db.execute(
        select(CandidateTask)
        .where(CandidateTask.candidate_id == candidate_id)
        .order_by(CandidateTask.created_at.desc())
    )
    tasks = tasks_result.scalars().all()

    return {
        "candidate_id": str(candidate_id),
        "first_name": candidate.first_name,
        "last_name": candidate.last_name,
        "email": candidate.email,
        "contact_status": getattr(candidate, "contact_status", None),
        "gender": candidate.gender,
        "source": candidate.source,
        "sequence_active": getattr(candidate, "contact_status", None) == "email_sequenz_aktiv",
        "emails": [_serialize_email(e) for e in emails],
        "tasks": [_serialize_task(t) for t in tasks],
    }


@router.post("/sequences/{candidate_id}/stop")
async def stop_sequence(candidate_id: UUID, db: AsyncSession = Depends(get_db)):
    """Sequenz manuell stoppen."""
    candidate = await db.get(Candidate, candidate_id)
    if not candidate:
        return {"error": "Kandidat nicht gefunden"}, 404

    old_status = getattr(candidate, "contact_status", None)
    candidate.contact_status = "sequenz_manuell_gestoppt"
    await db.flush()

    logger.info(f"Sequenz gestoppt fuer {candidate.first_name} {candidate.last_name}")

    return {
        "candidate_id": str(candidate_id),
        "previous_status": old_status,
        "new_status": "sequenz_manuell_gestoppt",
        "message": f"Sequenz gestoppt fuer {candidate.first_name} {candidate.last_name}",
    }


# ══════════════════════════════════════════════════════════════════
# 7. System Health + Activity Log
# ══════════════════════════════════════════════════════════════════

@router.get("/health")
async def email_health(db: AsyncSession = Depends(get_db)):
    """System-Health-Check fuer E-Mail-Automatisierung."""
    today = date.today()

    result = await db.execute(text("""
        SELECT
            (SELECT COUNT(*) FROM candidates WHERE email IS NOT NULL AND email != '' AND deleted_at IS NULL) as total_with_email,
            (SELECT COUNT(*) FROM candidates WHERE contact_status = 'email_sequenz_aktiv') as active_sequences,
            (SELECT COUNT(*) FROM candidate_emails WHERE created_at >= CURRENT_DATE AND direction = 'outbound') as sent_today,
            (SELECT COUNT(*) FROM candidate_emails WHERE created_at >= CURRENT_DATE - INTERVAL '7 days' AND direction = 'outbound') as sent_7d,
            (SELECT COUNT(*) FROM candidate_emails WHERE created_at >= CURRENT_DATE AND send_error IS NOT NULL) as errors_today,
            (SELECT COUNT(*) FROM candidate_tasks WHERE status = 'open') as pending_tasks,
            (SELECT COUNT(*) FROM candidate_tasks WHERE status = 'open' AND due_date < CURRENT_DATE) as overdue_tasks
    """))
    row = result.fetchone()

    errors_today = row[4] if row else 0
    warnings = []
    if errors_today > 10:
        warnings.append(f"{errors_today} E-Mail-Fehler heute — SMTP/Instantly pruefen")

    overdue = row[6] if row else 0
    if overdue > 5:
        warnings.append(f"{overdue} ueberfaellige Aufgaben")

    status = "healthy"
    if warnings:
        status = "warning"
    if errors_today > 20:
        status = "error"

    return {
        "status": status,
        "total_candidates_with_email": row[0] if row else 0,
        "active_sequences": row[1] if row else 0,
        "emails_sent_today": row[2] if row else 0,
        "emails_sent_7d": row[3] if row else 0,
        "errors_today": errors_today,
        "pending_tasks": row[5] if row else 0,
        "overdue_tasks": overdue,
        "warnings": warnings,
    }


@router.get("/candidates/{candidate_id}/activity-log")
async def activity_log(candidate_id: UUID, db: AsyncSession = Depends(get_db)):
    """Vollstaendiger Aktivitaetslog eines Kandidaten (E-Mails + Aufgaben + Outreach)."""
    candidate = await db.get(Candidate, candidate_id)
    if not candidate:
        return {"error": "Kandidat nicht gefunden"}, 404

    entries = []

    # E-Mails
    emails_result = await db.execute(
        select(CandidateEmail)
        .where(CandidateEmail.candidate_id == candidate_id)
        .order_by(CandidateEmail.created_at.asc())
    )
    for e in emails_result.scalars().all():
        entries.append({
            "timestamp": e.created_at.isoformat() if e.created_at else None,
            "type": f"email_{e.direction}",
            "summary": f"E-Mail {'Gesendet' if e.direction == 'outbound' else 'Empfangen'}: {e.subject}",
            "details": {
                "email_id": str(e.id),
                "sequence_step": e.sequence_step,
                "channel": e.channel,
            },
        })

    # Aufgaben
    tasks_result = await db.execute(
        select(CandidateTask)
        .where(CandidateTask.candidate_id == candidate_id)
        .order_by(CandidateTask.created_at.asc())
    )
    for t in tasks_result.scalars().all():
        entries.append({
            "timestamp": t.created_at.isoformat() if t.created_at else None,
            "type": "task_created",
            "summary": f"Aufgabe erstellt: {t.title}",
            "details": {
                "task_id": str(t.id),
                "task_type": t.task_type,
                "status": t.status,
                "due_date": t.due_date.isoformat() if t.due_date else None,
            },
        })

    # Outreach Items
    items_result = await db.execute(
        select(OutreachItem)
        .where(OutreachItem.candidate_id == candidate_id)
        .order_by(OutreachItem.created_at.asc())
    )
    for i in items_result.scalars().all():
        entries.append({
            "timestamp": i.created_at.isoformat() if i.created_at else None,
            "type": "outreach",
            "summary": f"Rundmail: {i.campaign_type} — Status: {i.status}",
            "details": {
                "item_id": str(i.id),
                "campaign_type": i.campaign_type,
                "status": i.status,
                "sent_at": i.sent_at.isoformat() if i.sent_at else None,
            },
        })

    # Chronologisch sortieren
    entries.sort(key=lambda x: x["timestamp"] or "")

    return {
        "candidate_id": str(candidate_id),
        "first_name": candidate.first_name,
        "last_name": candidate.last_name,
        "total_entries": len(entries),
        "entries": entries,
    }


# ══════════════════════════════════════════════════════════════════
# 8. Gender-Klassifizierung (fuer n8n / Scripts)
# ══════════════════════════════════════════════════════════════════

@router.get("/gender/status")
async def gender_status(db: AsyncSession = Depends(get_db)):
    """Status der Gender-Klassifizierung fuer Finance-Kandidaten."""
    result = await db.execute(text("""
        SELECT
            COUNT(*) FILTER (WHERE classification_data IS NOT NULL
                AND classification_data->>'primary_role' IS NOT NULL
                AND classification_data->>'primary_role' != '') as total_finance,
            COUNT(*) FILTER (WHERE classification_data IS NOT NULL
                AND classification_data->>'primary_role' IS NOT NULL
                AND classification_data->>'primary_role' != ''
                AND gender IS NOT NULL AND gender != '') as with_gender,
            COUNT(*) FILTER (WHERE classification_data IS NOT NULL
                AND classification_data->>'primary_role' IS NOT NULL
                AND classification_data->>'primary_role' != ''
                AND (gender IS NULL OR gender = '')) as without_gender,
            COUNT(*) FILTER (WHERE classification_data IS NOT NULL
                AND classification_data->>'primary_role' IS NOT NULL
                AND classification_data->>'primary_role' != ''
                AND email IS NOT NULL AND email != ''
                AND (gender IS NULL OR gender = '')) as without_gender_with_email
        FROM candidates
        WHERE deleted_at IS NULL AND (hidden = FALSE OR hidden IS NULL)
    """))
    row = result.fetchone()

    total = row[0] if row else 0
    with_gender = row[1] if row else 0
    without_gender = row[2] if row else 0

    return {
        "total_finance_candidates": total,
        "with_gender": with_gender,
        "without_gender": without_gender,
        "without_gender_with_email": row[3] if row else 0,
        "progress_percent": round((with_gender / total * 100), 1) if total > 0 else 0,
        "status": "complete" if without_gender == 0 else "pending",
    }


@router.get("/gender/candidates-without")
async def candidates_without_gender(
    db: AsyncSession = Depends(get_db),
    limit: int = 100,
    offset: int = 0,
):
    """Liste der Finance-Kandidaten ohne Gender — fuer n8n Batch-Verarbeitung."""
    result = await db.execute(text("""
        SELECT id, first_name, last_name, email
        FROM candidates
        WHERE classification_data IS NOT NULL
          AND classification_data->>'primary_role' IS NOT NULL
          AND classification_data->>'primary_role' != ''
          AND (gender IS NULL OR gender = '')
          AND deleted_at IS NULL
          AND (hidden = FALSE OR hidden IS NULL)
        ORDER BY created_at ASC
        LIMIT :limit OFFSET :offset
    """), {"limit": limit, "offset": offset})

    candidates_list = []
    for row in result.fetchall():
        candidates_list.append({
            "id": str(row[0]),
            "first_name": row[1],
            "last_name": row[2],
            "email": row[3],
        })

    return {
        "candidates": candidates_list,
        "count": len(candidates_list),
        "limit": limit,
        "offset": offset,
    }


@router.patch("/gender/update/{candidate_id}")
async def update_gender(
    candidate_id: UUID,
    data: dict,
    db: AsyncSession = Depends(get_db),
):
    """Gender eines Kandidaten setzen — wird von n8n nach OpenAI-Klassifizierung aufgerufen.

    Body: {"gender": "Herr"} oder {"gender": "Frau"} oder {"gender": null}
    """
    candidate = await db.get(Candidate, candidate_id)
    if not candidate:
        return JSONResponse(status_code=404, content={"error": "Kandidat nicht gefunden"})

    new_gender = data.get("gender")

    # Nur gueltige Werte erlauben
    if new_gender is not None and new_gender not in ("Herr", "Frau"):
        return JSONResponse(status_code=400, content={
            "error": f"Ungueltiger Gender-Wert: '{new_gender}'. Erlaubt: 'Herr', 'Frau', null"
        })

    # Null -> "Unbekannt" um Endlosschleife zu vermeiden
    if new_gender is None:
        new_gender = "Unbekannt"

    old_gender = candidate.gender
    candidate.gender = new_gender
    await db.flush()

    logger.info(f"Gender aktualisiert: {candidate.first_name} {candidate.last_name}: {old_gender} -> {new_gender}")

    return {
        "candidate_id": str(candidate_id),
        "first_name": candidate.first_name,
        "last_name": candidate.last_name,
        "old_gender": old_gender,
        "new_gender": new_gender,
    }


@router.post("/gender/batch-update")
async def batch_update_gender(
    data: dict,
    db: AsyncSession = Depends(get_db),
):
    """Batch-Update Gender fuer mehrere Kandidaten — n8n sendet Ergebnisse gesammelt.

    Body: {"updates": [{"id": "uuid", "gender": "Herr"}, {"id": "uuid", "gender": "Frau"}, ...]}
    """
    updates = data.get("updates", [])
    if not updates:
        return {"error": "Keine Updates angegeben", "updated": 0}

    updated = 0
    errors = []

    for item in updates:
        cid = item.get("id")
        gender = item.get("gender")

        if gender is not None and gender not in ("Herr", "Frau"):
            errors.append({"id": cid, "error": f"Ungueltiger Wert: '{gender}'"})
            continue

        # Null/None -> "Unbekannt" damit der Kandidat nicht erneut vom
        # candidates-without Endpoint zurueckgegeben wird (Endlosschleife)
        if gender is None:
            gender = "Unbekannt"

        try:
            candidate = await db.get(Candidate, cid)
            if candidate:
                candidate.gender = gender
                updated += 1
            else:
                errors.append({"id": cid, "error": "Nicht gefunden"})
        except Exception as e:
            errors.append({"id": cid, "error": str(e)})

    await db.flush()
    logger.info(f"Gender Batch-Update: {updated}/{len(updates)} erfolgreich")

    return {
        "updated": updated,
        "total_requested": len(updates),
        "errors": errors,
    }


# ══════════════════════════════════════════════════════════════════
# Instantly Webhook Events (email_sent, bounced)
# ══════════════════════════════════════════════════════════════════


class InstantlyEmailLog(BaseModel):
    candidate_id: Optional[str] = None
    subject: Optional[str] = None
    body: Optional[str] = None
    direction: str = "outbound"
    from_address: Optional[str] = None
    to_address: Optional[str] = None
    source: str = "instantly"


@router.post("/emails/log")
async def log_instantly_email(
    data: InstantlyEmailLog,
    db: AsyncSession = Depends(get_db),
):
    """Loggt eine von Instantly gesendete E-Mail fuer einen Kandidaten.

    Wird vom n8n-Workflow 'Instantly Events loggen' aufgerufen
    wenn Instantly den email_sent Webhook feuert.
    """
    if not data.candidate_id:
        return JSONResponse(
            status_code=400,
            content={"error": "candidate_id fehlt"},
        )

    candidate = await db.get(Candidate, data.candidate_id)
    if not candidate:
        logger.warning(f"Instantly email_sent: Kandidat {data.candidate_id} nicht gefunden")
        return JSONResponse(
            status_code=404,
            content={"error": "Kandidat nicht gefunden", "candidate_id": data.candidate_id},
        )

    email = CandidateEmail(
        candidate_id=data.candidate_id,
        subject=data.subject or "",
        body_html=data.body,
        direction=data.direction,
        channel="instantly",
        from_address=data.from_address,
        to_address=data.to_address,
    )
    db.add(email)
    await db.flush()

    logger.info(f"Instantly E-Mail geloggt: {data.direction} fuer Kandidat {data.candidate_id}")

    return {"status": "ok", "email_id": str(email.id), "candidate_id": data.candidate_id}


class InstantlyBounceLog(BaseModel):
    candidate_id: Optional[str] = None
    email: Optional[str] = None
    source: str = "instantly"


@router.post("/emails/log-bounce")
async def log_instantly_bounce(
    data: InstantlyBounceLog,
    db: AsyncSession = Depends(get_db),
):
    """Setzt den Kandidaten-Status auf 'bounce' wenn Instantly einen Bounce meldet.

    Wird vom n8n-Workflow 'Instantly Events loggen' aufgerufen
    wenn Instantly den email_bounced Webhook feuert.
    """
    if not data.candidate_id:
        return JSONResponse(
            status_code=400,
            content={"error": "candidate_id fehlt"},
        )

    candidate = await db.get(Candidate, data.candidate_id)
    if not candidate:
        logger.warning(f"Instantly bounce: Kandidat {data.candidate_id} nicht gefunden")
        return JSONResponse(
            status_code=404,
            content={"error": "Kandidat nicht gefunden", "candidate_id": data.candidate_id},
        )

    candidate.contact_status = "bounce"
    await db.flush()

    logger.info(f"Instantly Bounce: Kandidat {data.candidate_id} ({data.email}) → Status 'bounce'")

    return {"status": "ok", "candidate_id": data.candidate_id, "new_status": "bounce"}
