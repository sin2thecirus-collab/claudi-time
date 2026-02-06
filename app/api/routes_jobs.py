"""Jobs API Routes - Endpoints für Stellenanzeigen."""

import logging
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, UploadFile, File, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
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
from app.models.job import Job
from app.services.csv_import_service import CSVImportService
from app.services.filter_service import FilterService
from app.services.job_service import JobService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs", tags=["Jobs"])
templates = Jinja2Templates(directory="app/templates")


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
):
    """
    Importiert Jobs aus einer CSV-Datei.

    Die Datei muss Tab-getrennt sein und die Pflicht-Spalte 'Unternehmen' haben.
    Bei HTMX-Requests wird HTML (import_progress.html) zurueckgegeben.
    """
    from app.database import async_session_maker

    # Pruefe Dateigroesse
    content = await file.read()
    file_size_mb = len(content) / (1024 * 1024)

    if file_size_mb > Limits.CSV_MAX_FILE_SIZE_MB:
        raise ConflictException(
            message=f"Datei zu groß. Maximum: {Limits.CSV_MAX_FILE_SIZE_MB} MB"
        )

    logger.info(f"CSV-Upload: {file.filename}, {file_size_mb:.1f} MB")

    # Eigene DB-Session fuer den Import (nicht die Request-Session)
    async with async_session_maker() as db:
        try:
            import_service = CSVImportService(db)

            # Import-Job erstellen (schnelle Header-Pruefung)
            import_job = await import_service.create_import_job(
                filename=file.filename or "upload.csv",
                content=content,
            )

            # Import direkt ausfuehren
            if import_job.status.value == "pending":
                import_job = await import_service.process_import(import_job.id, content)

        except Exception as e:
            logger.error(f"Import fehlgeschlagen: {e}", exc_info=True)
            try:
                await db.rollback()
            except Exception:
                pass
            # Versuche Import-Job als FAILED zu markieren
            try:
                if 'import_job' in locals() and import_job:
                    import_job.status = ImportStatus.FAILED
                    import_job.error_message = f"Import-Fehler: {str(e)[:500]}"
                    import_job.completed_at = datetime.now(timezone.utc)
                    await db.commit()
            except Exception:
                pass
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=500,
                content={"status": "failed", "error": "Import fehlgeschlagen"},
            )

    logger.info(
        f"Import abgeschlossen: {import_job.id}, "
        f"Status: {import_job.status}, "
        f"Erfolgreich: {import_job.successful_rows}/{import_job.total_rows}"
    )

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


