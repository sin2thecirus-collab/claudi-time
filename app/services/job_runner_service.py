"""Job Runner Service - Verwaltet Background-Jobs."""

import logging
import uuid
from datetime import datetime

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.candidate import Candidate
from app.models.job import Job
from app.models.job_run import JobRun, JobRunStatus, JobSource, JobType

logger = logging.getLogger(__name__)


class JobRunnerService:
    """Service für Background-Job-Management."""

    def __init__(self, db: AsyncSession):
        """Initialisiert den JobRunnerService."""
        self.db = db

    async def is_running(self, job_type: JobType) -> bool:
        """
        Prüft, ob ein Job dieses Typs bereits läuft.

        Args:
            job_type: Typ des Jobs

        Returns:
            True wenn ein Job läuft
        """
        query = select(func.count()).where(
            and_(
                JobRun.job_type == job_type,
                JobRun.status.in_([JobRunStatus.PENDING, JobRunStatus.RUNNING]),
            )
        )
        result = await self.db.execute(query)
        count = result.scalar()
        return count > 0

    async def start_job(
        self,
        job_type: JobType,
        source: JobSource = JobSource.MANUAL,
    ) -> JobRun:
        """
        Startet einen neuen Job.

        Args:
            job_type: Typ des Jobs
            source: Quelle (manual, cron, system)

        Returns:
            Neuer JobRun

        Raises:
            ValueError: Wenn bereits ein Job läuft
        """
        if await self.is_running(job_type):
            raise ValueError(f"Ein {job_type.value}-Job läuft bereits")

        job_run = JobRun(
            job_type=job_type,
            source=source,
            status=JobRunStatus.RUNNING,
            started_at=datetime.utcnow(),
        )
        self.db.add(job_run)
        await self.db.commit()
        await self.db.refresh(job_run)

        logger.info(f"Job gestartet: {job_type.value} (ID: {job_run.id})")
        return job_run

    async def update_progress(
        self,
        job_run_id: uuid.UUID,
        items_processed: int,
        items_total: int | None = None,
        items_successful: int | None = None,
        items_failed: int | None = None,
    ) -> JobRun | None:
        """
        Aktualisiert den Fortschritt eines Jobs.

        Args:
            job_run_id: ID des JobRuns
            items_processed: Anzahl verarbeiteter Items
            items_total: Gesamtanzahl (optional, wenn bekannt)
            items_successful: Anzahl erfolgreicher Items
            items_failed: Anzahl fehlgeschlagener Items

        Returns:
            Aktualisierter JobRun
        """
        job_run = await self.db.get(JobRun, job_run_id)
        if not job_run:
            return None

        job_run.items_processed = items_processed
        if items_total is not None:
            job_run.items_total = items_total
        if items_successful is not None:
            job_run.items_successful = items_successful
        if items_failed is not None:
            job_run.items_failed = items_failed

        await self.db.commit()
        await self.db.refresh(job_run)

        return job_run

    async def complete_job(
        self,
        job_run_id: uuid.UUID,
        items_total: int | None = None,
        items_successful: int | None = None,
        items_failed: int | None = None,
    ) -> JobRun | None:
        """
        Markiert einen Job als abgeschlossen.

        Args:
            job_run_id: ID des JobRuns
            items_total: Gesamtanzahl (optional)
            items_successful: Anzahl erfolgreicher Items
            items_failed: Anzahl fehlgeschlagener Items

        Returns:
            Aktualisierter JobRun
        """
        job_run = await self.db.get(JobRun, job_run_id)
        if not job_run:
            return None

        job_run.status = JobRunStatus.COMPLETED
        job_run.completed_at = datetime.utcnow()

        if items_total is not None:
            job_run.items_total = items_total
            job_run.items_processed = items_total
        if items_successful is not None:
            job_run.items_successful = items_successful
        if items_failed is not None:
            job_run.items_failed = items_failed

        await self.db.commit()
        await self.db.refresh(job_run)

        logger.info(
            f"Job abgeschlossen: {job_run.job_type.value} "
            f"(Erfolg: {job_run.items_successful}, Fehler: {job_run.items_failed})"
        )
        return job_run

    async def fail_job(
        self,
        job_run_id: uuid.UUID,
        error_message: str,
        errors_detail: dict | None = None,
    ) -> JobRun | None:
        """
        Markiert einen Job als fehlgeschlagen.

        Args:
            job_run_id: ID des JobRuns
            error_message: Fehlermeldung
            errors_detail: Detaillierte Fehlerdaten

        Returns:
            Aktualisierter JobRun
        """
        job_run = await self.db.get(JobRun, job_run_id)
        if not job_run:
            return None

        job_run.status = JobRunStatus.FAILED
        job_run.completed_at = datetime.utcnow()
        job_run.error_message = error_message
        job_run.errors_detail = errors_detail

        await self.db.commit()
        await self.db.refresh(job_run)

        logger.error(f"Job fehlgeschlagen: {job_run.job_type.value} - {error_message}")
        return job_run

    async def cancel_job(self, job_run_id: uuid.UUID) -> JobRun | None:
        """
        Bricht einen laufenden Job ab.

        Args:
            job_run_id: ID des JobRuns

        Returns:
            Aktualisierter JobRun
        """
        job_run = await self.db.get(JobRun, job_run_id)
        if not job_run:
            return None

        if not job_run.is_running:
            return job_run

        job_run.status = JobRunStatus.CANCELLED
        job_run.completed_at = datetime.utcnow()

        await self.db.commit()
        await self.db.refresh(job_run)

        logger.info(f"Job abgebrochen: {job_run.job_type.value}")
        return job_run

    async def get_status(self, job_type: JobType) -> dict:
        """
        Gibt den aktuellen Status für einen Job-Typ zurück.

        Args:
            job_type: Typ des Jobs

        Returns:
            Status-Dict mit aktuellen Informationen
        """
        # Laufender Job?
        running_query = select(JobRun).where(
            and_(
                JobRun.job_type == job_type,
                JobRun.status.in_([JobRunStatus.PENDING, JobRunStatus.RUNNING]),
            )
        )
        running_result = await self.db.execute(running_query)
        running_job = running_result.scalar_one_or_none()

        # Letzter abgeschlossener Job
        last_query = (
            select(JobRun)
            .where(
                and_(
                    JobRun.job_type == job_type,
                    JobRun.status.in_([JobRunStatus.COMPLETED, JobRunStatus.FAILED]),
                )
            )
            .order_by(JobRun.completed_at.desc())
            .limit(1)
        )
        last_result = await self.db.execute(last_query)
        last_job = last_result.scalar_one_or_none()

        # Anzahl ausstehender Items
        pending_count = await self._count_pending_items(job_type)

        return {
            "job_type": job_type.value,
            "is_running": running_job is not None,
            "current_job": self._job_to_dict(running_job) if running_job else None,
            "last_job": self._job_to_dict(last_job) if last_job else None,
            "pending_items": pending_count,
        }

    async def get_job_history(
        self,
        job_type: JobType | None = None,
        limit: int = 10,
    ) -> list[JobRun]:
        """
        Gibt die Job-Historie zurück.

        Args:
            job_type: Optional, nur Jobs dieses Typs
            limit: Maximale Anzahl

        Returns:
            Liste der JobRuns
        """
        query = select(JobRun).order_by(JobRun.created_at.desc()).limit(limit)

        if job_type:
            query = query.where(JobRun.job_type == job_type)

        result = await self.db.execute(query)
        return list(result.scalars().all())

    async def _count_pending_items(self, job_type: JobType) -> int:
        """Zählt ausstehende Items für einen Job-Typ."""
        if job_type == JobType.GEOCODING:
            # Jobs ohne Koordinaten
            jobs_query = select(func.count()).where(
                and_(
                    Job.location_coords.is_(None),
                    Job.deleted_at.is_(None),
                    Job.city.is_not(None),
                )
            )
            jobs_result = await self.db.execute(jobs_query)
            jobs_count = jobs_result.scalar() or 0

            # Kandidaten ohne Koordinaten
            candidates_query = select(func.count()).where(
                and_(
                    Candidate.address_coords.is_(None),
                    Candidate.hidden == False,  # noqa: E712
                    Candidate.city.is_not(None),
                )
            )
            candidates_result = await self.db.execute(candidates_query)
            candidates_count = candidates_result.scalar() or 0

            return jobs_count + candidates_count

        elif job_type == JobType.MATCHING:
            # Jobs mit Koordinaten (alle könnten gematcht werden)
            query = select(func.count()).where(
                and_(
                    Job.location_coords.is_not(None),
                    Job.deleted_at.is_(None),
                )
            )
            result = await self.db.execute(query)
            return result.scalar() or 0

        elif job_type == JobType.CV_PARSING:
            # Kandidaten mit CV-URL aber ohne Parsing
            query = select(func.count()).where(
                and_(
                    Candidate.cv_url.is_not(None),
                    Candidate.cv_parsed_at.is_(None),
                    Candidate.hidden == False,  # noqa: E712
                )
            )
            result = await self.db.execute(query)
            return result.scalar() or 0

        elif job_type == JobType.CLEANUP:
            # Abgelaufene Jobs
            query = select(func.count()).where(
                and_(
                    Job.expires_at < datetime.utcnow(),
                    Job.deleted_at.is_(None),
                    Job.excluded_from_deletion == False,  # noqa: E712
                )
            )
            result = await self.db.execute(query)
            return result.scalar() or 0

        return 0

    def _job_to_dict(self, job_run: JobRun | None) -> dict | None:
        """Konvertiert einen JobRun zu einem Dict."""
        if not job_run:
            return None

        return {
            "id": str(job_run.id),
            "job_type": job_run.job_type.value,
            "source": job_run.source.value,
            "status": job_run.status.value,
            "items_total": job_run.items_total,
            "items_processed": job_run.items_processed,
            "items_successful": job_run.items_successful,
            "items_failed": job_run.items_failed,
            "progress_percent": job_run.progress_percent,
            "error_message": job_run.error_message,
            "started_at": job_run.started_at.isoformat() if job_run.started_at else None,
            "completed_at": job_run.completed_at.isoformat() if job_run.completed_at else None,
            "duration_seconds": job_run.duration_seconds,
        }
