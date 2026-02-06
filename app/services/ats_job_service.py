"""ATS Job Service fuer das Matching-Tool."""

import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.ats_activity import ATSActivity, ActivityType
from app.models.ats_job import ATSJob, ATSJobPriority, ATSJobStatus

logger = logging.getLogger(__name__)


class ATSJobService:
    """Service fuer ATS-Stellen-Verwaltung."""

    def __init__(self, db: AsyncSession):
        self.db = db

    # ── CRUD ─────────────────────────────────────────

    async def create_job(self, title: str, **kwargs) -> ATSJob:
        """Erstellt eine neue ATS-Stelle."""
        job = ATSJob(title=title.strip(), **kwargs)
        self.db.add(job)
        await self.db.flush()

        # Activity loggen
        activity = ATSActivity(
            activity_type=ActivityType.JOB_CREATED,
            description=f"Stelle '{job.title}' erstellt",
            ats_job_id=job.id,
            company_id=job.company_id,
        )
        self.db.add(activity)
        await self.db.flush()

        logger.info(f"ATSJob erstellt: {job.id} - {job.title}")
        return job

    async def get_job(self, job_id: UUID) -> ATSJob | None:
        """Holt eine ATS-Stelle nach ID mit Relationships."""
        result = await self.db.execute(
            select(ATSJob)
            .options(
                selectinload(ATSJob.company),
                selectinload(ATSJob.contact),
                selectinload(ATSJob.pipeline_entries),
                selectinload(ATSJob.call_notes),
                selectinload(ATSJob.activities),
            )
            .where(ATSJob.id == job_id)
        )
        return result.scalar_one_or_none()

    async def update_job(self, job_id: UUID, data: dict) -> ATSJob | None:
        """Aktualisiert eine ATS-Stelle."""
        job = await self.db.get(ATSJob, job_id)
        if not job:
            return None
        for key, value in data.items():
            if value is not None and hasattr(job, key):
                setattr(job, key, value)
        await self.db.flush()
        logger.info(f"ATSJob aktualisiert: {job.id} - {job.title}")
        return job

    async def delete_job(self, job_id: UUID) -> bool:
        """Loescht eine ATS-Stelle."""
        job = await self.db.get(ATSJob, job_id)
        if not job:
            return False
        await self.db.delete(job)
        await self.db.flush()
        logger.info(f"ATSJob geloescht: {job_id}")
        return True

    async def change_status(self, job_id: UUID, new_status: str) -> ATSJob | None:
        """Aendert den Status einer ATS-Stelle."""
        job = await self.db.get(ATSJob, job_id)
        if not job:
            return None

        old_status = job.status.value
        job.status = ATSJobStatus(new_status)

        if new_status == "filled":
            job.filled_at = datetime.now(timezone.utc)

        # Activity loggen
        activity = ATSActivity(
            activity_type=ActivityType.JOB_STATUS_CHANGED,
            description=f"Status von '{old_status}' auf '{new_status}' geaendert",
            ats_job_id=job.id,
            company_id=job.company_id,
            metadata_json={"from_status": old_status, "to_status": new_status},
        )
        self.db.add(activity)
        await self.db.flush()

        logger.info(f"ATSJob Status: {job.title} -> {new_status}")
        return job

    # ── Listen ───────────────────────────────────────

    async def list_jobs(
        self,
        company_id: UUID | None = None,
        status: str | None = None,
        priority: str | None = None,
        search: str | None = None,
        sort_by: str = "created_at",
        page: int = 1,
        per_page: int = 25,
        include_deleted: bool = False,
    ) -> dict:
        """Listet ATS-Stellen mit Filter und Pagination."""
        query = select(ATSJob).options(selectinload(ATSJob.company))
        count_query = select(func.count(ATSJob.id))

        # Soft-deleted Jobs ausschliessen (Standard)
        if not include_deleted:
            query = query.where(ATSJob.deleted_at.is_(None))
            count_query = count_query.where(ATSJob.deleted_at.is_(None))

        # Filter
        if company_id:
            query = query.where(ATSJob.company_id == company_id)
            count_query = count_query.where(ATSJob.company_id == company_id)

        if status:
            query = query.where(ATSJob.status == ATSJobStatus(status))
            count_query = count_query.where(ATSJob.status == ATSJobStatus(status))

        if priority:
            query = query.where(ATSJob.priority == ATSJobPriority(priority))
            count_query = count_query.where(ATSJob.priority == ATSJobPriority(priority))

        if search:
            search_term = f"%{search}%"
            query = query.where(ATSJob.title.ilike(search_term))
            count_query = count_query.where(ATSJob.title.ilike(search_term))

        # Sortierung
        if sort_by == "title":
            query = query.order_by(ATSJob.title.asc())
        elif sort_by == "priority":
            query = query.order_by(ATSJob.priority.desc(), ATSJob.created_at.desc())
        else:
            query = query.order_by(ATSJob.created_at.desc())

        # Total Count
        total_result = await self.db.execute(count_query)
        total = total_result.scalar() or 0

        # Pagination
        offset = (page - 1) * per_page
        query = query.offset(offset).limit(per_page)

        result = await self.db.execute(query)
        jobs = result.scalars().all()

        # Pipeline-Counts pro Job
        from app.models.ats_pipeline import ATSPipelineEntry

        items = []
        for job in jobs:
            pipeline_count_result = await self.db.execute(
                select(func.count(ATSPipelineEntry.id)).where(
                    ATSPipelineEntry.ats_job_id == job.id
                )
            )
            pipeline_count = pipeline_count_result.scalar() or 0

            items.append({
                "job": job,
                "pipeline_count": pipeline_count,
            })

        pages = (total + per_page - 1) // per_page if per_page > 0 else 0

        return {
            "items": items,
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": pages,
        }

    async def get_jobs_for_company(self, company_id: UUID, include_deleted: bool = False) -> list[ATSJob]:
        """Holt alle ATS-Stellen fuer ein Unternehmen."""
        query = (
            select(ATSJob)
            .where(ATSJob.company_id == company_id)
            .order_by(ATSJob.created_at.desc())
        )

        # Soft-deleted Jobs ausschliessen (Standard)
        if not include_deleted:
            query = query.where(ATSJob.deleted_at.is_(None))

        result = await self.db.execute(query)
        return list(result.scalars().all())

    # ── Statistics ───────────────────────────────────

    async def get_stats(self) -> dict:
        """Gibt ATS-Stellen-Statistiken zurueck (nur nicht-geloeschte)."""
        # Nur nicht-geloeschte Jobs zaehlen
        base_filter = ATSJob.deleted_at.is_(None)

        total = await self.db.execute(
            select(func.count(ATSJob.id)).where(base_filter)
        )
        open_count = await self.db.execute(
            select(func.count(ATSJob.id)).where(
                base_filter,
                ATSJob.status == ATSJobStatus.OPEN
            )
        )
        filled = await self.db.execute(
            select(func.count(ATSJob.id)).where(
                base_filter,
                ATSJob.status == ATSJobStatus.FILLED
            )
        )
        return {
            "total": total.scalar() or 0,
            "open": open_count.scalar() or 0,
            "filled": filled.scalar() or 0,
        }
