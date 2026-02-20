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
        """Erstellt eine neue ATS-Stelle und verknuepften Source-Job."""
        # Import hier um Circular Import zu vermeiden
        from app.models.job import Job

        # Wenn keine source_job_id angegeben, erstellen wir einen neuen Job
        source_job_id = kwargs.pop("source_job_id", None)

        if not source_job_id:
            # Company-Name fuer Job ermitteln
            company_name = "Unbekannt"
            company_id = kwargs.get("company_id")
            if company_id:
                from app.models.company import Company
                company = await self.db.get(Company, company_id)
                if company:
                    company_name = company.name

            # Neuen Source-Job erstellen
            source_job = Job(
                position=title.strip(),
                company_name=company_name,
                company_id=company_id,
                city=kwargs.get("location_city"),
                work_location_city=kwargs.get("location_city"),
                job_text=kwargs.get("description"),
                employment_type=kwargs.get("employment_type"),
            )
            self.db.add(source_job)
            await self.db.flush()
            source_job_id = source_job.id
            logger.info("Source-Job erstellt: %s - %s", source_job.id, source_job.position)

        # ATS-Job mit Verknuepfung erstellen
        job = ATSJob(title=title.strip(), source_job_id=source_job_id, **kwargs)
        self.db.add(job)
        await self.db.flush()

        # Activity loggen
        activity = ATSActivity(
            activity_type=ActivityType.JOB_CREATED,
            description="Stelle '" + job.title + "' erstellt",
            ats_job_id=job.id,
            company_id=job.company_id,
        )
        self.db.add(activity)
        await self.db.flush()

        logger.info("ATSJob erstellt: %s - %s (source_job_id: %s)", job.id, job.title, source_job_id)
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
        """Aktualisiert eine ATS-Stelle.

        Felder die manuell geaendert werden, werden in manual_overrides
        markiert, damit der Job-Stelle-Sync sie nicht ueberschreibt.
        """
        job = await self.db.get(ATSJob, job_id)
        if not job:
            return None

        # Felder die vom Sync kommen und als Override getrackt werden
        syncable_fields = {"title", "description", "location_city", "employment_type"}
        overrides = dict(job.manual_overrides) if job.manual_overrides else {}

        for key, value in data.items():
            if value is not None and hasattr(job, key):
                setattr(job, key, value)
                if key in syncable_fields:
                    overrides[key] = True

        if overrides != (job.manual_overrides or {}):
            job.manual_overrides = overrides

        await self.db.flush()
        logger.info(f"ATSJob aktualisiert: {job.id} - {job.title}")
        return job

    async def update_qualification_fields(
        self, job_id: UUID, data: dict, overwrite: bool = False
    ) -> dict:
        """Aktualisiert Job-Qualifizierungsfelder auf einer ATSJob-Stelle.

        Standardmaessig werden nur NULL-Felder ueberschrieben.
        Mit overwrite=True werden auch bestehende Werte ersetzt.

        Returns:
            {"updated_fields": [...], "skipped_fields": [...]}
        """
        job = await self.db.get(ATSJob, job_id)
        if not job:
            return {"updated_fields": [], "skipped_fields": [], "error": "Job nicht gefunden"}

        quali_fields = [
            "team_size", "erp_system", "home_office_days", "flextime", "core_hours",
            "vacation_days", "overtime_handling", "open_office", "english_requirements",
            "hiring_process_steps", "feedback_timeline", "digitalization_level",
            "older_candidates_ok", "desired_start_date", "interviews_started",
            "ideal_candidate_description", "candidate_tasks", "multiple_entities",
            "task_distribution", "salary_min", "salary_max", "employment_type",
            "description", "requirements", "location_city",
        ]

        updated = []
        skipped = []

        for field in quali_fields:
            value = data.get(field)
            if value is None:
                continue
            if not hasattr(job, field):
                continue

            current = getattr(job, field)
            if current is not None and not overwrite:
                skipped.append(field)
            else:
                setattr(job, field, value)
                updated.append(field)

        await self.db.flush()
        logger.info(f"ATSJob Quali-Felder aktualisiert: {job.id}, updated={updated}, skipped={skipped}")
        return {"updated_fields": updated, "skipped_fields": skipped}

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
