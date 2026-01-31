"""Candidates API Routes - Endpoints für Kandidaten."""

import logging
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.exception_handlers import NotFoundException, ConflictException
from app.api.rate_limiter import RateLimitTier, rate_limit
from app.config import Limits
from app.database import get_db
from app.schemas.candidate import CandidateListResponse, CandidateResponse, CandidateUpdate, LanguageEntry
from app.schemas.filters import CandidateFilterParams, SortOrder
from app.schemas.pagination import PaginationParams
from app.schemas.validators import BatchDeleteRequest, BatchHideRequest
from app.services.candidate_service import CandidateService
from app.services.crm_sync_service import CRMSyncService
from app.services.job_runner_service import JobRunnerService
from app.models.job_run import JobType, JobSource

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/candidates", tags=["Kandidaten"])


# ==================== Sync ====================

@router.post(
    "/sync",
    status_code=status.HTTP_202_ACCEPTED,
    summary="CRM-Sync starten",
    description="Startet die Synchronisation mit dem CRM-System",
)
@rate_limit(RateLimitTier.ADMIN)
async def start_crm_sync(
    background_tasks: BackgroundTasks,
    full_sync: bool = Query(
        default=False,
        description="True = kompletter Sync, False = nur neue/geänderte",
    ),
    db: AsyncSession = Depends(get_db),
):
    """
    Startet einen CRM-Sync.

    - full_sync=False: Nur Kandidaten, die seit dem letzten Sync geändert wurden
    - full_sync=True: Alle Kandidaten (kann lange dauern)
    """
    job_runner = JobRunnerService(db)

    # Prüfe, ob bereits ein Sync läuft
    if await job_runner.is_running(JobType.CRM_SYNC):
        raise ConflictException(message="Ein CRM-Sync läuft bereits")

    # Job starten
    job_run = await job_runner.start_job(JobType.CRM_SYNC, JobSource.MANUAL)

    # Sync im Hintergrund starten
    background_tasks.add_task(
        _run_crm_sync,
        db,
        job_run.id,
        full_sync,
    )

    return {
        "message": "CRM-Sync gestartet",
        "job_run_id": str(job_run.id),
        "full_sync": full_sync,
    }


async def _run_crm_sync(db: AsyncSession, job_run_id: UUID, full_sync: bool):
    """Führt den CRM-Sync im Hintergrund aus."""
    job_runner = JobRunnerService(db)
    sync_service = CRMSyncService(db)

    async def update_progress(processed: int, total: int):
        """Callback für Fortschrittsupdates."""
        await job_runner.update_progress(
            job_run_id,
            items_processed=processed,
            items_total=total,
        )

    try:
        if full_sync:
            result = await sync_service.initial_sync(progress_callback=update_progress)
        else:
            result = await sync_service.sync_all(progress_callback=update_progress)

        await job_runner.complete_job(
            job_run_id,
            items_total=result.total_processed,
            items_successful=result.created + result.updated,
            items_failed=result.failed,
        )
    except Exception as e:
        logger.error(f"CRM-Sync fehlgeschlagen: {e}")
        await job_runner.fail_job(job_run_id, str(e))


# ==================== CRUD ====================

@router.get(
    "",
    response_model=CandidateListResponse,
    summary="Kandidaten auflisten",
)
async def list_candidates(
    # Pagination
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=Limits.PAGE_SIZE_DEFAULT, ge=1, le=Limits.PAGE_SIZE_MAX),
    # Filter
    name: str | None = Query(default=None, min_length=2, max_length=100),
    cities: list[str] | None = Query(default=None),
    skills: list[str] | None = Query(default=None),
    position: str | None = Query(default=None, min_length=2, max_length=100),
    only_active: bool = Query(default=False, description="Nur aktive (≤30 Tage)"),
    include_hidden: bool = Query(default=False),
    # Sortierung
    sort_by: str = Query(default="created_at"),
    sort_order: SortOrder = Query(default=SortOrder.DESC),
    db: AsyncSession = Depends(get_db),
):
    """
    Listet Kandidaten mit Filteroptionen.

    Standardmäßig werden alle nicht-versteckten Kandidaten angezeigt.
    """
    candidate_service = CandidateService(db)

    # Filter- und Pagination-Objekte erstellen
    filters = CandidateFilterParams(
        name=name,
        cities=cities,
        skills=skills,
        position=position,
        only_active=only_active,
        include_hidden=include_hidden,
        sort_by=sort_by,
        sort_order=sort_order.value,
    )
    pagination = PaginationParams(page=page, per_page=per_page)

    result = await candidate_service.list_candidates(
        filters=filters,
        pagination=pagination,
    )

    return CandidateListResponse(
        items=result.items,
        total=result.total,
        page=result.page,
        per_page=result.per_page,
        pages=result.pages,
    )


