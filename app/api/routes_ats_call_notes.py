"""API-Routen fuer ATS-Call-Notes (Telefonat-Notizen)."""

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.ats_call_note_service import ATSCallNoteService

router = APIRouter(prefix="/ats/calls", tags=["ATS Call Notes"])


# ── Pydantic Schemas ─────────────────────────────

class CallNoteCreate(BaseModel):
    call_type: str
    summary: str
    company_id: Optional[UUID] = None
    candidate_id: Optional[UUID] = None
    ats_job_id: Optional[UUID] = None
    contact_id: Optional[UUID] = None
    raw_notes: Optional[str] = None
    action_items: Optional[list] = None
    duration_minutes: Optional[int] = None
    direction: Optional[str] = None
    called_at: Optional[str] = None


# ── Endpoints ────────────────────────────────────

@router.post("")
async def create_call_note(data: CallNoteCreate, db: AsyncSession = Depends(get_db)):
    """Erstellt eine neue Call-Note."""
    valid_types = ["acquisition", "qualification", "followup", "candidate_call"]
    if data.call_type not in valid_types:
        raise HTTPException(status_code=400, detail=f"Ungueltiger Typ: {data.call_type}")

    service = ATSCallNoteService(db)
    note = await service.create_call_note(**data.model_dump(exclude_unset=True))
    await db.commit()
    return {
        "id": str(note.id),
        "call_type": note.call_type.value,
        "message": "Anruf protokolliert",
    }


@router.get("")
async def list_call_notes(
    company_id: Optional[UUID] = Query(None),
    candidate_id: Optional[UUID] = Query(None),
    ats_job_id: Optional[UUID] = Query(None),
    call_type: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Listet Call-Notes mit Filter."""
    service = ATSCallNoteService(db)
    result = await service.list_call_notes(
        company_id=company_id,
        candidate_id=candidate_id,
        ats_job_id=ats_job_id,
        call_type=call_type,
        page=page,
        per_page=per_page,
    )

    items = []
    for note in result["items"]:
        items.append({
            "id": str(note.id),
            "call_type": note.call_type.value,
            "call_type_label": note.call_type_label,
            "summary": note.summary,
            "company_name": note.company.name if note.company else None,
            "candidate_name": f"{note.candidate.first_name or ''} {note.candidate.last_name or ''}".strip() if note.candidate else None,
            "contact_name": note.contact.full_name if note.contact else None,
            "duration_minutes": note.duration_minutes,
            "action_items": note.action_items,
            "called_at": note.called_at.isoformat() if note.called_at else None,
            "created_at": note.created_at.isoformat() if note.created_at else None,
        })

    return {
        "items": items,
        "total": result["total"],
        "page": result["page"],
        "per_page": result["per_page"],
        "pages": result["pages"],
    }


@router.get("/{note_id}")
async def get_call_note(note_id: UUID, db: AsyncSession = Depends(get_db)):
    """Holt Details einer Call-Note."""
    service = ATSCallNoteService(db)
    note = await service.get_call_note(note_id)
    if not note:
        raise HTTPException(status_code=404, detail="Call-Note nicht gefunden")

    return {
        "id": str(note.id),
        "call_type": note.call_type.value,
        "call_type_label": note.call_type_label,
        "summary": note.summary,
        "raw_notes": note.raw_notes,
        "action_items": note.action_items,
        "duration_minutes": note.duration_minutes,
        "company_id": str(note.company_id) if note.company_id else None,
        "company_name": note.company.name if note.company else None,
        "candidate_id": str(note.candidate_id) if note.candidate_id else None,
        "ats_job_id": str(note.ats_job_id) if note.ats_job_id else None,
        "contact_name": note.contact.full_name if note.contact else None,
        "todos": [
            {"id": str(t.id), "title": t.title, "status": t.status.value}
            for t in note.todos
        ] if note.todos else [],
        "called_at": note.called_at.isoformat() if note.called_at else None,
        "created_at": note.created_at.isoformat() if note.created_at else None,
    }


@router.delete("/{note_id}")
async def delete_call_note(note_id: UUID, db: AsyncSession = Depends(get_db)):
    """Loescht eine Call-Note."""
    service = ATSCallNoteService(db)
    deleted = await service.delete_call_note(note_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Call-Note nicht gefunden")
    await db.commit()
    return {"message": "Call-Note geloescht"}


@router.post("/{note_id}/generate-todos")
async def generate_todos_from_call_note(
    note_id: UUID, db: AsyncSession = Depends(get_db)
):
    """Generiert Todos aus den Action Items einer Call-Note."""
    service = ATSCallNoteService(db)
    todos = await service.auto_create_todos(note_id)
    if not todos:
        return {"message": "Keine Action Items gefunden", "created": 0}
    await db.commit()
    return {
        "message": f"{len(todos)} Aufgaben erstellt",
        "created": len(todos),
        "todos": [
            {"id": str(t.id), "title": t.title}
            for t in todos
        ],
    }
