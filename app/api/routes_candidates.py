"""Candidates API Routes - Endpoints für Kandidaten."""

import logging
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, File, Query, UploadFile, status
from fastapi.responses import StreamingResponse
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
    # Sortierung (Standard: zuletzt gesynct zuerst)
    sort_by: str = Query(default="crm_synced_at"),
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


# ==================== Kandidaten-Suche (fuer Pipeline-Hinzufuegen) ====================
# WICHTIG: Muss VOR /{candidate_id} stehen, sonst wird "search" als UUID interpretiert!

@router.get(
    "/search",
    summary="Kandidaten schnell suchen (fuer Autocomplete)",
)
async def search_candidates_quick(
    q: str = Query(..., min_length=2, description="Suchbegriff (Name)"),
    limit: int = Query(default=10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
):
    """
    Schnelle Kandidatensuche fuer Autocomplete/Dropdown.

    Gibt nur ID, Name und Position zurueck (kompakt).
    Wird z.B. beim Hinzufuegen von Kandidaten zur Pipeline verwendet.
    """
    from sqlalchemy import select, or_
    from app.models.candidate import Candidate

    search_term = f"%{q}%"

    result = await db.execute(
        select(Candidate)
        .where(
            Candidate.deleted_at.is_(None),
            Candidate.hidden.is_(False),
            or_(
                Candidate.first_name.ilike(search_term),
                Candidate.last_name.ilike(search_term),
                (Candidate.first_name + " " + Candidate.last_name).ilike(search_term),
            )
        )
        .order_by(Candidate.last_name, Candidate.first_name)
        .limit(limit)
    )
    candidates = result.scalars().all()

    return [
        {
            "id": str(c.id),
            "name": f"{c.first_name or ''} {c.last_name or ''}".strip() or "Unbekannt",
            "position": c.current_position,
            "city": c.city,
        }
        for c in candidates
    ]



# ==================== CV parsen ohne Kandidat (Quick-Add) ====================

@router.post(
    "/parse-cv",
    summary="CV parsen ohne Kandidat (fuer Quick-Add)",
)
@rate_limit(RateLimitTier.AI)
async def parse_cv_for_quickadd(
    file: UploadFile = File(..., description="PDF-Datei"),
    db: AsyncSession = Depends(get_db),
):
    """
    Parst ein CV-PDF und gibt strukturierte Daten zurueck,
    ohne einen Kandidaten anzulegen. Fuer den Quick-Add Workflow.
    """
    from app.services.cv_parser_service import CVParserService
    from app.services.r2_storage_service import R2StorageService

    # Datei-Validierung
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        return {"success": False, "message": "Nur PDF-Dateien erlaubt"}

    pdf_bytes = await file.read()
    if len(pdf_bytes) < 100:
        return {"success": False, "message": "Datei ist zu klein (leer?)"}
    if len(pdf_bytes) > 10 * 1024 * 1024:
        return {"success": False, "message": "Datei zu gross (max. 10 MB)"}

    # Text extrahieren
    async with CVParserService(db) as parser:
        cv_text = parser.extract_text_from_pdf(pdf_bytes)
        if not cv_text:
            return {"success": False, "message": "Konnte keinen Text aus dem PDF extrahieren"}

        # OpenAI Parsing (synchron warten)
        parse_result = await parser.parse_cv_text(cv_text)

    if not parse_result.success or not parse_result.data:
        return {
            "success": False,
            "message": parse_result.error or "CV-Parsing fehlgeschlagen",
        }

    parsed = parse_result.data

    # R2 Upload mit temporaerer ID
    cv_key = None
    try:
        r2 = R2StorageService()
        if r2.is_available:
            cv_key = r2.upload_cv(
                "temp-" + str(uuid4()),
                pdf_bytes,
                first_name=parsed.first_name or "Unbekannt",
                last_name=parsed.last_name or "Unbekannt",
            )
    except Exception as e:
        logger.warning(f"R2 Upload fuer Quick-Add fehlgeschlagen: {e}")

    return {
        "success": True,
        "cv_key": cv_key,
        "cv_text": cv_text,
        "parsed": {
            "first_name": parsed.first_name,
            "last_name": parsed.last_name,
            "birth_date": parsed.birth_date,
            "current_position": parsed.current_position,
            "current_company": None,
            "email": None,
            "phone": None,
            "street_address": parsed.street_address,
            "postal_code": parsed.postal_code,
            "city": parsed.city,
            "skills": parsed.skills or [],
            "it_skills": parsed.it_skills or [],
            "languages": [
                lang.model_dump() if hasattr(lang, "model_dump") else lang
                for lang in (parsed.languages or [])
            ],
            "work_history": [
                entry.model_dump() if hasattr(entry, "model_dump") else entry
                for entry in (parsed.work_history or [])
            ],
            "education": [
                entry.model_dump() if hasattr(entry, "model_dump") else entry
                for entry in (parsed.education or [])
            ],
            "further_education": [
                entry.model_dump() if hasattr(entry, "model_dump") else entry
                for entry in (parsed.further_education or [])
            ],
        },
    }


# ==================== Neuen Kandidaten erstellen ====================

@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Neuen Kandidaten erstellen",
)
@rate_limit(RateLimitTier.WRITE)
async def create_candidate(
    data: dict,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """
    Erstellt einen neuen Kandidaten aus den uebergebenen Daten.
    Startet Geocoding + Kategorisierung + Finance-Klassifizierung im Hintergrund.
    """
    from datetime import date as date_type
    from app.models.candidate import Candidate

    # birth_date String -> date parsen
    birth_date_val = None
    if data.get("birth_date"):
        try:
            bd = data["birth_date"]
            if isinstance(bd, str):
                # Unterstuetzte Formate: DD.MM.YYYY, YYYY-MM-DD
                if "." in bd:
                    parts = bd.split(".")
                    if len(parts) == 3:
                        birth_date_val = date_type(int(parts[2]), int(parts[1]), int(parts[0]))
                elif "-" in bd:
                    parts = bd.split("-")
                    if len(parts) == 3:
                        birth_date_val = date_type(int(parts[0]), int(parts[1]), int(parts[2]))
            elif isinstance(bd, date_type):
                birth_date_val = bd
        except (ValueError, IndexError) as e:
            logger.warning(f"Konnte birth_date nicht parsen: {data.get('birth_date')} - {e}")

    # Kandidat erstellen
    candidate = Candidate(
        first_name=data.get("first_name"),
        last_name=data.get("last_name"),
        email=data.get("email"),
        phone=data.get("phone"),
        birth_date=birth_date_val,
        current_position=data.get("current_position"),
        current_company=data.get("current_company"),
        street_address=data.get("street_address"),
        postal_code=data.get("postal_code"),
        city=data.get("city"),
        salary=data.get("salary"),
        notice_period=data.get("notice_period"),
        skills=data.get("skills"),
        it_skills=data.get("it_skills"),
        languages=data.get("languages"),
        work_history=data.get("work_history"),
        education=data.get("education"),
        further_education=data.get("further_education"),
        cv_stored_path=data.get("cv_stored_path"),
        cv_text=data.get("cv_text"),
    )

    db.add(candidate)
    await db.commit()
    await db.refresh(candidate)

    # Background-Processing: Geocoding + Kategorisierung + Finance-Klassifizierung
    background_tasks.add_task(
        _process_candidate_after_create,
        candidate.id,
    )

    return {"id": str(candidate.id), "name": candidate.full_name}


async def _process_candidate_after_create(candidate_id: UUID):
    """Background-Task: Geocoding + Kategorisierung + Finance-Klassifizierung fuer neuen Kandidaten."""
    from app.database import async_session_maker
    from app.services.geocoding_service import GeocodingService
    from app.services.categorization_service import CategorizationService
    from app.services.finance_classifier_service import FinanceClassifierService
    from sqlalchemy import select
    from app.models.candidate import Candidate

    async with async_session_maker() as db:
        try:
            # Kandidat laden
            result = await db.execute(
                select(Candidate).where(Candidate.id == candidate_id)
            )
            candidate = result.scalar_one_or_none()
            if not candidate:
                logger.error(f"Post-Create Processing: Kandidat {candidate_id} nicht gefunden")
                return

            # Schritt 1: Geocoding
            logger.info(f"Post-Create Schritt 1/3: Geocoding fuer {candidate_id}")
            try:
                geo_service = GeocodingService(db)
                await geo_service.geocode_candidate(candidate)
            except Exception as e:
                logger.warning(f"Geocoding fehlgeschlagen fuer {candidate_id}: {e}")

            # Schritt 2: Kategorisierung
            logger.info(f"Post-Create Schritt 2/3: Kategorisierung fuer {candidate_id}")
            try:
                cat_service = CategorizationService(db)
                cat_result = cat_service.categorize_candidate(candidate)
                cat_service.apply_to_candidate(candidate, cat_result)
            except Exception as e:
                logger.warning(f"Kategorisierung fehlgeschlagen fuer {candidate_id}: {e}")

            # Schritt 3: Finance-Klassifizierung (nur fuer FINANCE-Kandidaten)
            if candidate.hotlist_category == "FINANCE":
                logger.info(f"Post-Create Schritt 3/3: Finance-Klassifizierung fuer {candidate_id}")
                try:
                    fin_service = FinanceClassifierService(db)
                    fin_result = await fin_service.classify_candidate(candidate)
                    if fin_result.success:
                        fin_service.apply_to_candidate(candidate, fin_result)
                except Exception as e:
                    logger.warning(f"Finance-Klassifizierung fehlgeschlagen fuer {candidate_id}: {e}")
            else:
                logger.info(f"Post-Create Schritt 3/3: uebersprungen (nicht FINANCE) fuer {candidate_id}")

            await db.commit()
            logger.info(f"Post-Create Processing komplett fuer {candidate_id}")

        except Exception as e:
            logger.error(f"Post-Create Processing fehlgeschlagen fuer {candidate_id}: {e}")
            try:
                await db.rollback()
            except Exception:
                pass


# ==================== Einzelner Kandidat ====================

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


@router.post(
    "/{candidate_id}/upload-cv",
    summary="CV hochladen und verarbeiten",
)
@rate_limit(RateLimitTier.WRITE)
async def upload_candidate_cv(
    candidate_id: UUID,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="PDF-Datei"),
    db: AsyncSession = Depends(get_db),
):
    """
    Laedt ein CV-PDF hoch, speichert es in R2 und startet
    automatisch CV-Parsing + Geocoding + Kategorisierung + Finance-Klassifizierung.
    """
    from sqlalchemy import select
    from app.models.candidate import Candidate
    from app.services.r2_storage_service import R2StorageService

    # Kandidat laden
    result = await db.execute(
        select(Candidate).where(
            Candidate.id == candidate_id,
            Candidate.deleted_at.is_(None),
        )
    )
    candidate = result.scalar_one_or_none()
    if not candidate:
        raise NotFoundException(message="Kandidat nicht gefunden")

    # Datei-Validierung
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        return {"success": False, "message": "Nur PDF-Dateien erlaubt"}

    pdf_bytes = await file.read()
    if len(pdf_bytes) < 100:
        return {"success": False, "message": "Datei ist zu klein (leer?)"}
    if len(pdf_bytes) > 10 * 1024 * 1024:
        return {"success": False, "message": "Datei zu gross (max. 10 MB)"}

    # R2 Upload
    r2 = R2StorageService()
    if not r2.is_available:
        return {"success": False, "message": "R2 Storage nicht verfuegbar"}

    try:
        key = r2.upload_cv(
            str(candidate.id),
            pdf_bytes,
            first_name=candidate.first_name,
            last_name=candidate.last_name,
            hotlist_category=candidate.hotlist_category,
        )
    except Exception as e:
        logger.error(f"R2 Upload fehlgeschlagen fuer {candidate_id}: {e}")
        return {"success": False, "message": f"Upload fehlgeschlagen: {e}"}

    # DB aktualisieren
    candidate.cv_stored_path = key
    candidate.cv_url = candidate.cv_url or f"r2://{key}"
    await db.commit()

    # Background-Processing starten
    background_tasks.add_task(
        _process_cv_after_upload,
        candidate_id,
        pdf_bytes,
    )

    return {
        "success": True,
        "cv_key": key,
        "message": "CV hochgeladen, wird verarbeitet...",
    }


