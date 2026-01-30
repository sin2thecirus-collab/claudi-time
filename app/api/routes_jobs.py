"""Jobs API Routes - Endpoints für Stellenanzeigen."""

import logging
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, UploadFile, File, status
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.exception_handlers import NotFoundException, ConflictException
from app.api.rate_limiter import RateLimitTier, rate_limit
from app.config import Limits
from app.database import get_db
from app.schemas.filters import JobFilterParams, JobSortBy, SortOrder
from app.schemas.job import JobListResponse, JobResponse, JobUpdate, ImportJobResponse
from app.schemas.pagination import PaginatedResponse, PaginationParams
from app.schemas.validators import BatchDeleteRequest
from app.models.import_job import ImportStatus
from app.services.csv_import_service import CSVImportService
from app.services.filter_service import FilterService
from app.services.job_service import JobService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs", tags=["Jobs"])


# ==================== Import ====================

@router.post(
    "/import",
    status_code=status.HTTP_202_ACCEPTED,
    summary="CSV-Import starten",
    description="Startet den Import von Stellenanzeigen aus einer CSV-Datei",
)
@rate_limit(RateLimitTier.IMPORT)
async def import_jobs(
    request: Request,
    file: UploadFile = File(..., description="CSV-Datei (Tab-getrennt)"),
    db: AsyncSession = Depends(get_db),
):
    """
    Importiert Jobs aus einer CSV-Datei.

    Die Datei muss Tab-getrennt sein und folgende Pflicht-Spalten haben:
    - Unternehmen
    - Position

    Der Import laeuft im Hintergrund. Status kann ueber GET /jobs/import/{id}/status abgefragt werden.
    Bei HTMX-Requests wird HTML (import_progress.html) zurueckgegeben.
    """
    # Pruefe Dateigroesse
    content = await file.read()
    file_size_mb = len(content) / (1024 * 1024)

    if file_size_mb > Limits.CSV_MAX_FILE_SIZE_MB:
        raise ConflictException(
            message=f"Datei zu groß. Maximum: {Limits.CSV_MAX_FILE_SIZE_MB} MB"
        )

    # Import-Job erstellen
    import_service = CSVImportService(db)
    import_job = await import_service.create_import_job(
        filename=file.filename or "upload.csv",
        content=content,
    )

    # Import direkt ausfuehren (synchron) — zuverlaessiger als Background-Task
    if import_job.status.value == "pending":
        try:
            import_job = await import_service.process_import(import_job.id, content)
        except Exception as e:
            logger.error(f"Import fehlgeschlagen: {e}", exc_info=True)
            import_job.status = ImportStatus.FAILED
            import_job.error_message = f"Import-Fehler: {str(e)[:500]}"
            import_job.completed_at = datetime.now(timezone.utc)
            await db.commit()

    # HTMX-Request: HTML zurueckgeben
    is_htmx = request.headers.get("HX-Request") == "true"
    if is_htmx:
        from fastapi.templating import Jinja2Templates
        templates = Jinja2Templates(directory="app/templates")
        return templates.TemplateResponse(
            "components/import_progress.html",
            {"request": request, "import_job": import_job},
        )

    return ImportJobResponse.model_validate(import_job)


