"""Admin API Routes - Endpoints für Background-Jobs und System-Verwaltung."""

import logging
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.exception_handlers import ConflictException, NotFoundException
from app.api.rate_limiter import RateLimitTier, rate_limit
from app.config import settings
from app.database import get_db
from app.models.job_run import JobRunStatus, JobSource, JobType
from app.services.job_runner_service import JobRunnerService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["Admin"])


# ==================== Schemas ====================

class JobStatusResponse(BaseModel):
    """Schema für Job-Status Response."""
    job_type: str
    is_running: bool
    current_job: dict | None
    last_job: dict | None
    pending_items: int


class JobTriggerResponse(BaseModel):
    """Schema für Job-Trigger Response."""
    message: str
    job_run_id: str
    job_type: str
    source: str


# ==================== Cron-Authentifizierung ====================

async def verify_cron_secret(
    x_cron_secret: str | None = Header(default=None, alias="X-Cron-Secret"),
):
    """
    Verifiziert das Cron-Secret für automatisierte Jobs.

    Nur nötig für Cron-Aufrufe, manuelle Trigger brauchen kein Secret.
    """
    if x_cron_secret and settings.cron_secret:
        if x_cron_secret != settings.cron_secret:
            raise ConflictException(message="Ungültiges Cron-Secret")
    return x_cron_secret


# ==================== Geocoding ====================

@router.post(
    "/geocoding/trigger",
    response_model=JobTriggerResponse,
    summary="Geocoding starten",
)
@rate_limit(RateLimitTier.ADMIN)
async def trigger_geocoding(
    background_tasks: BackgroundTasks,
    source: str = Query(default="manual", pattern="^(manual|cron)$"),
    db: AsyncSession = Depends(get_db),
    cron_secret: str | None = Depends(verify_cron_secret),
):
    """
    Startet den Geocoding-Job.

    Geokodiert alle Jobs und Kandidaten ohne Koordinaten.
    Rate-Limit: 1 Request/Sekunde bei Nominatim.
    """
    job_runner = JobRunnerService(db)

    if await job_runner.is_running(JobType.GEOCODING):
        raise ConflictException(message="Geocoding läuft bereits")

    job_source = JobSource.CRON if source == "cron" else JobSource.MANUAL
    job_run = await job_runner.start_job(JobType.GEOCODING, job_source)

    background_tasks.add_task(_run_geocoding, db, job_run.id)

    return JobTriggerResponse(
        message="Geocoding gestartet",
        job_run_id=str(job_run.id),
        job_type=JobType.GEOCODING.value,
        source=job_source.value,
    )


@router.get(
    "/geocoding/status",
    response_model=JobStatusResponse,
    summary="Geocoding-Status",
)
async def get_geocoding_status(
    db: AsyncSession = Depends(get_db),
):
    """Gibt den aktuellen Geocoding-Status zurück."""
    job_runner = JobRunnerService(db)
    status_data = await job_runner.get_status(JobType.GEOCODING)
    return JobStatusResponse(**status_data)


# ==================== CRM-Sync ====================

@router.post(
    "/crm-sync/trigger",
    response_model=JobTriggerResponse,
    summary="CRM-Sync starten",
)
@rate_limit(RateLimitTier.ADMIN)
async def trigger_crm_sync(
    background_tasks: BackgroundTasks,
    source: str = Query(default="manual", pattern="^(manual|cron)$"),
    full_sync: bool = Query(default=False),
    db: AsyncSession = Depends(get_db),
    cron_secret: str | None = Depends(verify_cron_secret),
):
    """
    Startet den CRM-Sync.

    - full_sync=False: Nur seit letztem Sync geänderte Kandidaten
    - full_sync=True: Alle Kandidaten (Initial-Sync)
    """
    job_runner = JobRunnerService(db)

    if await job_runner.is_running(JobType.CRM_SYNC):
        raise ConflictException(message="CRM-Sync läuft bereits")

    job_source = JobSource.CRON if source == "cron" else JobSource.MANUAL
    job_run = await job_runner.start_job(JobType.CRM_SYNC, job_source)

    background_tasks.add_task(_run_crm_sync, db, job_run.id, full_sync)

    return JobTriggerResponse(
        message="CRM-Sync gestartet",
        job_run_id=str(job_run.id),
        job_type=JobType.CRM_SYNC.value,
        source=job_source.value,
    )


