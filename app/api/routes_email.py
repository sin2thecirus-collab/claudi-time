"""API-Endpoints fuer Email-Drafts und Email-Verwaltung."""

import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.email_draft import EmailDraft, EmailDraftStatus
from app.services.email_service import EmailService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/email", tags=["Email"])


# ── Schemas ──

class DraftUpdate(BaseModel):
    subject: Optional[str] = None
    body_html: Optional[str] = None


class DraftResponse(BaseModel):
    id: str
    candidate_id: Optional[str]
    ats_job_id: Optional[str]
    email_type: str
    to_email: str
    subject: str
    body_html: str
    status: str
    gpt_context: Optional[str]
    auto_send: bool
    sent_at: Optional[str]
    send_error: Optional[str]
    created_at: Optional[str]


def _serialize_draft(d: EmailDraft) -> dict:
    """EmailDraft → JSON-serialisierbares Dict."""
    return {
        "id": str(d.id),
        "candidate_id": str(d.candidate_id) if d.candidate_id else None,
        "candidate_name": (
            f"{d.candidate.first_name} {d.candidate.last_name}"
            if d.candidate else None
        ),
        "ats_job_id": str(d.ats_job_id) if d.ats_job_id else None,
        "call_note_id": str(d.call_note_id) if d.call_note_id else None,
        "email_type": d.email_type,
        "to_email": d.to_email,
        "subject": d.subject,
        "body_html": d.body_html,
        "status": d.status,
        "gpt_context": d.gpt_context,
        "auto_send": d.auto_send,
        "sent_at": d.sent_at.isoformat() if d.sent_at else None,
        "send_error": d.send_error,
        "created_at": d.created_at.isoformat() if d.created_at else None,
        "updated_at": d.updated_at.isoformat() if d.updated_at else None,
    }


# ── Endpoints ──

@router.get("/drafts")
async def list_drafts(
    candidate_id: Optional[UUID] = None,
    status: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """Listet Email-Drafts, optional gefiltert nach Kandidat oder Status."""
    service = EmailService(db)
    drafts = await service.list_drafts(
        candidate_id=candidate_id,
        status=status,
    )
    return {
        "items": [_serialize_draft(d) for d in drafts],
        "total": len(drafts),
    }


@router.get("/drafts/count")
async def draft_count(db: AsyncSession = Depends(get_db)):
    """Zaehlt offene Drafts (fuer Dashboard-Badge)."""
    service = EmailService(db)
    count = await service.count_pending_drafts()
    return {"pending_drafts": count}


@router.get("/drafts/{draft_id}")
async def get_draft(draft_id: UUID, db: AsyncSession = Depends(get_db)):
    """Holt einen einzelnen Draft."""
    draft = await db.get(EmailDraft, draft_id)
    if not draft:
        return {"error": "Draft nicht gefunden"}, 404
    return _serialize_draft(draft)


@router.put("/drafts/{draft_id}")
async def update_draft(
    draft_id: UUID,
    data: DraftUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Bearbeitet einen Draft (Subject und/oder Body)."""
    draft = await db.get(EmailDraft, draft_id)
    if not draft:
        return {"error": "Draft nicht gefunden"}, 404

    if draft.status != EmailDraftStatus.DRAFT.value:
        return {"error": f"Draft hat Status '{draft.status}', kann nicht bearbeitet werden"}, 400

    if data.subject is not None:
        draft.subject = data.subject
    if data.body_html is not None:
        draft.body_html = data.body_html

    await db.commit()
    return _serialize_draft(draft)


@router.post("/drafts/{draft_id}/send")
async def send_draft(draft_id: UUID, db: AsyncSession = Depends(get_db)):
    """Sendet einen Draft (nach Recruiter-Pruefung)."""
    service = EmailService(db)
    result = await service.send_draft(draft_id)
    await db.commit()

    if result["success"]:
        return {"message": "Email gesendet", "message_id": result.get("message_id")}
    else:
        return {"error": result.get("error")}, 500


@router.delete("/drafts/{draft_id}")
async def cancel_draft(draft_id: UUID, db: AsyncSession = Depends(get_db)):
    """Verwirft einen Draft."""
    draft = await db.get(EmailDraft, draft_id)
    if not draft:
        return {"error": "Draft nicht gefunden"}, 404

    draft.status = EmailDraftStatus.CANCELLED.value
    await db.commit()
    return {"message": "Draft verworfen", "id": str(draft_id)}


@router.post("/test-send")
async def test_email_send(
    to_email: str = "hamdard@sincirus.com",
    db: AsyncSession = Depends(get_db),
):
    """Debug-Endpoint: Sendet eine Test-Email um die M365-Integration zu pruefen."""
    from app.services.email_service import MicrosoftGraphClient

    result = await MicrosoftGraphClient.send_email(
        to_email=to_email,
        subject="[Test] Pulspoint Email-Integration",
        body_html="""
        <div style="font-family: Arial, sans-serif; padding: 20px;">
            <h2>✅ Email-Integration funktioniert!</h2>
            <p>Diese Test-Email wurde automatisch von Pulspoint CRM gesendet.</p>
            <p>Microsoft Graph API ist korrekt konfiguriert.</p>
        </div>
        """,
    )

    return result