# ==================== CRUD ====================


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Job manuell erstellen",
    description="Erstellt einen neuen Job manuell (nicht via CSV-Import)",
)
@rate_limit(RateLimitTier.WRITE)
async def create_job(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Erstellt einen neuen Job manuell.

    Das Unternehmen kann ueber company_id mit einem bestehenden Unternehmen
    verknuepft werden. Bei Auswahl eines Unternehmens werden Adressdaten
    automatisch uebernommen.
    """
    from app.schemas.job import JobCreate

    body = await request.json()
    data = JobCreate(**body)

    job_service = JobService(db)
    job = await job_service.create_job(data)
    await db.commit()

    return {
        "id": str(job.id),
        "message": "Job erstellt",
        "position": job.position,
        "company_name": job.company_name,
    }


@router.post(
    "/link-companies",
    summary="Jobs mit Unternehmen verknuepfen",
    description="Verknuepft alle Jobs ohne company_id mit passenden Unternehmen (basierend auf company_name)",
)
async def link_jobs_to_companies(
    db: AsyncSession = Depends(get_db),
):
    """
    Verknuepft bestehende Jobs mit Unternehmen.

    Sucht fuer jeden Job ohne company_id ein passendes Unternehmen
    anhand des company_name und setzt die Verknuepfung.
    Versucht erst exakte Uebereinstimmung, dann "enthaelt" Suche.
    """
    from sqlalchemy import select, func
    from app.models import Job
    from app.models.company import Company

    # Finde alle Jobs ohne company_id
    jobs_query = select(Job).where(
        Job.company_id.is_(None),
        Job.deleted_at.is_(None)
    )
    result = await db.execute(jobs_query)
    jobs = result.scalars().all()

    linked_count = 0
    details = []

    for job in jobs:
        # 1. Versuch: Exakte Uebereinstimmung (case-insensitive)
        company_query = select(Company).where(
            func.lower(Company.name) == func.lower(job.company_name)
        )
        company_result = await db.execute(company_query)
        company = company_result.scalar_one_or_none()

        # 2. Versuch: Company-Name enthaelt Job.company_name oder umgekehrt
        if not company and job.company_name:
            # Suche Company deren Name im Job-Company-Namen vorkommt
            search_term = job.company_name.strip().lower()
            company_query = select(Company).where(
                func.lower(Company.name).contains(search_term)
            )
            company_result = await db.execute(company_query)
            matches = company_result.scalars().all()

            if len(matches) == 1:
                company = matches[0]
            elif not matches:
                # Umgekehrt: Job-Company-Name enthaelt Company-Namen
                all_companies = await db.execute(select(Company))
                for c in all_companies.scalars().all():
                    if c.name.strip().lower() in search_term:
                        company = c
                        break

        if company:
            job.company_id = company.id
            linked_count += 1
            details.append({
                "job_id": str(job.id),
                "job_company_name": job.company_name,
                "linked_to": company.name,
            })

    await db.commit()

    return {
        "message": f"{linked_count} Jobs mit Unternehmen verknuepft",
        "total_unlinked": len(jobs),
        "linked": linked_count,
        "details": details,
    }


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


# ==================== Batch-Operationen ====================
# WICHTIG: Batch-Routes MUESSEN vor /{job_id} Routes stehen,
# da FastAPI sonst "batch" als job_id interpretiert!


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
    try:
        logger.info(f"Batch delete request: {request.ids}")
        job_service = JobService(db)
        deleted_count = await job_service.batch_delete(request.ids)
        logger.info(f"Batch delete success: {deleted_count} jobs deleted")
        return {"deleted_count": deleted_count}
    except Exception as e:
        logger.error(f"Batch delete error: {e}", exc_info=True)
        raise


@router.post(
    "/batch/to-pipeline",
    summary="Mehrere Jobs zur Interview-Pipeline hinzufuegen",
)
@rate_limit(RateLimitTier.WRITE)
async def batch_add_jobs_to_pipeline(
    request: BatchDeleteRequest,  # Wiederverwendung des Schemas fuer IDs
    db: AsyncSession = Depends(get_db),
):
    """
    Erstellt ATSJobs aus mehreren importierten Jobs und fuegt sie zur Pipeline hinzu.

    Maximal 100 Jobs pro Anfrage.
    """
    from sqlalchemy import select
    from app.services.ats_job_service import ATSJobService

    if len(request.ids) > Limits.BATCH_DELETE_MAX:
        raise ConflictException(
            message=f"Maximal {Limits.BATCH_DELETE_MAX} Jobs pro Batch erlaubt"
        )

    # Jobs laden
    result = await db.execute(
        select(Job).where(
            Job.id.in_(request.ids),
            Job.deleted_at.is_(None),
        )
    )
    jobs = result.scalars().all()

    ats_service = ATSJobService(db)
    added_count = 0

    for job in jobs:
        ats_job = await ats_service.create_job(
            title=job.position,
            company_id=job.company_id,
            location_city=job.work_location_city or job.city,
            employment_type=job.employment_type,
            description=job.job_text,
            source=f"Import: {job.job_url}" if job.job_url else "CSV-Import",
            # source_job_id wird erst nach Migration 011 verfuegbar sein
        )
        ats_job.in_pipeline = True
        added_count += 1

    await db.commit()

    return {
        "message": f"{added_count} Job(s) zur Pipeline hinzugefuegt",
        "added_count": added_count,
    }


# ==================== Einzelne Job-Operationen ====================


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


@router.get(
    "/{job_id}/delete-dialog",
    response_class=HTMLResponse,
    summary="Delete-Dialog fuer Job",
)
async def get_job_delete_dialog(
    request: Request,
    job_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Gibt den Delete-Dialog fuer einen Job zurueck."""
    job_service = JobService(db)
    job = await job_service.get_job(job_id)

    return templates.TemplateResponse(
        "components/delete_dialog.html",
        {
            "request": request,
            "title": "Job loeschen",
            "message": f"Moechten Sie den Job wirklich loeschen?",
            "item_name": f"{job.position} bei {job.company_name}",
            "delete_url": f"/api/jobs/{job_id}",
            "delete_method": "DELETE",
        },
    )


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


@router.post(
    "/{job_id}/to-pipeline",
    summary="Job zur Interview-Pipeline hinzufuegen",
)
@rate_limit(RateLimitTier.WRITE)
async def add_job_to_pipeline(
    job_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """
    Erstellt einen ATSJob aus einem importierten Job und fuegt ihn zur Pipeline hinzu.

    Der importierte Job bleibt bestehen, es wird eine Kopie als ATSJob erstellt.
    """
    from app.services.ats_job_service import ATSJobService

    job_service = JobService(db)
    job = await job_service.get_job(job_id)

    if not job:
        raise NotFoundException(message="Job nicht gefunden")

    # ATSJob erstellen mit Daten aus dem importierten Job
    ats_service = ATSJobService(db)
    ats_job = await ats_service.create_job(
        title=job.position,
        company_id=job.company_id,
        location_city=job.work_location_city or job.city,
        employment_type=job.employment_type,
        description=job.job_text,
        source=f"Import: {job.job_url}" if job.job_url else "CSV-Import",
        # source_job_id wird erst nach Migration 011 verfuegbar sein
    )
    # In Pipeline setzen
    ats_job.in_pipeline = True
    await db.commit()

    return {
        "message": f"'{job.position}' zur Pipeline hinzugefuegt",
        "ats_job_id": str(ats_job.id),
        "job_id": str(job_id),
    }


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
    response_class=HTMLResponse,
    summary="Kandidaten für einen Job (HTMX Partial)",
    description="Gibt Kandidaten mit Match-Daten als HTML-Partial für HTMX zurück",
)
async def get_candidates_for_job(
    request: Request,
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
    Gibt Kandidaten für einen Job als HTML-Partial zurück.

    Wird von HTMX auf der Job-Detail-Seite geladen.
    Enthält Distanz, Keyword-Score und ggf. KI-Bewertung.
    """
    from fastapi.templating import Jinja2Templates
    from app.services.candidate_service import CandidateService
    from app.schemas.filters import CandidateFilterParams, CandidateSortBy

    templates = Jinja2Templates(directory="app/templates")

    # Job prüfen
    job_service = JobService(db)
    job = await job_service.get_job(job_id)
    if not job:
        raise NotFoundException(message="Job nicht gefunden")

    # Filter erstellen (sort_by aus Query in CandidateSortBy umwandeln)
    try:
        candidate_sort_by = CandidateSortBy(sort_by)
    except ValueError:
        candidate_sort_by = CandidateSortBy.DISTANCE_KM

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
        sort_by=candidate_sort_by,
        sort_order=sort_order,
    )

    pagination = PaginationParams(page=page, per_page=per_page)

    candidate_service = CandidateService(db)
    result = await candidate_service.get_candidates_for_job(
        job_id=job_id,
        filters=filters,
        pagination=pagination,
    )

    return templates.TemplateResponse(
        "partials/candidate_list.html",
        {
            "request": request,
            "candidates": result.items,
            "total": result.total,
            "page": result.page,
            "pages": result.pages,
            "job_id": str(job_id),
        },
    )


# ==================== Hilfsfunktionen ====================

def _job_to_response(job) -> JobResponse:
    """Konvertiert ein Job-Model oder Dict zu einem Response-Schema."""
    # Unterstuetzt sowohl Job-Objekte als auch Dicts (von _add_match_counts)
    if isinstance(job, dict):
        return JobResponse(
            id=job["id"],
            company_name=job["company_name"],
            company_id=job.get("company_id"),
            company=job.get("company"),
            position=job["position"],
            street_address=job.get("street_address"),
            postal_code=job.get("postal_code"),
            city=job.get("city"),
            work_location_city=job.get("work_location_city"),
            display_city=job.get("display_city"),
            job_url=job.get("job_url"),
            job_text=job.get("job_text"),
            employment_type=job.get("employment_type"),
            industry=job.get("industry"),
            company_size=job.get("company_size"),
            has_coordinates=job.get("has_coordinates", False),
            expires_at=job.get("expires_at"),
            excluded_from_deletion=job.get("excluded_from_deletion", False),
            is_deleted=job.get("is_deleted", False),
            is_expired=job.get("is_expired", False),
            imported_at=job.get("imported_at"),
            last_updated_at=job.get("last_updated_at"),
            created_at=job.get("created_at"),
            updated_at=job.get("updated_at"),
            match_count=job.get("match_count"),
            active_candidate_count=job.get("active_candidate_count"),
        )

    # Job-Objekt Handling
    return JobResponse(
        id=job.id,
        company_name=job.company_name,
        company_id=getattr(job, "company_id", None),
        company=None,
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
        imported_at=getattr(job, "imported_at", None),
        last_updated_at=getattr(job, "last_updated_at", None),
        created_at=job.created_at,
        updated_at=job.updated_at,
        match_count=getattr(job, "match_count", None),
        active_candidate_count=getattr(job, "active_candidate_count", None),
    )


# ==================== Maintenance ====================


@router.post(
    "/maintenance/clean-job-texts",
    summary="Bestehende Job-Texte strukturiert aufbereiten",
    tags=["Maintenance"],
)
async def clean_existing_job_texts(
    db: AsyncSession = Depends(get_db),
):
    """
    Bereinigt alle bestehenden job_text Felder in der Datenbank.

    Erkennt Abschnitts-Ueberschriften und fuegt Zeilenumbrueche ein,
    damit der Text im Split-View lesbar angezeigt wird.
    """
    from sqlalchemy import select, and_
    from app.models.job import Job as JobModel

    # Alle Jobs mit job_text laden die KEINE Zeilenumbrueche haben
    # (= noch nicht bereinigt)
    query = select(JobModel).where(
        and_(
            JobModel.job_text.is_not(None),
            JobModel.deleted_at.is_(None),
        )
    )
    result = await db.execute(query)
    jobs = result.scalars().all()

    cleaned_count = 0
    skipped_count = 0

    for job in jobs:
        if not job.job_text:
            continue

        # Nur bereinigen wenn weniger als 4 Zeilenumbrueche
        # (sonst ist der Text vermutlich schon strukturiert)
        if job.job_text.count("\n") > 3:
            skipped_count += 1
            continue

        new_text = CSVImportService._clean_job_text(job.job_text, job.position or "")
        if new_text and new_text != job.job_text:
            job.job_text = new_text
            cleaned_count += 1

    await db.commit()

    return {
        "status": "completed",
        "total_jobs": len(jobs),
        "cleaned": cleaned_count,
        "skipped_already_structured": skipped_count,
    }