@router.get(
    "/crm-sync/status",
    response_model=JobStatusResponse,
    summary="CRM-Sync-Status",
)
async def get_crm_sync_status(
    db: AsyncSession = Depends(get_db),
):
    """Gibt den aktuellen CRM-Sync-Status zurück."""
    job_runner = JobRunnerService(db)
    status_data = await job_runner.get_status(JobType.CRM_SYNC)
    return JobStatusResponse(**status_data)


# ==================== Matching ====================

@router.post(
    "/matching/trigger",
    response_model=JobTriggerResponse,
    summary="Matching starten",
)
@rate_limit(RateLimitTier.ADMIN)
async def trigger_matching(
    background_tasks: BackgroundTasks,
    source: str = Query(default="manual", pattern="^(manual|cron)$"),
    db: AsyncSession = Depends(get_db),
    cron_secret: str | None = Depends(verify_cron_secret),
):
    """
    Startet den Matching-Job.

    Berechnet Matches für alle aktiven Jobs neu.
    """
    job_runner = JobRunnerService(db)

    if await job_runner.is_running(JobType.MATCHING):
        raise ConflictException(message="Matching läuft bereits")

    job_source = JobSource.CRON if source == "cron" else JobSource.MANUAL
    job_run = await job_runner.start_job(JobType.MATCHING, job_source)

    background_tasks.add_task(_run_matching, db, job_run.id)

    return JobTriggerResponse(
        message="Matching gestartet",
        job_run_id=str(job_run.id),
        job_type=JobType.MATCHING.value,
        source=job_source.value,
    )


@router.get(
    "/matching/status",
    response_model=JobStatusResponse,
    summary="Matching-Status",
)
async def get_matching_status(
    db: AsyncSession = Depends(get_db),
):
    """Gibt den aktuellen Matching-Status zurück."""
    job_runner = JobRunnerService(db)
    status_data = await job_runner.get_status(JobType.MATCHING)
    return JobStatusResponse(**status_data)


# ==================== Cleanup ====================

@router.post(
    "/cleanup/trigger",
    response_model=JobTriggerResponse,
    summary="Cleanup starten",
)
@rate_limit(RateLimitTier.ADMIN)
async def trigger_cleanup(
    background_tasks: BackgroundTasks,
    source: str = Query(default="manual", pattern="^(manual|cron)$"),
    db: AsyncSession = Depends(get_db),
    cron_secret: str | None = Depends(verify_cron_secret),
):
    """
    Startet den Cleanup-Job.

    Löscht:
    - Abgelaufene Jobs (nicht excluded_from_deletion)
    - Verwaiste Matches
    """
    job_runner = JobRunnerService(db)

    if await job_runner.is_running(JobType.CLEANUP):
        raise ConflictException(message="Cleanup läuft bereits")

    job_source = JobSource.CRON if source == "cron" else JobSource.MANUAL
    job_run = await job_runner.start_job(JobType.CLEANUP, job_source)

    background_tasks.add_task(_run_cleanup, db, job_run.id)

    return JobTriggerResponse(
        message="Cleanup gestartet",
        job_run_id=str(job_run.id),
        job_type=JobType.CLEANUP.value,
        source=job_source.value,
    )


@router.get(
    "/cleanup/status",
    response_model=JobStatusResponse,
    summary="Cleanup-Status",
)
async def get_cleanup_status(
    db: AsyncSession = Depends(get_db),
):
    """Gibt den aktuellen Cleanup-Status zurück."""
    job_runner = JobRunnerService(db)
    status_data = await job_runner.get_status(JobType.CLEANUP)
    return JobStatusResponse(**status_data)


# ==================== Job-Historie ====================

