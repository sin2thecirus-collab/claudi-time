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


# ==================== Debug: CRM-Verbindungstest ====================

@router.get(
    "/test-crm",
    summary="CRM-Verbindung testen",
)
async def test_crm_connection():
    """
    Testet die Verbindung zur Recruit CRM API.

    Gibt detaillierte Informationen über:
    - API-Key Status
    - Base-URL
    - API-Antwort
    """
    from app.services.crm_client import RecruitCRMClient, CRMError

    result = {
        "api_key_configured": bool(settings.recruit_crm_api_key),
        "api_key_length": len(settings.recruit_crm_api_key) if settings.recruit_crm_api_key else 0,
        "api_key_prefix": settings.recruit_crm_api_key[:10] + "..." if len(settings.recruit_crm_api_key) > 10 else "zu kurz",
        "base_url": settings.recruit_crm_base_url,
        "connection_test": None,
        "candidates_count": None,
        "error": None,
    }

    if not settings.recruit_crm_api_key:
        result["error"] = "RECRUIT_CRM_API_KEY ist nicht konfiguriert!"
        return result

    try:
        import httpx

        # Direkter API-Test ohne den CRM-Client
        async with httpx.AsyncClient() as http_client:
            # Test 1: Einfacher Request ohne Parameter
            test_response = await http_client.get(
                f"{settings.recruit_crm_base_url}/candidates",
                headers={
                    "Authorization": f"Bearer {settings.recruit_crm_api_key}",
                    "Accept": "application/json",
                },
                timeout=15.0,
            )

            result["raw_status_code"] = test_response.status_code
            result["raw_response_preview"] = test_response.text[:500] if test_response.text else "Leer"

            if test_response.status_code == 200:
                result["connection_test"] = "ERFOLGREICH"
                data = test_response.json()
                result["candidates_count"] = data.get("total", len(data.get("data", [])))
                result["response_keys"] = list(data.keys()) if isinstance(data, dict) else "Keine Dict-Antwort"
                if data.get("data"):
                    first = data["data"][0]
                    result["first_candidate_keys"] = list(first.keys()) if isinstance(first, dict) else None
                    result["first_candidate_name"] = f"{first.get('first_name', '')} {first.get('last_name', '')}"
            else:
                result["connection_test"] = "FEHLGESCHLAGEN"
                result["error"] = f"HTTP {test_response.status_code}"

    except Exception as e:
        result["connection_test"] = "FEHLGESCHLAGEN"
        result["error"] = f"Fehler: {str(e)}"

    return result


@router.post(
    "/import-all",
    summary="Alle Kandidaten direkt importieren (kein Background Task)",
)
async def import_all_candidates(
    db: AsyncSession = Depends(get_db),
    max_pages: int = Query(default=100, description="Maximale Seitenzahl"),
):
    """
    Importiert ALLE Kandidaten direkt in der Request-Session.
    Kein Background Task — verwendet die bewährte DB-Session aus get_db.
    """
    import traceback
    from datetime import datetime, timezone

    from sqlalchemy import select

    from app.models.candidate import Candidate
    from app.services.crm_client import RecruitCRMClient

    created = 0
    updated = 0
    failed = 0
    errors = []

    try:
        client = RecruitCRMClient()

        async for page_num, candidates, estimated_total in client.get_all_candidates_paginated(
            per_page=100, max_pages=max_pages
        ):
            logger.info(f"Import Seite {page_num}: {len(candidates)} Kandidaten")

            for crm_data in candidates:
                try:
                    mapped = client.map_to_candidate_data(crm_data)
                    crm_id = mapped.get("crm_id")
                    if not crm_id:
                        failed += 1
                        continue

                    result = await db.execute(
                        select(Candidate).where(Candidate.crm_id == crm_id)
                    )
                    existing = result.scalar_one_or_none()
                    now = datetime.now(timezone.utc)

                    if existing:
                        for key, value in mapped.items():
                            if key != "crm_id" and value is not None:
                                setattr(existing, key, value)
                        existing.crm_synced_at = now
                        existing.updated_at = now
                        updated += 1
                    else:
                        candidate = Candidate(
                            crm_id=crm_id,
                            first_name=mapped.get("first_name"),
                            last_name=mapped.get("last_name"),
                            email=mapped.get("email"),
                            phone=mapped.get("phone"),
                            current_position=mapped.get("current_position"),
                            current_company=mapped.get("current_company"),
                            skills=mapped.get("skills"),
                            street_address=mapped.get("street_address"),
                            postal_code=mapped.get("postal_code"),
                            city=mapped.get("city"),
                            cv_url=mapped.get("cv_url"),
                            crm_synced_at=now,
                        )
                        db.add(candidate)
                        created += 1

                except Exception as e:
                    failed += 1
                    errors.append(f"{crm_data.get('slug')}: {e}")

            # Commit nach jeder Seite
            await db.commit()
            logger.info(f"Seite {page_num} committed: {created} erstellt, {updated} aktualisiert")

        await client.close()

        return {
            "success": True,
            "created": created,
            "updated": updated,
            "failed": failed,
            "total": created + updated + failed,
            "errors": errors[:20],
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc(),
            "created": created,
            "updated": updated,
            "failed": failed,
        }