@router.get(
    "/{candidate_id}",
    response_model=CandidateResponse,
    summary="Kandidaten-Details abrufen",
)
async def get_candidate(
    candidate_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Gibt die Details eines Kandidaten zurück."""
    candidate_service = CandidateService(db)
    candidate = await candidate_service.get_candidate(candidate_id)

    if not candidate:
        raise NotFoundException(message="Kandidat nicht gefunden")

    return _candidate_to_response(candidate)


@router.patch(
    "/{candidate_id}",
    response_model=CandidateResponse,
    summary="Kandidaten aktualisieren",
)
@rate_limit(RateLimitTier.WRITE)
async def update_candidate(
    candidate_id: UUID,
    data: CandidateUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Aktualisiert einen Kandidaten."""
    candidate_service = CandidateService(db)
    candidate = await candidate_service.update_candidate(candidate_id, data)

    if not candidate:
        raise NotFoundException(message="Kandidat nicht gefunden")

    return _candidate_to_response(candidate)


# ==================== Hide/Unhide ====================

@router.put(
    "/{candidate_id}/hide",
    response_model=CandidateResponse,
    summary="Kandidaten ausblenden",
)
@rate_limit(RateLimitTier.WRITE)
async def hide_candidate(
    candidate_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """
    Blendet einen Kandidaten aus.

    Ausgeblendete Kandidaten erscheinen nicht mehr in Suchergebnissen
    und Matching-Listen.
    """
    candidate_service = CandidateService(db)
    candidate = await candidate_service.hide_candidate(candidate_id)

    if not candidate:
        raise NotFoundException(message="Kandidat nicht gefunden")

    return _candidate_to_response(candidate)


@router.put(
    "/{candidate_id}/unhide",
    response_model=CandidateResponse,
    summary="Kandidaten wieder einblenden",
)
@rate_limit(RateLimitTier.WRITE)
async def unhide_candidate(
    candidate_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Macht das Ausblenden eines Kandidaten rückgängig."""
    candidate_service = CandidateService(db)
    candidate = await candidate_service.unhide_candidate(candidate_id)

    if not candidate:
        raise NotFoundException(message="Kandidat nicht gefunden")

    return _candidate_to_response(candidate)


@router.put(
    "/batch/hide",
    summary="Mehrere Kandidaten ausblenden",
)
@rate_limit(RateLimitTier.WRITE)
async def batch_hide_candidates(
    request: BatchHideRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Blendet mehrere Kandidaten auf einmal aus.

    Maximal 100 Kandidaten pro Anfrage.
    """
    candidate_service = CandidateService(db)
    hidden_count = await candidate_service.batch_hide(request.ids)

    return {"hidden_count": hidden_count}


@router.put(
    "/batch/unhide",
    summary="Mehrere Kandidaten wieder einblenden",
)
@rate_limit(RateLimitTier.WRITE)
async def batch_unhide_candidates(
    request: BatchHideRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Blendet mehrere Kandidaten wieder ein.

    Maximal 100 Kandidaten pro Anfrage.
    """
    candidate_service = CandidateService(db)
    unhidden_count = await candidate_service.batch_unhide(request.ids)

    return {"unhidden_count": unhidden_count}


# ==================== Delete (Soft-Delete) ====================

@router.delete(
    "/batch/delete",
    summary="Mehrere Kandidaten löschen (Soft-Delete)",
)
@rate_limit(RateLimitTier.WRITE)
async def batch_delete_candidates(
    request: BatchDeleteRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Löscht mehrere Kandidaten auf einmal (Soft-Delete).

    Maximal 100 Kandidaten pro Anfrage.
    Gelöschte Kandidaten werden beim CRM-Sync ignoriert.
    """
    candidate_service = CandidateService(db)
    deleted_count = await candidate_service.batch_delete(request.ids)
    await db.commit()

    return {"deleted_count": deleted_count}


@router.post(
    "/{candidate_id}/reparse-cv",
    summary="CV eines Kandidaten neu parsen (OpenAI)",
)
async def reparse_single_cv(
    candidate_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """
    Parst das CV eines einzelnen Kandidaten erneut mit OpenAI.

    Gibt den alten und neuen Namen zurück, damit das Frontend
    den Fortschritt live anzeigen kann.
    """
    from app.services.cv_parser_service import CVParserService
    from sqlalchemy import select
    from app.models.candidate import Candidate

    # Kandidat laden
    result = await db.execute(
        select(Candidate).where(Candidate.id == candidate_id)
    )
    candidate = result.scalar_one_or_none()

    if not candidate:
        return {"status": "error", "message": "Kandidat nicht gefunden"}

    old_first = candidate.first_name or ""
    old_last = candidate.last_name or ""
    old_name = f"{old_first} {old_last}".strip() or "—"

    if not candidate.cv_url:
        return {
            "status": "skipped",
            "message": "Kein CV vorhanden",
            "candidate_id": str(candidate_id),
            "old_name": old_name,
            "new_name": old_name,
        }

    # CV parsen
    async with CVParserService(db) as parser:
        try:
            candidate, parse_result = await parser.parse_candidate_cv(candidate_id)
        except Exception as e:
            return {
                "status": "error",
                "message": str(e),
                "candidate_id": str(candidate_id),
                "old_name": old_name,
                "new_name": old_name,
            }

    if not parse_result.success:
        return {
            "status": "error",
            "message": parse_result.error or "Parsing fehlgeschlagen",
            "candidate_id": str(candidate_id),
            "old_name": old_name,
            "new_name": old_name,
        }

    new_first = candidate.first_name or ""
    new_last = candidate.last_name or ""
    new_name = f"{new_first} {new_last}".strip() or "—"
    name_changed = old_name != new_name

    return {
        "status": "ok",
        "candidate_id": str(candidate_id),
        "old_name": old_name,
        "new_name": new_name,
        "name_changed": name_changed,
        "position": candidate.current_position or "—",
    }


@router.delete(
    "/{candidate_id}",
    summary="Kandidaten löschen (Soft-Delete)",
)
@rate_limit(RateLimitTier.WRITE)
async def delete_candidate(
    candidate_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """
    Löscht einen Kandidaten (Soft-Delete).

    Der Kandidat wird als gelöscht markiert und beim nächsten
    CRM-Sync komplett ignoriert (kein Update, kein Neu-Erstellen).
    """
    candidate_service = CandidateService(db)
    success = await candidate_service.delete_candidate(candidate_id)

    if not success:
        raise NotFoundException(message="Kandidat nicht gefunden")

    await db.commit()

    return {"success": True, "message": "Kandidat gelöscht"}


# ==================== CV-Parsing ====================

@router.post(
    "/{candidate_id}/parse-cv",
    response_model=CandidateResponse,
    summary="CV neu parsen",
)
@rate_limit(RateLimitTier.AI)
async def parse_candidate_cv(
    candidate_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """
    Parst den CV eines Kandidaten erneut.

    Verwendet OpenAI, um strukturierte Daten aus dem CV zu extrahieren.
    """
    from app.services.cv_parser_service import CVParserService

    candidate_service = CandidateService(db)
    candidate = await candidate_service.get_candidate(candidate_id)

    if not candidate:
        raise NotFoundException(message="Kandidat nicht gefunden")

    if not candidate.cv_url:
        raise ConflictException(message="Kandidat hat keine CV-URL")

    cv_parser = CVParserService(db)
    updated_candidate = await cv_parser.parse_candidate_cv(candidate_id)

    if not updated_candidate:
        raise ConflictException(message="CV-Parsing fehlgeschlagen")

    return _candidate_to_response(updated_candidate)


# ==================== Matches für Kandidat ====================

@router.get(
    "/{candidate_id}/jobs",
    summary="Passende Jobs für einen Kandidaten",
)
async def get_jobs_for_candidate(
    candidate_id: UUID,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=Limits.PAGE_SIZE_DEFAULT, ge=1, le=Limits.PAGE_SIZE_MAX),
    sort_by: str = Query(default="distance_km"),
    sort_order: SortOrder = Query(default=SortOrder.ASC),
    db: AsyncSession = Depends(get_db),
):
    """
    Gibt passende Jobs für einen Kandidaten zurück.

    Zeigt alle Jobs im Umkreis von 25km mit Match-Daten.
    """
    candidate_service = CandidateService(db)
    candidate = await candidate_service.get_candidate(candidate_id)

    if not candidate:
        raise NotFoundException(message="Kandidat nicht gefunden")

    jobs, total = await candidate_service.get_jobs_for_candidate(
        candidate_id=candidate_id,
        page=page,
        per_page=per_page,
        sort_by=sort_by,
        sort_order=sort_order.value,
    )

    pages = (total + per_page - 1) // per_page if per_page > 0 else 0

    return {
        "items": jobs,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": pages,
    }


# ==================== Hilfsfunktionen ====================

def _candidate_to_response(candidate) -> CandidateResponse:
    """Konvertiert ein Candidate-Model zu einem Response-Schema."""
    return CandidateResponse(
        id=candidate.id,
        crm_id=candidate.crm_id,
        first_name=candidate.first_name,
        last_name=candidate.last_name,
        full_name=candidate.full_name,
        email=candidate.email,
        phone=candidate.phone,
        birth_date=candidate.birth_date,
        age=candidate.age,
        current_position=candidate.current_position,
        current_company=candidate.current_company,
        skills=candidate.skills,
        languages=[
            LanguageEntry(**lang) if isinstance(lang, dict) else lang
            for lang in (candidate.languages or [])
        ] or None,
        it_skills=candidate.it_skills,
        work_history=candidate.work_history,
        education=candidate.education,
        further_education=candidate.further_education,
        street_address=candidate.street_address,
        postal_code=candidate.postal_code,
        city=candidate.city,
        has_coordinates=candidate.address_coords is not None,
        cv_url=candidate.cv_url,
        cv_parsed_at=candidate.cv_parsed_at,
        hidden=candidate.hidden,
        is_active=candidate.is_active,
        crm_synced_at=candidate.crm_synced_at,
        created_at=candidate.created_at,
        updated_at=candidate.updated_at,
    )
