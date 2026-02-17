"""ATS Todo Service fuer das Matching-Tool."""

import logging
from datetime import date, datetime, timezone
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.ats_activity import ATSActivity, ActivityType
from app.models.ats_todo import ATSTodo, TodoPriority, TodoStatus

logger = logging.getLogger(__name__)


class ATSTodoService:
    """Service fuer ATS-Aufgaben-Verwaltung."""

    def __init__(self, db: AsyncSession):
        self.db = db

    # ── CRUD ─────────────────────────────────────────

    async def create_todo(
        self,
        title: str,
        description: str | None = None,
        priority: str = "wichtig",
        due_date: date | None = None,
        due_time: str | None = None,
        company_id: UUID | None = None,
        candidate_id: UUID | None = None,
        ats_job_id: UUID | None = None,
        call_note_id: UUID | None = None,
        pipeline_entry_id: UUID | None = None,
        contact_id: UUID | None = None,
    ) -> ATSTodo:
        """Erstellt eine neue Aufgabe."""
        todo = ATSTodo(
            title=title.strip(),
            description=description,
            priority=TodoPriority(priority),
            due_date=due_date,
            due_time=due_time,
            company_id=company_id,
            candidate_id=candidate_id,
            ats_job_id=ats_job_id,
            call_note_id=call_note_id,
            pipeline_entry_id=pipeline_entry_id,
            contact_id=contact_id,
        )
        self.db.add(todo)
        await self.db.flush()

        # Activity loggen
        activity = ATSActivity(
            activity_type=ActivityType.TODO_CREATED,
            description=f"Aufgabe erstellt: {todo.title[:80]}",
            ats_job_id=ats_job_id,
            company_id=company_id,
            candidate_id=candidate_id,
        )
        self.db.add(activity)
        await self.db.flush()

        logger.info(f"Todo erstellt: {todo.id} - {todo.title[:50]}")
        return todo

    async def get_todo(self, todo_id: UUID) -> ATSTodo | None:
        """Holt eine Aufgabe nach ID."""
        result = await self.db.execute(
            select(ATSTodo)
            .options(
                selectinload(ATSTodo.company),
                selectinload(ATSTodo.candidate),
                selectinload(ATSTodo.ats_job),
                selectinload(ATSTodo.contact),
            )
            .where(ATSTodo.id == todo_id)
        )
        return result.scalar_one_or_none()

    async def update_todo(self, todo_id: UUID, data: dict) -> ATSTodo | None:
        """Aktualisiert eine Aufgabe."""
        todo = await self.db.get(ATSTodo, todo_id)
        if not todo:
            return None

        for key, value in data.items():
            if hasattr(todo, key):
                if key == "status" and value:
                    todo.status = TodoStatus(value)
                elif key == "priority" and value:
                    todo.priority = TodoPriority(value)
                elif value is not None:
                    setattr(todo, key, value)

        await self.db.flush()
        logger.info(f"Todo aktualisiert: {todo.id}")
        return todo

    async def complete_todo(self, todo_id: UUID) -> ATSTodo | None:
        """Schliesst eine Aufgabe ab."""
        todo = await self.db.get(ATSTodo, todo_id)
        if not todo:
            return None

        todo.status = TodoStatus.DONE
        todo.completed_at = datetime.now(timezone.utc)
        await self.db.flush()

        # Activity loggen
        activity = ATSActivity(
            activity_type=ActivityType.TODO_COMPLETED,
            description=f"Aufgabe erledigt: {todo.title[:80]}",
            ats_job_id=todo.ats_job_id,
            company_id=todo.company_id,
            candidate_id=todo.candidate_id,
        )
        self.db.add(activity)
        await self.db.flush()

        logger.info(f"Todo erledigt: {todo.id} - {todo.title[:50]}")
        return todo

    async def delete_todo(self, todo_id: UUID) -> bool:
        """Loescht eine Aufgabe."""
        todo = await self.db.get(ATSTodo, todo_id)
        if not todo:
            return False
        await self.db.delete(todo)
        await self.db.flush()
        logger.info(f"Todo geloescht: {todo_id}")
        return True

    # ── Listen ───────────────────────────────────────

    async def list_todos(
        self,
        status: str | None = None,
        priority: str | None = None,
        due_date: date | None = None,
        company_id: UUID | None = None,
        candidate_id: UUID | None = None,
        ats_job_id: UUID | None = None,
        contact_id: UUID | None = None,
        page: int = 1,
        per_page: int = 50,
    ) -> dict:
        """Listet Aufgaben mit Filter und Pagination."""
        query = select(ATSTodo).options(
            selectinload(ATSTodo.company),
            selectinload(ATSTodo.candidate),
            selectinload(ATSTodo.ats_job),
            selectinload(ATSTodo.contact),
        )
        count_query = select(func.count(ATSTodo.id))

        # Filter
        if status:
            query = query.where(ATSTodo.status == TodoStatus(status))
            count_query = count_query.where(ATSTodo.status == TodoStatus(status))

        if priority:
            query = query.where(ATSTodo.priority == TodoPriority(priority))
            count_query = count_query.where(ATSTodo.priority == TodoPriority(priority))

        if due_date:
            query = query.where(ATSTodo.due_date == due_date)
            count_query = count_query.where(ATSTodo.due_date == due_date)

        if company_id:
            query = query.where(ATSTodo.company_id == company_id)
            count_query = count_query.where(ATSTodo.company_id == company_id)

        if candidate_id:
            query = query.where(ATSTodo.candidate_id == candidate_id)
            count_query = count_query.where(ATSTodo.candidate_id == candidate_id)

        if ats_job_id:
            query = query.where(ATSTodo.ats_job_id == ats_job_id)
            count_query = count_query.where(ATSTodo.ats_job_id == ats_job_id)

        if contact_id:
            query = query.where(ATSTodo.contact_id == contact_id)
            count_query = count_query.where(ATSTodo.contact_id == contact_id)

        # Sortierung: offene zuerst, dann nach Prioritaet und Faelligkeit
        query = query.order_by(
            ATSTodo.status.asc(),
            ATSTodo.priority.desc(),
            ATSTodo.due_date.asc().nullslast(),
            ATSTodo.created_at.desc(),
        )

        # Total Count
        total_result = await self.db.execute(count_query)
        total = total_result.scalar() or 0

        # Pagination
        offset = (page - 1) * per_page
        query = query.offset(offset).limit(per_page)

        result = await self.db.execute(query)
        todos = list(result.scalars().all())

        pages = (total + per_page - 1) // per_page if per_page > 0 else 0

        return {
            "items": todos,
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": pages,
        }

    async def get_today_todos(self) -> list[ATSTodo]:
        """Holt alle Aufgaben fuer heute."""
        today = date.today()
        result = await self.db.execute(
            select(ATSTodo)
            .options(
                selectinload(ATSTodo.company),
                selectinload(ATSTodo.candidate),
                selectinload(ATSTodo.ats_job),
                selectinload(ATSTodo.contact),
            )
            .where(
                ATSTodo.due_date == today,
                ATSTodo.status.in_([TodoStatus.OPEN, TodoStatus.IN_PROGRESS]),
            )
            .order_by(ATSTodo.priority.desc())
        )
        return list(result.scalars().all())

    async def get_overdue_todos(self) -> list[ATSTodo]:
        """Holt alle ueberfaelligen Aufgaben."""
        today = date.today()
        result = await self.db.execute(
            select(ATSTodo)
            .options(
                selectinload(ATSTodo.company),
                selectinload(ATSTodo.candidate),
                selectinload(ATSTodo.ats_job),
                selectinload(ATSTodo.contact),
            )
            .where(
                ATSTodo.due_date < today,
                ATSTodo.status.in_([TodoStatus.OPEN, TodoStatus.IN_PROGRESS]),
            )
            .order_by(ATSTodo.due_date.asc(), ATSTodo.priority.desc())
        )
        return list(result.scalars().all())

    # ── Statistics ───────────────────────────────────

    async def get_stats(self) -> dict:
        """Gibt Todo-Statistiken zurueck."""
        total = await self.db.execute(select(func.count(ATSTodo.id)))
        open_count = await self.db.execute(
            select(func.count(ATSTodo.id)).where(ATSTodo.status == TodoStatus.OPEN)
        )
        in_progress = await self.db.execute(
            select(func.count(ATSTodo.id)).where(ATSTodo.status == TodoStatus.IN_PROGRESS)
        )
        done = await self.db.execute(
            select(func.count(ATSTodo.id)).where(ATSTodo.status == TodoStatus.DONE)
        )
        overdue = await self.db.execute(
            select(func.count(ATSTodo.id)).where(
                ATSTodo.due_date < date.today(),
                ATSTodo.status.in_([TodoStatus.OPEN, TodoStatus.IN_PROGRESS]),
            )
        )
        return {
            "total": total.scalar() or 0,
            "open": open_count.scalar() or 0,
            "in_progress": in_progress.scalar() or 0,
            "done": done.scalar() or 0,
            "overdue": overdue.scalar() or 0,
        }