@router.post(
    "/migrate-cv-url",
    summary="Einmalige DB-Migration: cv_url Spalte vergrößern",
)
async def migrate_cv_url(db: AsyncSession = Depends(get_db)):
    """Ändert cv_url von VARCHAR(500) auf TEXT."""
    from sqlalchemy import text

    try:
        await db.execute(text("ALTER TABLE candidates ALTER COLUMN cv_url TYPE TEXT"))
        await db.commit()
        return {"success": True, "message": "cv_url auf TEXT geändert"}
    except Exception as e:
        return {"success": False, "error": str(e)}


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
    full_sync: bool = Query(default=True, description="Alle Kandidaten importieren (True) oder nur Änderungen (False)"),
    parse_cvs: bool = Query(
        default=False,
        description="CVs mit OpenAI parsen für fehlende Daten (aktuell deaktiviert)"
    ),
    db: AsyncSession = Depends(get_db),
    cron_secret: str | None = Depends(verify_cron_secret),
):
    """
    Startet den CRM-Sync.

    - full_sync=False: Nur seit letztem Sync geänderte Kandidaten
    - full_sync=True: Alle Kandidaten (Initial-Sync)
    - parse_cvs=True: CVs mit OpenAI analysieren für:
        - Aktuelle Position
        - Beruflicher Werdegang (Work History)
        - Vollständige Adresse (Straße, PLZ, Ort)
    """
    job_runner = JobRunnerService(db)

    if await job_runner.is_running(JobType.CRM_SYNC):
        raise ConflictException(message="CRM-Sync läuft bereits")

    job_source = JobSource.CRON if source == "cron" else JobSource.MANUAL
    job_run = await job_runner.start_job(JobType.CRM_SYNC, job_source)

    background_tasks.add_task(_run_crm_sync, db, job_run.id, full_sync, parse_cvs)

    return JobTriggerResponse(
        message=f"CRM-Sync gestartet{' mit CV-Parsing' if parse_cvs else ''}",
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

async def _run_geocoding(db_unused: AsyncSession, job_run_id: UUID):
    """Führt Geocoding im Hintergrund aus.
    
    WICHTIG: db_unused wird nicht verwendet! Background Tasks müssen ihre eigene
    DB-Session erstellen, da die Request-Session bereits geschlossen sein könnte.
    """
    from app.database import async_session_maker
    from app.services.geocoding_service import GeocodingService

    # Eigene DB-Session für Background Task erstellen
    async with async_session_maker() as db:
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
            await db.commit()
        except Exception as e:
            logger.error(f"Geocoding fehlgeschlagen: {e}")
            await job_runner.fail_job(job_run_id, str(e))
            await db.commit()


async def _run_crm_sync(db_unused: AsyncSession, job_run_id: UUID, full_sync: bool, parse_cvs: bool = True):
    """Führt CRM-Sync im Hintergrund aus.

    Importiert Kandidaten direkt aus der Recruit CRM API in die Datenbank.
    Verwendet den bewährten direkten Import-Ansatz (ohne CRMSyncService),
    da dieser nachweislich funktioniert.

    Args:
        db_unused: Nicht verwenden - wird aus Kompatibilitätsgründen beibehalten
        job_run_id: ID des Job-Runs für Progress-Tracking
        full_sync: True für Initial-Sync (alle Kandidaten)
        parse_cvs: Aktuell ignoriert (CV-Parsing wird separat implementiert)
    """
    from datetime import datetime, timezone

    from sqlalchemy import select

    from app.database import async_session_maker
    from app.models.candidate import Candidate
    from app.services.crm_client import RecruitCRMClient

    async with async_session_maker() as db:
        job_runner = JobRunnerService(db)

        try:
            logger.info(f"=== CRM-SYNC START === full_sync={full_sync}")

            client = RecruitCRMClient()
            created_count = 0
            updated_count = 0
            failed_count = 0
            total_processed = 0

            async for page_num, candidates, estimated_total in client.get_all_candidates_paginated(
                per_page=100
            ):
                logger.info(
                    f"CRM-Sync: Seite {page_num}, {len(candidates)} Kandidaten "
                    f"(geschätzt gesamt: {estimated_total})"
                )

                for crm_data in candidates:
                    try:
                        mapped = client.map_to_candidate_data(crm_data)
                        crm_id = mapped.get("crm_id")

                        if not crm_id:
                            failed_count += 1
                            continue

                        # Prüfe ob Kandidat bereits existiert
                        result = await db.execute(
                            select(Candidate).where(Candidate.crm_id == crm_id)
                        )
                        existing = result.scalar_one_or_none()

                        now = datetime.now(timezone.utc)

                        if existing:
                            # Update vorhandenen Kandidaten
                            for key, value in mapped.items():
                                if key != "crm_id" and value is not None:
                                    setattr(existing, key, value)
                            existing.crm_synced_at = now
                            existing.updated_at = now
                            updated_count += 1
                        else:
                            # Neuen Kandidaten erstellen
                            candidate = Candidate(
                                crm_id=crm_id,
                                first_name=mapped.get("first_name"),
                                last_name=mapped.get("last_name"),
                                email=mapped.get("email"),
                                phone=mapped.get("phone"),
                                current_position=mapped.get("current_position"),
                                current_company=mapped.get("current_company"),
                                skills=mapped.get("skills"),
                                street_address=mapped.get("street_address"),
                                postal_code=mapped.get("postal_code"),
                                city=mapped.get("city"),
                                cv_url=mapped.get("cv_url"),
                                crm_synced_at=now,
                            )
                            db.add(candidate)

                        total_processed += 1

                    except Exception as e:
                        failed_count += 1
                        logger.error(
                            f"Fehler bei Kandidat {crm_data.get('slug', crm_data.get('id'))}: {e}"
                        )

                # Commit nach jeder Seite
                await db.commit()

                # Fortschritt aktualisieren
                try:
                    await job_runner.update_progress(
                        job_run_id,
                        items_processed=total_processed,
                        items_total=estimated_total,
                    )
                    await db.commit()
                except Exception as e:
                    logger.warning(f"Progress-Update fehlgeschlagen: {e}")

            # Job abschließen
            await job_runner.complete_job(
                job_run_id,
                items_total=total_processed,
                items_successful=created_count + updated_count,
                items_failed=failed_count,
            )
            await db.commit()

            # CRM-Client schließen
            await client.close()

            logger.info(
                f"=== CRM-SYNC FERTIG === {created_count} erstellt, "
                f"{updated_count} aktualisiert, {failed_count} fehlgeschlagen, "
                f"{total_processed} gesamt"
            )

        except Exception as e:
            logger.error(f"CRM-Sync fehlgeschlagen: {e}", exc_info=True)
            try:
                await job_runner.fail_job(job_run_id, str(e))
                await db.commit()
            except Exception:
                logger.error("Konnte Job-Fehler nicht speichern")


async def _run_matching(db_unused: AsyncSession, job_run_id: UUID):
    """Führt Matching im Hintergrund aus.

    WICHTIG: db_unused wird nicht verwendet! Background Tasks müssen ihre eigene
    DB-Session erstellen, da die Request-Session bereits geschlossen sein könnte.
    """
    from app.database import async_session_maker
    from app.services.matching_service import MatchingService

    # Eigene DB-Session für Background Task erstellen
    async with async_session_maker() as db:
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
            await db.commit()
        except Exception as e:
            logger.error(f"Matching fehlgeschlagen: {e}", exc_info=True)
            await job_runner.fail_job(job_run_id, str(e))
            await db.commit()


async def _run_cleanup(db_unused: AsyncSession, job_run_id: UUID):
    """Führt Cleanup im Hintergrund aus.

    WICHTIG: db_unused wird nicht verwendet! Background Tasks müssen ihre eigene
    DB-Session erstellen, da die Request-Session bereits geschlossen sein könnte.
    """
    from datetime import datetime
    from sqlalchemy import and_, update
    from app.database import async_session_maker
    from app.models.job import Job
    from app.services.matching_service import MatchingService

    # Eigene DB-Session für Background Task erstellen
    async with async_session_maker() as db:
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
            await db.commit()
        except Exception as e:
            logger.error(f"Cleanup fehlgeschlagen: {e}", exc_info=True)
            await job_runner.fail_job(job_run_id, str(e))
            await db.commit()