async def _process_cv_after_upload(candidate_id: UUID, pdf_bytes: bytes):
    """Background-Task: CV-Parsing + Geocoding + Kategorisierung + Finance-Klassifizierung."""
    from app.database import async_session_maker
    from app.services.cv_parser_service import CVParserService
    from app.services.geocoding_service import GeocodingService
    from app.services.categorization_service import CategorizationService
    from app.services.finance_classifier_service import FinanceClassifierService
    from sqlalchemy import select
    from app.models.candidate import Candidate

    async with async_session_maker() as db:
        try:
            # Kandidat laden
            result = await db.execute(
                select(Candidate).where(Candidate.id == candidate_id)
            )
            candidate = result.scalar_one_or_none()
            if not candidate:
                logger.error(f"CV-Processing: Kandidat {candidate_id} nicht gefunden")
                return

            # Schritt 1: CV-Parsing (Text-Extraktion + OpenAI)
            logger.info(f"CV-Processing Schritt 1/4: Parsing fuer {candidate_id}")
            async with CVParserService(db) as parser:
                cv_text = parser.extract_text_from_pdf(pdf_bytes)
                if cv_text:
                    parse_result = await parser.parse_cv_text(cv_text)
                    if parse_result.success and parse_result.data:
                        await parser._update_candidate_from_cv(
                            candidate, parse_result.data, cv_text
                        )
                        logger.info(f"CV-Parsing OK fuer {candidate_id}")
                    else:
                        logger.warning(
                            f"CV-Parsing fehlgeschlagen fuer {candidate_id}: "
                            f"{parse_result.error}"
                        )
                        # cv_text trotzdem speichern
                        candidate.cv_text = cv_text

            # Schritt 2: Geocoding
            logger.info(f"CV-Processing Schritt 2/4: Geocoding fuer {candidate_id}")
            try:
                geo_service = GeocodingService(db)
                await geo_service.geocode_candidate(candidate)
            except Exception as e:
                logger.warning(f"Geocoding fehlgeschlagen fuer {candidate_id}: {e}")

            # Schritt 3: Kategorisierung
            logger.info(f"CV-Processing Schritt 3/4: Kategorisierung fuer {candidate_id}")
            try:
                cat_service = CategorizationService(db)
                cat_result = cat_service.categorize_candidate(candidate)
                cat_service.apply_to_candidate(candidate, cat_result)
            except Exception as e:
                logger.warning(f"Kategorisierung fehlgeschlagen fuer {candidate_id}: {e}")

            # Schritt 4: Finance-Klassifizierung (nur fuer FINANCE-Kandidaten)
            if candidate.hotlist_category == "FINANCE":
                logger.info(f"CV-Processing Schritt 4/4: Finance-Klassifizierung fuer {candidate_id}")
                try:
                    fin_service = FinanceClassifierService(db)
                    fin_result = await fin_service.classify_candidate(candidate)
                    if fin_result.success:
                        fin_service.apply_to_candidate(candidate, fin_result)
                except Exception as e:
                    logger.warning(f"Finance-Klassifizierung fehlgeschlagen fuer {candidate_id}: {e}")
            else:
                logger.info(f"CV-Processing Schritt 4/4: uebersprungen (nicht FINANCE) fuer {candidate_id}")

            # Schritt 4.5: Verifizierte Position setzen (nach Klassifizierung)
            manual = candidate.manual_overrides or {}
            if "current_position" not in manual and candidate.hotlist_job_title:
                if candidate.hotlist_category in ("FINANCE", "ENGINEERING"):
                    candidate.current_position = candidate.hotlist_job_title
                    logger.info(
                        f"CV-Processing: Position verifiziert auf "
                        f"'{candidate.hotlist_job_title}' fuer {candidate_id}"
                    )

            await db.commit()
            logger.info(f"CV-Processing komplett fuer {candidate_id}")

        except Exception as e:
            logger.error(f"CV-Processing fehlgeschlagen fuer {candidate_id}: {e}")
            try:
                await db.rollback()
            except Exception:
                pass


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