@router.get(
    "/jobs/history",
    summary="Job-Historie",
)
async def get_job_history(
    job_type: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """
    Gibt die Historie aller Background-Jobs zurück.

    Kann nach Job-Typ gefiltert werden.
    """
    job_runner = JobRunnerService(db)

    type_filter = None
    if job_type:
        try:
            type_filter = JobType(job_type)
        except ValueError:
            pass

    history = await job_runner.get_job_history(job_type=type_filter, limit=limit)

    return {
        "items": [job_runner._job_to_dict(j) for j in history],
        "total": len(history),
    }


@router.get(
    "/jobs/{job_run_id}",
    summary="Job-Details",
)
async def get_job_run(
    job_run_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Gibt die Details eines spezifischen Job-Runs zurück."""
    from app.models.job_run import JobRun

    job_run = await db.get(JobRun, job_run_id)
    if not job_run:
        raise NotFoundException(message="Job-Run nicht gefunden")

    job_runner = JobRunnerService(db)
    return job_runner._job_to_dict(job_run)


@router.post(
    "/jobs/{job_run_id}/cancel",
    summary="Job abbrechen",
)
@rate_limit(RateLimitTier.ADMIN)
async def cancel_job_run(
    job_run_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Bricht einen laufenden Job ab."""
    job_runner = JobRunnerService(db)
    job_run = await job_runner.cancel_job(job_run_id)

    if not job_run:
        raise NotFoundException(message="Job-Run nicht gefunden")

    return job_runner._job_to_dict(job_run)


# ==================== Übersicht ====================

@router.get(
    "/status",
    summary="Gesamtstatus aller Jobs",
)
async def get_all_job_status(
    db: AsyncSession = Depends(get_db),
):
    """Gibt den Status aller Job-Typen zurück."""
    job_runner = JobRunnerService(db)

    return {
        "geocoding": await job_runner.get_status(JobType.GEOCODING),
        "crm_sync": await job_runner.get_status(JobType.CRM_SYNC),
        "matching": await job_runner.get_status(JobType.MATCHING),
        "cleanup": await job_runner.get_status(JobType.CLEANUP),
        "cv_parsing": await job_runner.get_status(JobType.CV_PARSING),
    }


# ==================== Background-Task-Funktionen ====================

async def _run_geocoding(db: AsyncSession, job_run_id: UUID):
    """Führt Geocoding im Hintergrund aus."""
    from app.services.geocoding_service import GeocodingService

    job_runner = JobRunnerService(db)

    try:
        geocoding_service = GeocodingService(db)
        result = await geocoding_service.process_all_pending()

        await job_runner.complete_job(
            job_run_id,
            items_total=result.total,
            items_successful=result.successful,
            items_failed=result.failed,
        )
    except Exception as e:
        logger.error(f"Geocoding fehlgeschlagen: {e}")
        await job_runner.fail_job(job_run_id, str(e))


async def _run_crm_sync(db: AsyncSession, job_run_id: UUID, full_sync: bool):
    """Führt CRM-Sync im Hintergrund aus."""
    from app.services.crm_sync_service import CRMSyncService

    job_runner = JobRunnerService(db)

    try:
        sync_service = CRMSyncService(db)

        if full_sync:
            result = await sync_service.initial_sync()
        else:
            result = await sync_service.sync_all()

        await job_runner.complete_job(
            job_run_id,
            items_total=result.total_candidates,
            items_successful=result.created + result.updated,
            items_failed=result.errors,
        )
    except Exception as e:
        logger.error(f"CRM-Sync fehlgeschlagen: {e}")
        await job_runner.fail_job(job_run_id, str(e))


async def _run_matching(db: AsyncSession, job_run_id: UUID):
    """Führt Matching im Hintergrund aus."""
    from app.services.matching_service import MatchingService

    job_runner = JobRunnerService(db)

    try:
        matching_service = MatchingService(db)
        result = await matching_service.recalculate_all_matches()

        await job_runner.complete_job(
            job_run_id,
            items_total=result.jobs_processed,
            items_successful=result.total_matches_created + result.total_matches_updated,
            items_failed=len(result.errors),
        )
    except Exception as e:
        logger.error(f"Matching fehlgeschlagen: {e}")
        await job_runner.fail_job(job_run_id, str(e))


async def _run_cleanup(db: AsyncSession, job_run_id: UUID):
    """Führt Cleanup im Hintergrund aus."""
    from datetime import datetime
    from sqlalchemy import and_, update
    from app.models.job import Job
    from app.services.matching_service import MatchingService

    job_runner = JobRunnerService(db)

    try:
        # Abgelaufene Jobs soft-deleten
        result = await db.execute(
            update(Job)
            .where(
                and_(
                    Job.expires_at < datetime.utcnow(),
                    Job.deleted_at.is_(None),
                    Job.excluded_from_deletion == False,  # noqa: E712
                )
            )
            .values(deleted_at=datetime.utcnow())
        )
        deleted_jobs = result.rowcount

        # Verwaiste Matches löschen
        matching_service = MatchingService(db)
        deleted_matches = await matching_service.cleanup_orphaned_matches()

        await db.commit()

        await job_runner.complete_job(
            job_run_id,
            items_total=deleted_jobs + deleted_matches,
            items_successful=deleted_jobs + deleted_matches,
            items_failed=0,
        )
    except Exception as e:
        logger.error(f"Cleanup fehlgeschlagen: {e}")
        await job_runner.fail_job(job_run_id, str(e))
