"""Job Service für das Matching-Tool."""

import logging
from datetime import datetime, timedelta, timezone
from typing import Sequence
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.exception_handlers import NotFoundException
from app.config import Limits
from app.models import Job, Match, PriorityCity
from app.schemas import JobCreate, JobFilterParams, JobUpdate, PaginatedResponse

logger = logging.getLogger(__name__)


class JobService:
    """
    Service für Job-Operationen.

    Features:
    - CRUD-Operationen
    - Soft-Delete
    - Filterung und Suche
    - Prio-Städte Sortierung
    - Pagination
    """

    def __init__(self, db: AsyncSession):
        """
        Initialisiert den Job-Service.

        Args:
            db: AsyncSession für Datenbankzugriff
        """
        self.db = db

    async def create_job(self, data: JobCreate) -> Job:
        """
        Erstellt einen neuen Job.

        Args:
            data: JobCreate-Schema

        Returns:
            Erstellter Job
        """
        job = Job(**data.model_dump(exclude_unset=True))
        self.db.add(job)
        await self.db.commit()
        await self.db.refresh(job)

        logger.info(f"Job erstellt: {job.id} - {job.company_name} / {job.position}")
        return job

    async def get_job(self, job_id: UUID) -> Job:
        """
        Holt einen Job nach ID.

        Args:
            job_id: Job-ID

        Returns:
            Job

        Raises:
            NotFoundException: Wenn Job nicht existiert
        """
        job = await self.db.get(Job, job_id)
        if not job:
            raise NotFoundException(
                message=f"Job {job_id} nicht gefunden",
            )
        return job

    async def update_job(self, job_id: UUID, data: JobUpdate) -> Job:
        """
        Aktualisiert einen Job.

        Args:
            job_id: Job-ID
            data: JobUpdate-Schema

        Returns:
            Aktualisierter Job
        """
        job = await self.get_job(job_id)

        update_data = data.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            setattr(job, field, value)

        await self.db.commit()
        await self.db.refresh(job)

        logger.info(f"Job aktualisiert: {job_id}")
        return job

    async def soft_delete_job(self, job_id: UUID) -> Job:
        """
        Soft-Delete eines Jobs.

        Args:
            job_id: Job-ID

        Returns:
            Gelöschter Job
        """
        job = await self.get_job(job_id)
        job.deleted_at = datetime.now(timezone.utc)
        await self.db.commit()

        logger.info(f"Job soft-deleted: {job_id}")
        return job

    async def restore_job(self, job_id: UUID) -> Job:
        """
        Stellt einen soft-deleted Job wieder her.

        Args:
            job_id: Job-ID

        Returns:
            Wiederhergestellter Job
        """
        job = await self.get_job(job_id)
        job.deleted_at = None
        await self.db.commit()

        logger.info(f"Job wiederhergestellt: {job_id}")
        return job

    async def batch_delete(self, job_ids: list[UUID]) -> int:
        """
        Soft-Delete mehrerer Jobs.

        Args:
            job_ids: Liste der Job-IDs (max. 100)

        Returns:
            Anzahl gelöschter Jobs
        """
        if len(job_ids) > Limits.BATCH_DELETE_MAX:
            raise ValueError(
                f"Maximal {Limits.BATCH_DELETE_MAX} Jobs pro Batch erlaubt"
            )

        now = datetime.now(timezone.utc)
        result = await self.db.execute(
            select(Job).where(
                Job.id.in_(job_ids),
                Job.deleted_at.is_(None),
            )
        )
        jobs = result.scalars().all()

        for job in jobs:
            job.deleted_at = now

        await self.db.commit()

        logger.info(f"Batch-Delete: {len(jobs)} Jobs gelöscht")
        return len(jobs)

    async def permanently_delete_job(self, job_id: UUID) -> bool:
        """
        Löscht einen Job permanent.

        Args:
            job_id: Job-ID

        Returns:
            True bei Erfolg
        """
        job = await self.get_job(job_id)
        await self.db.delete(job)
        await self.db.commit()

        logger.info(f"Job permanent gelöscht: {job_id}")
        return True

    async def exclude_from_deletion(self, job_id: UUID, exclude: bool) -> Job:
        """
        Schließt einen Job von der Auto-Löschung aus.

        Args:
            job_id: Job-ID
            exclude: True = ausschließen, False = einschließen

        Returns:
            Aktualisierter Job
        """
        job = await self.get_job(job_id)
        job.excluded_from_deletion = exclude
        await self.db.commit()

        action = "von Löschung ausgeschlossen" if exclude else "für Löschung freigegeben"
        logger.info(f"Job {job_id}: {action}")
        return job

    async def list_jobs(
        self,
        filters: JobFilterParams,
        page: int = 1,
        per_page: int = Limits.PAGE_SIZE_DEFAULT,
    ) -> PaginatedResponse:
        """
        Listet Jobs mit Filterung und Pagination.

        Args:
            filters: Filter-Parameter
            page: Seitennummer
            per_page: Einträge pro Seite

        Returns:
            PaginatedResponse mit Jobs
        """
        # Basis-Query
        query = select(Job)

        # Gelöschte ausschließen (außer explizit angefordert)
        if not filters.include_deleted:
            query = query.where(Job.deleted_at.is_(None))

        # Abgelaufene ausschließen (außer explizit angefordert)
        if not filters.include_expired:
            query = query.where(
                or_(
                    Job.expires_at.is_(None),
                    Job.expires_at > datetime.now(timezone.utc),
                )
            )

        # Filter anwenden
        query = self._apply_filters(query, filters)

        # Sortierung mit Prio-Städte Unterstützung
        query = await self._apply_sorting(query, filters)

        # Gesamtanzahl ermitteln
        count_query = select(func.count()).select_from(query.subquery())
        total_result = await self.db.execute(count_query)
        total = total_result.scalar() or 0

        # Pagination
        offset = (page - 1) * per_page
        query = query.offset(offset).limit(per_page)

        # Ausführen
        result = await self.db.execute(query)
        jobs = result.scalars().all()

        # Match-Counts hinzufügen
        jobs_with_counts = await self._add_match_counts(jobs)

        pages = (total + per_page - 1) // per_page if per_page > 0 else 0

        return PaginatedResponse(
            items=jobs_with_counts,
            total=total,
            page=page,
            per_page=per_page,
            pages=pages,
        )

    def _apply_filters(self, query, filters: JobFilterParams):
        """Wendet Filter auf die Query an."""
        # Textsuche (Position oder Unternehmen)
        if filters.search:
            search_term = f"%{filters.search}%"
            query = query.where(
                or_(
                    Job.position.ilike(search_term),
                    Job.company_name.ilike(search_term),
                )
            )

        # Städte-Filter
        if filters.cities:
            query = query.where(
                or_(
                    Job.city.in_(filters.cities),
                    Job.work_location_city.in_(filters.cities),
                )
            )

        # Branchen-Filter
        if filters.industries:
            query = query.where(Job.industry.in_(filters.industries))

        # Unternehmen-Filter
        if filters.company:
            query = query.where(Job.company_name.ilike(f"%{filters.company}%"))

        # Position-Filter
        if filters.position:
            query = query.where(Job.position.ilike(f"%{filters.position}%"))

        # Datum-Filter
        if filters.created_after:
            query = query.where(Job.created_at >= filters.created_after)

        if filters.created_before:
            query = query.where(Job.created_at <= filters.created_before)

        if filters.expires_after:
            query = query.where(Job.expires_at >= filters.expires_after)

        if filters.expires_before:
            query = query.where(Job.expires_at <= filters.expires_before)

        return query

    async def _apply_sorting(self, query, filters: JobFilterParams):
        """
        Wendet Sortierung an mit Prio-Städte Unterstützung.

        Prio-Städte werden immer zuerst angezeigt.
        """
        # Prio-Städte laden
        prio_result = await self.db.execute(
            select(PriorityCity).order_by(PriorityCity.priority_order)
        )
        priority_cities = prio_result.scalars().all()

        if priority_cities:
            # CASE-Statement für Prio-Städte Sortierung
            city_order_cases = []
            for i, pc in enumerate(priority_cities):
                city_order_cases.append(
                    (or_(Job.city == pc.city_name, Job.work_location_city == pc.city_name), i)
                )

            # Fallback: Andere Städte ganz hinten
            city_order = func.coalesce(
                func.case(*city_order_cases),
                len(priority_cities) + 1,  # Nicht-Prio Städte nach den Prio-Städten
            )

            query = query.order_by(city_order)

        # Sekundäre Sortierung nach gewähltem Feld
        sort_column = getattr(Job, filters.sort_by.value, Job.created_at)
        if filters.sort_order.value == "desc":
            query = query.order_by(sort_column.desc())
        else:
            query = query.order_by(sort_column.asc())

        return query

    async def _add_match_counts(self, jobs: Sequence[Job]) -> list[dict]:
        """Fügt Match-Counts zu Jobs hinzu."""
        if not jobs:
            return []

        job_ids = [job.id for job in jobs]

        # Match-Counts abfragen
        count_query = (
            select(Match.job_id, func.count(Match.id).label("count"))
            .where(Match.job_id.in_(job_ids))
            .group_by(Match.job_id)
        )
        result = await self.db.execute(count_query)
        counts = {row.job_id: row.count for row in result}

        # Active Candidate Counts (Kandidaten ≤30 Tage)
        active_threshold = datetime.now(timezone.utc) - timedelta(
            days=Limits.ACTIVE_CANDIDATE_DAYS
        )
        # Hinweis: Für aktive Kandidaten müsste ein JOIN mit candidates gemacht werden
        # Wird in späteren Phasen implementiert

        jobs_data = []
        for job in jobs:
            job_dict = {
                "id": job.id,
                "company_name": job.company_name,
                "position": job.position,
                "street_address": job.street_address,
                "postal_code": job.postal_code,
                "city": job.city,
                "work_location_city": job.work_location_city,
                "display_city": job.display_city,
                "job_url": job.job_url,
                "job_text": job.job_text,
                "employment_type": job.employment_type,
                "industry": job.industry,
                "company_size": job.company_size,
                "has_coordinates": job.location_coords is not None,
                "expires_at": job.expires_at,
                "excluded_from_deletion": job.excluded_from_deletion,
                "is_deleted": job.is_deleted,
                "is_expired": job.is_expired,
                "created_at": job.created_at,
                "updated_at": job.updated_at,
                "match_count": counts.get(job.id, 0),
                "active_candidate_count": None,  # Später implementieren
            }
            jobs_data.append(job_dict)

        return jobs_data

    async def get_expiring_jobs(self, days: int = 7) -> Sequence[Job]:
        """
        Holt Jobs, die in den nächsten X Tagen ablaufen.

        Args:
            days: Anzahl Tage

        Returns:
            Liste der Jobs
        """
        threshold = datetime.now(timezone.utc) + timedelta(days=days)

        result = await self.db.execute(
            select(Job).where(
                Job.deleted_at.is_(None),
                Job.expires_at.isnot(None),
                Job.expires_at <= threshold,
                Job.expires_at > datetime.now(timezone.utc),
            )
        )
        return result.scalars().all()

    async def get_jobs_without_coords(self) -> Sequence[Job]:
        """
        Holt Jobs ohne Koordinaten.

        Returns:
            Liste der Jobs
        """
        result = await self.db.execute(
            select(Job).where(
                Job.deleted_at.is_(None),
                Job.location_coords.is_(None),
            )
        )
        return result.scalars().all()

    async def count_active_jobs(self) -> int:
        """
        Zählt aktive Jobs.

        Returns:
            Anzahl aktiver Jobs
        """
        result = await self.db.execute(
            select(func.count(Job.id)).where(
                Job.deleted_at.is_(None),
                or_(
                    Job.expires_at.is_(None),
                    Job.expires_at > datetime.now(timezone.utc),
                ),
            )
        )
        return result.scalar() or 0