# ==================== CV-Proxy (fuer iframe-Vorschau) ====================

def _is_word_document(content: bytes, url: str | None = None) -> bool:
    """Erkennt ob der Inhalt ein Word-Dokument ist (DOCX oder DOC)."""
    # DOCX = ZIP-Archiv (PK Header)
    if content[:4] == b"PK\x03\x04":
        return True
    # DOC = OLE2 Compound File (D0CF Header)
    if content[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return True
    # Fallback: URL-Endung pruefen
    if url and any(url.lower().endswith(ext) for ext in (".docx", ".doc")):
        return True
    return False


async def _convert_word_to_pdf(word_content: bytes) -> bytes:
    """Konvertiert Word-Dokument (DOC/DOCX) zu PDF via LibreOffice (async)."""
    import asyncio
    import tempfile
    import os
    import shutil

    if not shutil.which("soffice"):
        raise RuntimeError("LibreOffice nicht installiert (soffice nicht gefunden)")

    tmpdir = tempfile.mkdtemp()
    try:
        # Word-Datei speichern
        input_path = os.path.join(tmpdir, "document.docx")
        with open(input_path, "wb") as f:
            f.write(word_content)

        # HOME + LibreOffice User-Profil in tmpdir
        env = os.environ.copy()
        env["HOME"] = tmpdir
        env["TMPDIR"] = tmpdir

        # Eigenes User-Profil pro Aufruf (verhindert Lock-Konflikte)
        user_profile = f"file://{tmpdir}/libreoffice_profile"

        # LibreOffice async ausfuehren (blockiert nicht den Event Loop)
        process = await asyncio.create_subprocess_exec(
            "soffice",
            "--headless",
            "--norestore",
            "--nofirststartwizard",
            f"-env:UserInstallation={user_profile}",
            "--convert-to", "pdf",
            "--outdir", tmpdir,
            input_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            raise RuntimeError("Word-zu-PDF Konvertierung Timeout (60s)")

        if process.returncode != 0:
            stderr_text = stderr.decode(errors="replace")
            logger.error(f"LibreOffice Konvertierung fehlgeschlagen (exit {process.returncode}): {stderr_text}")
            raise RuntimeError(f"Word-zu-PDF Konvertierung fehlgeschlagen: {stderr_text[:200]}")

        # PDF lesen
        pdf_path = os.path.join(tmpdir, "document.pdf")
        if not os.path.exists(pdf_path):
            # Manchmal erzeugt LibreOffice Dateien mit anderem Namen
            pdf_files = [f for f in os.listdir(tmpdir) if f.endswith(".pdf")]
            if pdf_files:
                pdf_path = os.path.join(tmpdir, pdf_files[0])
            else:
                stdout_text = stdout.decode(errors="replace") if stdout else ""
                logger.error(f"LibreOffice hat kein PDF erzeugt. stdout: {stdout_text}")
                raise RuntimeError("PDF wurde nicht erstellt — LibreOffice hat keine Ausgabe erzeugt")

        with open(pdf_path, "rb") as f:
            return f.read()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@router.get(
    "/{candidate_id}/cv-preview",
    summary="CV als PDF-Proxy fuer iframe-Vorschau",
)
async def cv_preview_proxy(
    candidate_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """
    Proxy-Endpoint der das CV als PDF liefert.

    Unterstuetzte Formate: PDF, DOCX, DOC (Word wird automatisch konvertiert).

    Reihenfolge:
    1. Aus R2 Object Storage (wenn cv_stored_path vorhanden)
    2. Fallback: Vom CRM-Server holen (EINMALIG in R2 speichern)
    """
    from app.services.r2_storage_service import R2StorageService

    candidate_service = CandidateService(db)
    candidate = await candidate_service.get_candidate(candidate_id)

    if not candidate:
        raise NotFoundException(message="Kandidat nicht gefunden")

    if not candidate.cv_stored_path and not candidate.cv_url:
        raise NotFoundException(message="Kein CV vorhanden")

    r2 = R2StorageService()

    # 1. Aus R2 laden (wenn bereits gespeichert)
    if candidate.cv_stored_path and r2.is_available:
        try:
            content = r2.download_cv(candidate.cv_stored_path)
            if content:
                # R2-Datei pruefen: Word-Dokument konvertieren
                if _is_word_document(content, candidate.cv_stored_path):
                    logger.info(f"Word-Dokument in R2 erkannt fuer {candidate.full_name}, konvertiere zu PDF")
                    try:
                        pdf_content = await _convert_word_to_pdf(content)
                    except RuntimeError as e:
                        logger.error(f"Word-Konvertierung fehlgeschlagen (R2) fuer {candidate.full_name}: {e}")
                        raise NotFoundException(
                            message=f"CV ist ein Word-Dokument und konnte nicht konvertiert werden: {e}"
                        )
                    # Konvertiertes PDF in R2 ueberschreiben (nur 1x konvertieren)
                    try:
                        r2.client.put_object(
                            Bucket=r2.bucket,
                            Key=candidate.cv_stored_path,
                            Body=pdf_content,
                            ContentType="application/pdf",
                        )
                        logger.info(f"Word-CV in R2 durch PDF ersetzt: {candidate.cv_stored_path}")
                    except Exception:
                        pass
                    content = pdf_content
                return StreamingResponse(
                    iter([content]),
                    media_type="application/pdf",
                    headers={
                        "Content-Disposition": "inline",
                        "Cache-Control": "private, max-age=3600",
                    },
                )
        except Exception as e:
            logger.warning(f"R2 Download/Konvertierung fehlgeschlagen, Fallback auf CRM: {e}")

    # 2. Fallback: Vom CRM-Server holen
    if not candidate.cv_url:
        raise NotFoundException(message="Kein CV vorhanden")

    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        response = await client.get(candidate.cv_url)

    if response.status_code != 200:
        raise NotFoundException(message="CV konnte nicht geladen werden")

    file_content = response.content

    # Word-Dokument? → zu PDF konvertieren
    if _is_word_document(file_content, candidate.cv_url):
        logger.info(f"Word-Dokument vom CRM erkannt fuer {candidate.full_name}, konvertiere zu PDF")
        try:
            pdf_content = await _convert_word_to_pdf(file_content)
        except RuntimeError as e:
            logger.error(f"Word-Konvertierung fehlgeschlagen (CRM) fuer {candidate.full_name}: {e}")
            raise NotFoundException(
                message=f"CV ist ein Word-Dokument und konnte nicht konvertiert werden: {e}"
            )
    else:
        pdf_content = file_content

    # EINMALIG in R2 speichern (nur wenn noch nicht vorhanden)
    if r2.is_available and not candidate.cv_stored_path:
        try:
            key = r2.upload_cv(
                str(candidate.id),
                pdf_content,
                first_name=candidate.first_name,
                last_name=candidate.last_name,
                hotlist_category=candidate.hotlist_category,
            )
            candidate.cv_stored_path = key
            await db.commit()
            logger.info(f"CV fuer {candidate.full_name} einmalig in R2 gespeichert: {key}")
        except Exception as e:
            logger.warning(f"R2 Auto-Upload fehlgeschlagen fuer {candidate.id}: {e}")

    return StreamingResponse(
        iter([pdf_content]),
        media_type="application/pdf",
        headers={
            "Content-Disposition": "inline",
            "Cache-Control": "private, max-age=300",
        },
    )


# ==================== R2 Migration ====================

@router.post(
    "/migrate-cvs-to-r2",
    summary="Migriert bestehende CVs von CRM-URLs nach R2",
)
async def migrate_cvs_to_r2(
    batch_size: int = Query(default=20, le=100),
    db: AsyncSession = Depends(get_db),
):
    """
    Migriert CVs die noch nicht in R2 gespeichert sind.

    Holt PDFs von CRM-URLs und laedt sie nach R2 hoch.
    Laeuft in Batches um Timeouts zu vermeiden.
    """
    from sqlalchemy import select
    from app.models.candidate import Candidate
    from app.services.r2_storage_service import R2StorageService

    r2 = R2StorageService()
    if not r2.is_available:
        return {"error": "R2 Storage nicht konfiguriert", "migrated": 0}

    # Kandidaten mit CV-URL aber ohne R2-Pfad finden
    stmt = (
        select(Candidate)
        .where(Candidate.cv_url.isnot(None))
        .where(Candidate.cv_stored_path.is_(None))
        .where(Candidate.deleted_at.is_(None))
        .limit(batch_size)
    )
    result = await db.execute(stmt)
    candidates = result.scalars().all()

    migrated = 0
    errors = 0

    for candidate in candidates:
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
                response = await client.get(candidate.cv_url)

            if response.status_code == 200 and len(response.content) > 100:
                key = r2.upload_cv(
                    str(candidate.id),
                    response.content,
                    first_name=candidate.first_name,
                    last_name=candidate.last_name,
                    hotlist_category=candidate.hotlist_category,
                )
                candidate.cv_stored_path = key
                migrated += 1
            else:
                errors += 1
                logger.warning(
                    f"CV-Migration: HTTP {response.status_code} fuer {candidate.id}"
                )
        except Exception as e:
            errors += 1
            logger.warning(f"CV-Migration fehlgeschlagen fuer {candidate.id}: {e}")

    await db.commit()

    # Wie viele sind noch offen?
    count_stmt = (
        select(Candidate)
        .where(Candidate.cv_url.isnot(None))
        .where(Candidate.cv_stored_path.is_(None))
        .where(Candidate.deleted_at.is_(None))
    )
    remaining_result = await db.execute(count_stmt)
    remaining = len(remaining_result.scalars().all())

    return {
        "migrated": migrated,
        "errors": errors,
        "remaining": remaining,
        "message": f"{migrated} CVs nach R2 migriert, {remaining} verbleibend",
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