@router.get(
    "/import/{import_id}/status",
    summary="Import-Status abfragen",
)
async def get_import_status(
    request: Request,
    import_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Gibt den aktuellen Status eines Imports zurueck."""
    import_service = CSVImportService(db)
    import_job = await import_service.get_import_job(import_id)

    if not import_job:
        raise NotFoundException(message="Import-Job nicht gefunden")

    # HTMX-Request: HTML zurueckgeben
    is_htmx = request.headers.get("HX-Request") == "true"
    if is_htmx:
        from fastapi.templating import Jinja2Templates
        templates = Jinja2Templates(directory="app/templates")
        return templates.TemplateResponse(
            "components/import_progress.html",
            {"request": request, "import_job": import_job},
        )

    return ImportJobResponse.model_validate(import_job)


@router.post(
    "/import/{import_id}/cancel",
    summary="Import abbrechen",
)
async def cancel_import(
    request: Request,
    import_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Bricht einen laufenden Import ab."""
    import_service = CSVImportService(db)
    import_job = await import_service.cancel_import(import_id)

    if not import_job:
        raise NotFoundException(message="Import-Job nicht gefunden")

    # HTMX-Request: HTML zurueckgeben
    is_htmx = request.headers.get("HX-Request") == "true"
    if is_htmx:
        from fastapi.templating import Jinja2Templates
        templates = Jinja2Templates(directory="app/templates")
        return templates.TemplateResponse(
            "components/import_progress.html",
            {"request": request, "import_job": import_job},
        )

    return ImportJobResponse.model_validate(import_job)


async def _run_import_background(import_job_id: UUID, content: bytes):
    """Background-Task fuer CSV-Import mit eigener DB-Session."""
    from app.database import async_session_maker
    from app.models.import_job import ImportStatus

    logger.info(f"Background-Import gestartet: {import_job_id}, Content-Size: {len(content)} bytes")

    async with async_session_maker() as session:
        try:
            service = CSVImportService(session)
            result = await service.process_import(import_job_id, content)
            logger.info(
                f"Background-Import abgeschlossen: {import_job_id}, "
                f"Status: {result.status}, "
                f"Erfolgreich: {result.successful_rows}/{result.total_rows}"
            )
        except Exception as e:
            logger.error(f"Background-Import fehlgeschlagen: {e}", exc_info=True)
            try:
                await session.rollback()
                # Import-Job als FAILED markieren
                import_job = await session.get(ImportJob, import_job_id)
                if import_job and not import_job.is_complete:
                    import_job.status = ImportStatus.FAILED
                    import_job.error_message = f"Background-Task Fehler: {str(e)[:500]}"
                    import_job.completed_at = datetime.now(timezone.utc)
                    await session.commit()
            except Exception as inner_e:
                logger.error(f"Konnte Import-Job {import_job_id} nicht als FAILED markieren: {inner_e}")


# ==================== CRUD ====================

@router.get(
    "",
    response_model=JobListResponse,
    summary="Jobs auflisten",
    description="Gibt eine paginierte Liste von Jobs mit Filteroptionen zurück",
)
async def list_jobs(
    # Pagination
    page: int = Query(default=1, ge=1, description="Seitennummer"),
    per_page: int = Query(
        default=Limits.PAGE_SIZE_DEFAULT,
        ge=1,
        le=Limits.PAGE_SIZE_MAX,
        description="Einträge pro Seite",
    ),
    # Filter
    search: str | None = Query(default=None, min_length=2, max_length=100),
    cities: list[str] | None = Query(default=None, description="Filter nach Städten"),
    industries: list[str] | None = Query(default=None, description="Filter nach Branchen"),
    company: str | None = Query(default=None, min_length=2, max_length=100),
    position: str | None = Query(default=None, min_length=2, max_length=100),
    has_active_candidates: bool | None = Query(default=None),
    include_deleted: bool = Query(default=False),
    include_expired: bool = Query(default=False),
    # Sortierung
    sort_by: JobSortBy = Query(default=JobSortBy.CREATED_AT),
    sort_order: SortOrder = Query(default=SortOrder.DESC),
    # Prio-Städte
    use_priority_sorting: bool = Query(
        default=True,
        description="Prio-Städte (Hamburg, München) zuerst",
    ),
    db: AsyncSession = Depends(get_db),
):
    """
    Listet Jobs mit umfangreichen Filteroptionen.

    Jobs werden standardmäßig nach Prio-Städten sortiert (Hamburg, München zuerst),
    dann nach dem gewählten Sortierfeld.
    """
    # Filter-Parameter erstellen
    filters = JobFilterParams(
        search=search,
        cities=cities,
        industries=industries,
        company=company,
        position=position,
        has_active_candidates=has_active_candidates,
        include_deleted=include_deleted,
        include_expired=include_expired,
        sort_by=sort_by,
        sort_order=sort_order,
    )

    job_service = JobService(db)

    # Jobs laden mit Filtern
    result = await job_service.list_jobs(
        filters=filters,
        page=page,
        per_page=per_page,
    )

    # Response erstellen
    return JobListResponse(
        items=[_job_to_response(job) for job in result.items],
        total=result.total,
        page=result.page,
        per_page=result.per_page,
        pages=result.pages,
    )


@router.get(
    "/{job_id}",
    response_model=JobResponse,
    summary="Job-Details abrufen",
)
async def get_job(
    job_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Gibt die Details eines einzelnen Jobs zurück."""
    job_service = JobService(db)
    job = await job_service.get_job(job_id)

    if not job:
        raise NotFoundException(message="Job nicht gefunden")

    return _job_to_response(job)


@router.patch(
    "/{job_id}",
    response_model=JobResponse,
    summary="Job aktualisieren",
)
@rate_limit(RateLimitTier.WRITE)
async def update_job(
    job_id: UUID,
    data: JobUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Aktualisiert einen Job."""
    job_service = JobService(db)
    job = await job_service.update_job(job_id, data)

    if not job:
        raise NotFoundException(message="Job nicht gefunden")

    return _job_to_response(job)


@router.delete(
    "/{job_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Job löschen (Soft-Delete)",
)
@rate_limit(RateLimitTier.WRITE)
async def delete_job(
    job_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """
    Löscht einen Job (Soft-Delete).

    Der Job wird als gelöscht markiert, kann aber wiederhergestellt werden.
    """
    job_service = JobService(db)
    success = await job_service.soft_delete_job(job_id)

    if not success:
        raise NotFoundException(message="Job nicht gefunden")


@router.delete(
    "/batch",
    status_code=status.HTTP_200_OK,
    summary="Mehrere Jobs löschen",
)
@rate_limit(RateLimitTier.WRITE)
async def batch_delete_jobs(
    request: BatchDeleteRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Löscht mehrere Jobs auf einmal (Soft-Delete).

    Maximal 100 Jobs pro Anfrage.
    """
    job_service = JobService(db)
    deleted_count = await job_service.batch_delete(request.ids)

    return {"deleted_count": deleted_count}


@router.post(
    "/{job_id}/restore",
    response_model=JobResponse,
    summary="Job wiederherstellen",
)
@rate_limit(RateLimitTier.WRITE)
async def restore_job(
    job_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Stellt einen gelöschten Job wieder her."""
    job_service = JobService(db)
    job = await job_service.restore_job(job_id)

    if not job:
        raise NotFoundException(message="Job nicht gefunden")

    return _job_to_response(job)


@router.put(
    "/{job_id}/exclude-deletion",
    response_model=JobResponse,
    summary="Von Auto-Löschung ausnehmen",
)
@rate_limit(RateLimitTier.WRITE)
async def exclude_from_deletion(
    job_id: UUID,
    exclude: bool = Query(default=True, description="True = von Löschung ausnehmen"),
    db: AsyncSession = Depends(get_db),
):
    """
    Nimmt einen Job von der automatischen Löschung aus (oder macht dies rückgängig).

    Jobs mit dieser Markierung werden beim Cleanup-Job nicht gelöscht.
    """
    job_service = JobService(db)
    job = await job_service.exclude_from_deletion(job_id, exclude)

    if not job:
        raise NotFoundException(message="Job nicht gefunden")

    return _job_to_response(job)


@router.get(
    "/{job_id}/candidates",
    summary="Kandidaten für einen Job",
    description="Gibt Kandidaten mit Match-Daten für einen Job zurück",
)
async def get_candidates_for_job(
    job_id: UUID,
    # Pagination
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=Limits.PAGE_SIZE_DEFAULT, ge=1, le=Limits.PAGE_SIZE_MAX),
    # Filter
    name: str | None = Query(default=None, min_length=2, max_length=100),
    cities: list[str] | None = Query(default=None),
    skills: list[str] | None = Query(default=None),
    min_distance_km: float | None = Query(default=None, ge=0),
    max_distance_km: float | None = Query(default=None, le=25),
    only_active: bool = Query(default=True),
    include_hidden: bool = Query(default=False),
    only_ai_checked: bool = Query(default=False),
    min_ai_score: float | None = Query(default=None, ge=0, le=1),
    match_status: str | None = Query(default=None),
    # Sortierung
    sort_by: str = Query(default="distance_km"),
    sort_order: SortOrder = Query(default=SortOrder.ASC),
    db: AsyncSession = Depends(get_db),
):
    """
    Gibt Kandidaten für einen Job mit Match-Daten zurück.

    Enthält Distanz, Keyword-Score und ggf. KI-Bewertung.
    """
    from app.services.candidate_service import CandidateService
    from app.schemas.filters import CandidateFilterParams, CandidateSortBy

    # Job prüfen
    job_service = JobService(db)
    job = await job_service.get_job(job_id)
    if not job:
        raise NotFoundException(message="Job nicht gefunden")

    # Filter erstellen
    filters = CandidateFilterParams(
        name=name,
        cities=cities,
        skills=skills,
        min_distance_km=min_distance_km,
        max_distance_km=max_distance_km,
        only_active=only_active,
        include_hidden=include_hidden,
        only_ai_checked=only_ai_checked,
        min_ai_score=min_ai_score,
        status=match_status,
    )

    candidate_service = CandidateService(db)
    candidates, total = await candidate_service.get_candidates_for_job(
        job_id=job_id,
        filters=filters,
        page=page,
        per_page=per_page,
        sort_by=sort_by,
        sort_order=sort_order.value,
    )

    pages = (total + per_page - 1) // per_page if per_page > 0 else 0

    return {
        "items": candidates,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": pages,
    }


# ==================== Hilfsfunktionen ====================

def _job_to_response(job) -> JobResponse:
    """Konvertiert ein Job-Model zu einem Response-Schema."""
    return JobResponse(
        id=job.id,
        company_name=job.company_name,
        position=job.position,
        street_address=job.street_address,
        postal_code=job.postal_code,
        city=job.city,
        work_location_city=job.work_location_city,
        display_city=job.display_city,
        job_url=job.job_url,
        job_text=job.job_text,
        employment_type=job.employment_type,
        industry=job.industry,
        company_size=job.company_size,
        has_coordinates=job.location_coords is not None,
        expires_at=job.expires_at,
        excluded_from_deletion=job.excluded_from_deletion,
        is_deleted=job.is_deleted,
        is_expired=job.is_expired,
        created_at=job.created_at,
        updated_at=job.updated_at,
        match_count=getattr(job, "match_count", None),
        active_candidate_count=getattr(job, "active_candidate_count", None),
    )
