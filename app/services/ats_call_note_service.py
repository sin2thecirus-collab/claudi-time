"""ATS Call Note Service fuer das Matching-Tool."""

import logging
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.ats_activity import ATSActivity, ActivityType
from app.models.ats_call_note import ATSCallNote, CallType, CallDirection
from app.models.ats_todo import ATSTodo, TodoPriority

logger = logging.getLogger(__name__)


class ATSCallNoteService:
    """Service fuer ATS-Telefonat-Notizen."""

    def __init__(self, db: AsyncSession):
        self.db = db

    # ── CRUD ─────────────────────────────────────────

    async def create_call_note(
        self,
        call_type: str,
        summary: str,
        company_id: UUID | None = None,
        candidate_id: UUID | None = None,
        ats_job_id: UUID | None = None,
        contact_id: UUID | None = None,
        raw_notes: str | None = None,
        action_items: list | None = None,
        duration_minutes: int | None = None,
        direction: str | None = None,
        called_at: str | None = None,
    ) -> ATSCallNote:
        """Erstellt eine neue Call-Note."""
        from datetime import datetime as dt

        # Direction parsen
        parsed_direction = None
        if direction:
            try:
                parsed_direction = CallDirection(direction)
            except ValueError:
                parsed_direction = None

        # called_at parsen
        parsed_called_at = None
        if called_at:
            try:
                parsed_called_at = dt.fromisoformat(called_at)
            except (ValueError, TypeError):
                parsed_called_at = None

        note = ATSCallNote(
            call_type=CallType(call_type),
            summary=summary.strip(),
            company_id=company_id,
            candidate_id=candidate_id,
            ats_job_id=ats_job_id,
            contact_id=contact_id,
            raw_notes=raw_notes,
            action_items=action_items,
            duration_minutes=duration_minutes,
            direction=parsed_direction,
        )
        if parsed_called_at:
            note.called_at = parsed_called_at
        self.db.add(note)
        await self.db.flush()

        # Activity loggen
        activity = ATSActivity(
            activity_type=ActivityType.CALL_LOGGED,
            description=f"Anruf protokolliert: {note.call_type_label}",
            ats_job_id=ats_job_id,
            company_id=company_id,
            candidate_id=candidate_id,
        )
        self.db.add(activity)
        await self.db.flush()

        logger.info(f"CallNote erstellt: {note.id} - {note.call_type.value}")
        return note

    async def get_call_note(self, note_id: UUID) -> ATSCallNote | None:
        """Holt eine Call-Note nach ID."""
        result = await self.db.execute(
            select(ATSCallNote)
            .options(
                selectinload(ATSCallNote.company),
                selectinload(ATSCallNote.candidate),
                selectinload(ATSCallNote.contact),
                selectinload(ATSCallNote.todos),
            )
            .where(ATSCallNote.id == note_id)
        )
        return result.scalar_one_or_none()

    async def delete_call_note(self, note_id: UUID) -> bool:
        """Loescht eine Call-Note."""
        note = await self.db.get(ATSCallNote, note_id)
        if not note:
            return False
        await self.db.delete(note)
        await self.db.flush()
        logger.info(f"CallNote geloescht: {note_id}")
        return True

    # ── Listen ───────────────────────────────────────

    async def list_call_notes(
        self,
        company_id: UUID | None = None,
        candidate_id: UUID | None = None,
        ats_job_id: UUID | None = None,
        call_type: str | None = None,
        page: int = 1,
        per_page: int = 25,
    ) -> dict:
        """Listet Call-Notes mit Filter und Pagination."""
        query = select(ATSCallNote).options(
            selectinload(ATSCallNote.company),
            selectinload(ATSCallNote.candidate),
            selectinload(ATSCallNote.contact),
        )
        count_query = select(func.count(ATSCallNote.id))

        # Filter
        if company_id:
            query = query.where(ATSCallNote.company_id == company_id)
            count_query = count_query.where(ATSCallNote.company_id == company_id)

        if candidate_id:
            query = query.where(ATSCallNote.candidate_id == candidate_id)
            count_query = count_query.where(ATSCallNote.candidate_id == candidate_id)

        if ats_job_id:
            query = query.where(ATSCallNote.ats_job_id == ats_job_id)
            count_query = count_query.where(ATSCallNote.ats_job_id == ats_job_id)

        if call_type:
            query = query.where(ATSCallNote.call_type == CallType(call_type))
            count_query = count_query.where(ATSCallNote.call_type == CallType(call_type))

        # Sortierung: neueste zuerst
        query = query.order_by(ATSCallNote.called_at.desc())

        # Total Count
        total_result = await self.db.execute(count_query)
        total = total_result.scalar() or 0

        # Pagination
        offset = (page - 1) * per_page
        query = query.offset(offset).limit(per_page)

        result = await self.db.execute(query)
        notes = list(result.scalars().all())

        pages = (total + per_page - 1) // per_page if per_page > 0 else 0

        return {
            "items": notes,
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": pages,
        }

    # ── Todo-Generierung ─────────────────────────────

    async def auto_create_todos(self, call_note_id: UUID) -> list[ATSTodo]:
        """Erstellt Todos aus den Action Items einer Call-Note."""
        note = await self.db.get(ATSCallNote, call_note_id)
        if not note or not note.action_items:
            return []

        todos = []
        items = note.action_items if isinstance(note.action_items, list) else []

        for item in items:
            title = item if isinstance(item, str) else str(item)
            if not title.strip():
                continue

            todo = ATSTodo(
                title=title.strip(),
                priority=TodoPriority.WICHTIG,
                company_id=note.company_id,
                candidate_id=note.candidate_id,
                ats_job_id=note.ats_job_id,
                call_note_id=note.id,
            )
            self.db.add(todo)
            todos.append(todo)

        await self.db.flush()

        # Activities loggen
        for todo in todos:
            activity = ATSActivity(
                activity_type=ActivityType.TODO_CREATED,
                description=f"Aufgabe erstellt: {todo.title[:80]}",
                ats_job_id=note.ats_job_id,
                company_id=note.company_id,
                candidate_id=note.candidate_id,
            )
            self.db.add(activity)

        await self.db.flush()
        logger.info(f"Auto-Todos: {len(todos)} Aufgaben aus CallNote {call_note_id} erstellt")
        return todos
