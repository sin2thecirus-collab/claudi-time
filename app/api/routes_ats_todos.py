"""API-Routen fuer ATS-Todos (Aufgaben)."""

from datetime import date
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.ats_todo_service import ATSTodoService

router = APIRouter(prefix="/ats/todos", tags=["ATS Todos"])


# ── Pydantic Schemas ─────────────────────────────

class TodoCreate(BaseModel):
    title: str
    description: Optional[str] = None
    priority: Optional[str] = "wichtig"
    due_date: Optional[date] = None
    due_time: Optional[str] = None  # z.B. "14:00"
    company_id: Optional[UUID] = None
    candidate_id: Optional[UUID] = None
    ats_job_id: Optional[UUID] = None
    call_note_id: Optional[UUID] = None
    pipeline_entry_id: Optional[UUID] = None
    contact_id: Optional[UUID] = None


class TodoUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    priority: Optional[str] = None
    due_date: Optional[date] = None
    due_time: Optional[str] = None


# ── Helper ───────────────────────────────────────

def _serialize_todo(todo) -> dict:
    """Serialisiert ein Todo-Objekt."""
    return {
        "id": str(todo.id),
        "title": todo.title,
        "description": todo.description,
        "status": todo.status.value,
        "status_label": todo.status_label,
        "priority": todo.priority.value,
        "priority_label": todo.priority_label,
        "priority_color": todo.priority_color,
        "due_date": todo.due_date.isoformat() if todo.due_date else None,
        "due_time": todo.due_time,
        "is_overdue": todo.is_overdue,
        "completed_at": todo.completed_at.isoformat() if todo.completed_at else None,
        "company_id": str(todo.company_id) if todo.company_id else None,
        "company_name": todo.company.name if hasattr(todo, "company") and todo.company else None,
        "candidate_id": str(todo.candidate_id) if todo.candidate_id else None,
        "candidate_name": (
            f"{todo.candidate.first_name or ''} {todo.candidate.last_name or ''}".strip()
            if hasattr(todo, "candidate") and todo.candidate else None
        ),
        "ats_job_id": str(todo.ats_job_id) if todo.ats_job_id else None,
        "ats_job_title": todo.ats_job.title if hasattr(todo, "ats_job") and todo.ats_job else None,
        "contact_id": str(todo.contact_id) if todo.contact_id else None,
        "contact_name": todo.contact.full_name if hasattr(todo, "contact") and todo.contact else None,
        "created_at": todo.created_at.isoformat() if todo.created_at else None,
    }


# ── Endpoints ────────────────────────────────────

@router.post("")
async def create_todo(data: TodoCreate, db: AsyncSession = Depends(get_db)):
    """Erstellt eine neue Aufgabe."""
    valid_priorities = ["unwichtig", "mittelmaessig", "wichtig", "dringend", "sehr_dringend"]
    if data.priority and data.priority not in valid_priorities:
        raise HTTPException(status_code=400, detail=f"Ungueltige Prioritaet: {data.priority}")

    service = ATSTodoService(db)
    todo = await service.create_todo(**data.model_dump(exclude_unset=True))
    await db.commit()
    return _serialize_todo(todo)


@router.get("")
async def list_todos(
    status: Optional[str] = Query(None),
    priority: Optional[str] = Query(None),
    due_date: Optional[date] = Query(None),
    company_id: Optional[UUID] = Query(None),
    candidate_id: Optional[UUID] = Query(None),
    ats_job_id: Optional[UUID] = Query(None),
    contact_id: Optional[UUID] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Listet Aufgaben mit Filter."""
    service = ATSTodoService(db)
    result = await service.list_todos(
        status=status,
        priority=priority,
        due_date=due_date,
        company_id=company_id,
        candidate_id=candidate_id,
        ats_job_id=ats_job_id,
        contact_id=contact_id,
        page=page,
        per_page=per_page,
    )

    return {
        "items": [_serialize_todo(t) for t in result["items"]],
        "total": result["total"],
        "page": result["page"],
        "per_page": result["per_page"],
        "pages": result["pages"],
    }


@router.get("/today")
async def get_today_todos(db: AsyncSession = Depends(get_db)):
    """Holt alle Aufgaben fuer heute."""
    service = ATSTodoService(db)
    todos = await service.get_today_todos()
    return {"items": [_serialize_todo(t) for t in todos], "count": len(todos)}


@router.get("/overdue")
async def get_overdue_todos(db: AsyncSession = Depends(get_db)):
    """Holt alle ueberfaelligen Aufgaben."""
    service = ATSTodoService(db)
    todos = await service.get_overdue_todos()
    return {"items": [_serialize_todo(t) for t in todos], "count": len(todos)}


@router.patch("/{todo_id}")
async def update_todo(
    todo_id: UUID, data: TodoUpdate, db: AsyncSession = Depends(get_db)
):
    """Aktualisiert eine Aufgabe."""
    service = ATSTodoService(db)
    todo = await service.update_todo(todo_id, data.model_dump(exclude_unset=True))
    if not todo:
        raise HTTPException(status_code=404, detail="Aufgabe nicht gefunden")
    await db.commit()
    return _serialize_todo(todo)


@router.put("/{todo_id}/complete")
async def complete_todo(todo_id: UUID, db: AsyncSession = Depends(get_db)):
    """Schliesst eine Aufgabe ab."""
    service = ATSTodoService(db)
    todo = await service.complete_todo(todo_id)
    if not todo:
        raise HTTPException(status_code=404, detail="Aufgabe nicht gefunden")
    await db.commit()
    return {"message": "Aufgabe erledigt", "id": str(todo.id)}


@router.delete("/{todo_id}")
async def delete_todo(todo_id: UUID, db: AsyncSession = Depends(get_db)):
    """Loescht eine Aufgabe."""
    service = ATSTodoService(db)
    deleted = await service.delete_todo(todo_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Aufgabe nicht gefunden")
    await db.commit()
    return {"message": "Aufgabe geloescht"}
