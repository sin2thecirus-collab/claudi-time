"""Jobs API Routes - Endpoints für Stellenanzeigen."""

import logging
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, UploadFile, File, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
import math
from sqlalchemy import select, func, desc
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

# ── Globaler Job-Klassifizierungs-Fortschritt (In-Memory) ──────────────
_job_classification_progress: dict = {
    "running": False,
    "started_at": None,
    "total": 0,
    "processed": 0,
    "classified": 0,
    "errors": 0,
    "cost_usd": 0.0,
    "last_update": None,
    "result": None,
}


# ==================== Import ====================

class PipelineCancelled(Exception):
    """Wird geworfen wenn die Pipeline abgebrochen wird."""
    pass


async def _run_pipeline_background(import_job_id):
    """
    Fuehrt die Post-Import-Pipeline im Hintergrund aus.
    Fortschritt wird in Memory geschrieben (app.state), NICHT in die DB.
    Nur am Ende wird einmal in die DB geschrieben (fuer Import-History).
    Prueft vor jedem Step ob ein Cancel angefordert wurde.
    """
    from app.database import async_session_maker
    from app.state import set_progress, cleanup_progress, is_cancelled

    job_id_str = str(import_job_id)
    step_names = ["categorization", "classification", "geocoding"]
    pipeline = {name: {"status": "pending"} for name in step_names}
    cancelled = False

    def check_cancel():
        """Prueft ob Cancel angefordert wurde, wirft Exception."""
        if is_cancelled(job_id_str):
            raise PipelineCancelled("Pipeline abgebrochen")

    # --- Schritt 1: Kategorisierung ---
    try:
        check_cancel()
        pipeline["categorization"]["status"] = "running"
        set_progress(job_id_str, {"pipeline": dict(pipeline), "pipeline_status": "running"})
        try:
            async with async_session_maker() as step_db:
                from app.services.categorization_service import CategorizationService
                cat_service = CategorizationService(step_db)
                cat_result = await cat_service.categorize_all_jobs()
                await step_db.commit()
                pipeline["categorization"] = {
                    "status": "ok",
                    "categorized": getattr(cat_result, "categorized", 0),
                    "finance": getattr(cat_result, "finance", 0),
                    "engineering": getattr(cat_result, "engineering", 0),
                }
        except PipelineCancelled:
            raise
        except Exception as e:
            pipeline["categorization"] = {"status": "failed", "error": str(e)[:200]}
            logger.warning(f"Pipeline: categorization fehlgeschlagen: {e}", exc_info=True)
        set_progress(job_id_str, {"pipeline": dict(pipeline), "pipeline_status": "running"})
        logger.info(f"Pipeline: categorization -> {pipeline['categorization']['status']}")

        # --- Schritt 1.5: Deep Classification (FINANCE-Jobs) ---
        check_cancel()
        pipeline["classification"] = {"status": "running", "progress": 0}
        set_progress(job_id_str, {"pipeline": dict(pipeline), "pipeline_status": "running"})

        def classification_progress(processed, total):
            """Callback: wird nach jedem klassifizierten Job aufgerufen."""
            if is_cancelled(job_id_str):
                raise PipelineCancelled("Pipeline abgebrochen")
            pct = round(processed / total * 100) if total > 0 else 0
            pipeline["classification"]["progress"] = pct
            pipeline["classification"]["processed"] = processed
            pipeline["classification"]["total"] = total
            set_progress(job_id_str, {"pipeline": dict(pipeline), "pipeline_status": "running"})

        try:
            async with async_session_maker() as step_db:
                from app.services.finance_classifier_service import FinanceClassifierService
                classifier = FinanceClassifierService(step_db)
                class_result = await classifier.deep_classify_finance_jobs(
                    progress_callback=classification_progress
                )
                await step_db.commit()
                pipeline["classification"] = {
                    "status": "ok",
                    "classified": class_result.get("classified", 0),
                    "high_quality": class_result.get("high_quality", 0),
                    "medium_quality": class_result.get("medium_quality", 0),
                    "low_quality": class_result.get("low_quality", 0),
                    "titles_corrected": class_result.get("titles_corrected", 0),
                    "cost_usd": class_result.get("cost_usd", 0),
                }
        except PipelineCancelled:
            raise
        except Exception as e:
            pipeline["classification"] = {"status": "failed", "error": str(e)[:200]}
            logger.warning(f"Pipeline: classification fehlgeschlagen: {e}", exc_info=True)
        set_progress(job_id_str, {"pipeline": dict(pipeline), "pipeline_status": "running"})
        logger.info(f"Pipeline: classification -> {pipeline['classification']['status']}")

        # --- Schritt 2: Geocoding ---
        check_cancel()
        pipeline["geocoding"]["status"] = "running"
        set_progress(job_id_str, {"pipeline": dict(pipeline), "pipeline_status": "running"})
        try:
            async with async_session_maker() as step_db:
                from app.services.geocoding_service import GeocodingService
                geo_service = GeocodingService(step_db)
                geo_result = await geo_service.process_pending_jobs()
                await step_db.commit()
                pipeline["geocoding"] = {
                    "status": "ok",
                    "successful": getattr(geo_result, "successful", 0),
                    "skipped": getattr(geo_result, "skipped", 0),
                    "failed": getattr(geo_result, "failed", 0),
                }
        except PipelineCancelled:
            raise
        except Exception as e:
            pipeline["geocoding"] = {"status": "failed", "error": str(e)[:200]}
            logger.warning(f"Pipeline: geocoding fehlgeschlagen: {e}", exc_info=True)
        set_progress(job_id_str, {"pipeline": dict(pipeline), "pipeline_status": "running"})
        logger.info(f"Pipeline: geocoding -> {pipeline['geocoding']['status']}")

        # v4: Pipeline stoppt hier nach Geocoding.
        # Profiling, Embedding, Matching werden NICHT mehr automatisch ausgefuehrt.
        # Auto-Trigger: Claude Matching im Hintergrund starten
        try:
            import asyncio
            from app.services.v5_matching_service import run_matching, get_status
            if not get_status()["running"]:
                asyncio.create_task(run_matching())
                logger.info("V5 Matching automatisch gestartet nach CSV-Import")
            else:
                logger.info("V5 Matching laeuft bereits, kein Auto-Trigger")
        except Exception as e:
            logger.warning(f"V5 Matching Auto-Trigger: {e}")

    except PipelineCancelled:
        cancelled = True
        # Alle noch laufenden/pending Steps als "cancelled" markieren
        for step_name in step_names:
            if pipeline[step_name].get("status") in ("running", "pending"):
                pipeline[step_name]["status"] = "cancelled"
        logger.info(f"Pipeline fuer Import {import_job_id} ABGEBROCHEN")

    # Memory auf "done" oder "cancelled" setzen
    final_status = "cancelled" if cancelled else "done"
    set_progress(job_id_str, {"pipeline": dict(pipeline), "pipeline_status": final_status})
    logger.info(f"Pipeline fuer Import {import_job_id}: {final_status}")

    # Einmal am Ende in DB schreiben (fuer Import-History)
    try:
        async with async_session_maker() as final_db:
            from app.models.import_job import ImportJob
            result = await final_db.execute(
                select(ImportJob).where(ImportJob.id == import_job_id)
            )
            ij = result.scalar_one_or_none()
            if ij:
                ed = dict(ij.errors_detail) if ij.errors_detail else {}
                ed["pipeline"] = pipeline
                ed["pipeline_status"] = final_status
                ij.errors_detail = ed
                await final_db.commit()
    except Exception as e:
        logger.warning(f"Pipeline-Ergebnis in DB speichern fehlgeschlagen: {e}")

    # Memory-Eintrag nach kurzer Verzoegerung entfernen
    import asyncio
    await asyncio.sleep(10)
    cleanup_progress(job_id_str)


async def _execute_pipeline_steps() -> dict:
    """
    Fuehrt alle 5 Pipeline-Schritte aus (ohne Live-Tracking).
    Wird vom Maintenance-Endpoint verwendet.
    """
    from app.database import async_session_maker

    pipeline = {}

    # --- Kategorisierung ---
    try:
        async with async_session_maker() as step_db:
            from app.services.categorization_service import CategorizationService
            cat_service = CategorizationService(step_db)
            cat_result = await cat_service.categorize_all_jobs()
            await step_db.commit()
            pipeline["categorization"] = {
                "status": "ok",
                "categorized": getattr(cat_result, "categorized", 0),
                "finance": getattr(cat_result, "finance", 0),
                "engineering": getattr(cat_result, "engineering", 0),
            }
    except Exception as e:
        pipeline["categorization"] = {"status": "failed", "error": str(e)[:200]}

    # --- Deep Classification (Step 1.5) ---
    try:
        async with async_session_maker() as step_db:
            from app.services.finance_classifier_service import FinanceClassifierService
            classifier = FinanceClassifierService(step_db)
            class_result = await classifier.deep_classify_finance_jobs()
            await step_db.commit()
            pipeline["classification"] = {
                "status": "ok",
                "classified": class_result.get("classified", 0),
                "high_quality": class_result.get("high_quality", 0),
                "medium_quality": class_result.get("medium_quality", 0),
                "low_quality": class_result.get("low_quality", 0),
                "titles_corrected": class_result.get("titles_corrected", 0),
                "cost_usd": class_result.get("cost_usd", 0),
            }
    except Exception as e:
        pipeline["classification"] = {"status": "failed", "error": str(e)[:200]}

    # --- Geocoding ---
    try:
        async with async_session_maker() as step_db:
            from app.services.geocoding_service import GeocodingService
            geo_service = GeocodingService(step_db)
            geo_result = await geo_service.process_pending_jobs()
            await step_db.commit()
            pipeline["geocoding"] = {
                "status": "ok",
                "successful": getattr(geo_result, "successful", 0),
                "skipped": getattr(geo_result, "skipped", 0),
                "failed": getattr(geo_result, "failed", 0),
            }
    except Exception as e:
        pipeline["geocoding"] = {"status": "failed", "error": str(e)[:200]}

    # v4: Pipeline stoppt nach Geocoding.
    # Matching wird separat ueber das Action Board (Claude Matching v4) ausgeloest.

    return pipeline


@router.post("/import-preview")
async def preview_csv_import(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    """CSV-Vorschau: Zeigt Staedte + verfuegbare Kandidaten vor dem Import."""
    import csv
    import io
    from app.models.candidate import Candidate

    content = await file.read()
    text = content.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text), delimiter=";")

    city_jobs: dict[str, int] = {}
    total = 0
    for row in reader:
        city = (row.get("city") or row.get("Stadt") or row.get("Ort") or "").strip()
        if city:
            city_jobs[city] = city_jobs.get(city, 0) + 1
        total += 1

    result = []
    for city, job_count in sorted(city_jobs.items(), key=lambda x: x[1], reverse=True):
        cand_count_q = select(func.count(Candidate.id)).where(
            Candidate.deleted_at.is_(None),
            Candidate.hidden == False,
            Candidate.city == city,
        )
        cand_result = await db.execute(cand_count_q)
        cand_count = cand_result.scalar() or 0

        empfehlung = "importieren" if cand_count > 0 else "ueberspringen"
        result.append({
            "city": city,
            "job_count": job_count,
            "candidate_count": cand_count,
            "empfehlung": empfehlung,
        })

    relevant = sum(1 for r in result if r["empfehlung"] == "importieren")

    return {
        "total_jobs": total,
        "cities": result[:30],
        "relevant_count": sum(r["job_count"] for r in result if r["empfehlung"] == "importieren"),
        "skip_count": sum(r["job_count"] for r in result if r["empfehlung"] == "ueberspringen"),
        "recommendation": f"{relevant} Staedte mit Kandidaten, Rest kann uebersprungen werden",
    }


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

    Die Post-Import Pipeline (Kategorisierung, Classification, Geocoding)
    laeuft automatisch im Hintergrund NACH der Response.
    Matching wird separat ueber das Action Board (Claude Matching v4) ausgeloest.
    """
    import asyncio
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

            # Import direkt ausfuehren (nur CSV-Verarbeitung, OHNE Pipeline)
            if import_job.status.value == "pending":
                import_job = await import_service.process_import(import_job.id, content)

        except Exception as e:
            logger.error(f"Import fehlgeschlagen: {e}", exc_info=True)
            try:
                await db.rollback()
            except Exception:
                pass
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

    # Pipeline im Hintergrund starten (eigene DB-Session, unabhaengig vom Request)
    if import_job.successful_rows and import_job.successful_rows > 0:
        from app.state import set_progress

        # Pipeline-Status SOFORT in Memory setzen (kein DB-Write noetig)
        step_names = ["categorization", "geocoding", "profiling", "embedding", "matching"]
        pipeline_init = {name: {"status": "pending"} for name in step_names}
        set_progress(str(import_job.id), {
            "pipeline": pipeline_init,
            "pipeline_status": "running",
        })

        # Lokales Objekt updaten fuer die erste HTMX-Response
        ed = dict(import_job.errors_detail) if import_job.errors_detail else {}
        ed["pipeline"] = pipeline_init
        ed["pipeline_status"] = "running"
        import_job.errors_detail = ed

        asyncio.create_task(_run_pipeline_background(import_job.id))
        logger.info(f"Pipeline-Background-Task gestartet fuer Import {import_job.id}")

    # HTMX-Request: HTML zurueckgeben
    is_htmx = request.headers.get("HX-Request") == "true"
    if is_htmx:
        from fastapi.templating import Jinja2Templates
        templates = Jinja2Templates(directory="app/templates")
        # Pipeline-Daten als separate Variablen (nicht ueber detached object!)
        ed = import_job.errors_detail or {}
        return templates.TemplateResponse(
            "components/import_progress.html",
            {
                "request": request,
                "import_job": import_job,
                "pipeline_override": ed.get("pipeline", {}),
                "pipeline_status_override": ed.get("pipeline_status", ""),
            },
        )

    return ImportJobResponse.model_validate(import_job)


@router.get(
    "/import/history",
    summary="Import-Historie mit Pipeline-Details",
)
async def get_import_history(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    status: str = Query(None, alias="status", description="completed, failed, cancelled"),
    date_from: str = Query(None, description="ISO date YYYY-MM-DD"),
    date_to: str = Query(None, description="ISO date YYYY-MM-DD"),
    db: AsyncSession = Depends(get_db),
):
    """Listet alle vergangenen Imports mit Pipeline-Ergebnissen auf."""
    from app.models.import_job import ImportJob, ImportStatus as IS
    from datetime import date as date_type

    query = select(ImportJob).where(
        ImportJob.status.in_([IS.COMPLETED, IS.FAILED, IS.CANCELLED])
    )

    # Status-Filter
    if status:
        status_map = {"completed": IS.COMPLETED, "failed": IS.FAILED, "cancelled": IS.CANCELLED}
        if status in status_map:
            query = select(ImportJob).where(ImportJob.status == status_map[status])

    # Datums-Filter
    if date_from:
        try:
            d = date_type.fromisoformat(date_from)
            query = query.where(func.date(ImportJob.created_at) >= d)
        except ValueError:
            pass
    if date_to:
        try:
            d = date_type.fromisoformat(date_to)
            query = query.where(func.date(ImportJob.created_at) <= d)
        except ValueError:
            pass

    # Total zaehlen
    count_q = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    # Paginiert laden (neueste zuerst)
    query = query.order_by(desc(ImportJob.created_at))
    offset = (page - 1) * per_page
    query = query.offset(offset).limit(per_page)

    result = await db.execute(query)
    imports = result.scalars().all()

    def _build_import_dict(ij):
        duration = None
        if ij.started_at and ij.completed_at:
            duration = round((ij.completed_at - ij.started_at).total_seconds())

        ed = ij.errors_detail or {}
        return {
            "id": str(ij.id),
            "filename": ij.filename,
            "status": ij.status.value,
            "total_rows": ij.total_rows,
            "successful_rows": ij.successful_rows,
            "failed_rows": ij.failed_rows,
            "duplicates_updated": ed.get("duplicates_updated", 0),
            "blacklisted_skipped": ed.get("blacklisted_skipped", 0),
            "pipeline": ed.get("pipeline", {}),
            "error_message": ij.error_message,
            "started_at": ij.started_at.isoformat() if ij.started_at else None,
            "completed_at": ij.completed_at.isoformat() if ij.completed_at else None,
            "duration_seconds": duration,
            "created_at": ij.created_at.isoformat() if ij.created_at else None,
        }

    return JSONResponse({
        "imports": [_build_import_dict(ij) for ij in imports],
        "total": total,
        "page": page,
        "pages": math.ceil(total / per_page) if total > 0 else 0,
    })


@router.get(
    "/import/{import_id}/detail",
    summary="Detaillierter Import-Status mit Pipeline + Fehlern",
)
async def get_import_detail(
    import_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Gibt alle Details eines Imports zurueck inkl. Pipeline-Ergebnisse und Fehler."""
    from app.models.import_job import ImportJob

    ij = await db.get(ImportJob, import_id)
    if not ij:
        raise NotFoundException(message="Import-Job nicht gefunden")

    duration = None
    if ij.started_at and ij.completed_at:
        duration = round((ij.completed_at - ij.started_at).total_seconds())

    ed = ij.errors_detail or {}
    return JSONResponse({
        "id": str(ij.id),
        "filename": ij.filename,
        "status": ij.status.value,
        "total_rows": ij.total_rows,
        "processed_rows": ij.processed_rows,
        "successful_rows": ij.successful_rows,
        "failed_rows": ij.failed_rows,
        "duplicates_updated": ed.get("duplicates_updated", 0),
        "blacklisted_skipped": ed.get("blacklisted_skipped", 0),
        "pipeline": ed.get("pipeline", {}),
        "import_errors": ed.get("import_errors", []),
        "error_message": ij.error_message,
        "started_at": ij.started_at.isoformat() if ij.started_at else None,
        "completed_at": ij.completed_at.isoformat() if ij.completed_at else None,
        "duration_seconds": duration,
        "created_at": ij.created_at.isoformat() if ij.created_at else None,
    })


@router.get(
    "/import/{import_id}/status",
    summary="Import-Status abfragen",
)
async def get_import_status(
    request: Request,
    import_id: UUID,
):
    """
    Gibt den aktuellen Status eines Imports zurueck.
    Liest Pipeline-Fortschritt aus Memory (waehrend Pipeline laeuft)
    oder aus DB (wenn Pipeline fertig / kein Memory-Eintrag).
    Eigene DB-Session — kein Depends(get_db), keine Session-Probleme.
    """
    from app.state import get_progress
    from app.database import async_session_maker
    from app.models.import_job import ImportJob

    try:
        # DB: Import-Job laden (eigene Session)
        async with async_session_maker() as db:
            result = await db.execute(
                select(ImportJob).where(ImportJob.id == import_id)
            )
            import_job = result.scalar_one_or_none()

        if not import_job:
            raise NotFoundException(message="Import-Job nicht gefunden")

        # Memory-First: Pipeline-Fortschritt aus Memory lesen
        # WICHTIG: Daten als SEPARATE Variablen uebergeben, NICHT ueber
        # das detached SQLAlchemy-Objekt (MutableDict auf detached object
        # gibt Jinja2 die alten DB-Werte statt der neuen Memory-Werte!)
        mem_progress = get_progress(str(import_id))
        if mem_progress:
            pipeline_data = mem_progress.get("pipeline", {})
            pipeline_status_data = mem_progress.get("pipeline_status", "")
        else:
            # Fallback: aus DB lesen (Pipeline fertig oder kein Memory)
            ed = import_job.errors_detail or {}
            pipeline_data = ed.get("pipeline", {})
            pipeline_status_data = ed.get("pipeline_status", "")

        # HTMX-Request: HTML zurueckgeben (KEIN Cache — Polling muss frische Daten liefern)
        # Polling laeuft via JavaScript (fetch + setInterval), nicht via HTMX.
        is_htmx = request.headers.get("HX-Request") == "true"
        if is_htmx:
            response = templates.TemplateResponse(
                "components/import_progress.html",
                {
                    "request": request,
                    "import_job": import_job,
                    "pipeline_override": pipeline_data,
                    "pipeline_status_override": pipeline_status_data,
                },
            )
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            return response

        return ImportJobResponse.model_validate(import_job)

    except NotFoundException:
        raise
    except Exception as e:
        logger.warning(f"Status-Endpoint Fehler: {e}", exc_info=True)
        # Bei Fehler: leere HTML-Response statt 500 (verhindert Toast-Kaskade)
        is_htmx = request.headers.get("HX-Request") == "true"
        if is_htmx:
            return HTMLResponse(content="", status_code=200)
        return JSONResponse(
            status_code=500,
            content={"error": "internal_error"},
        )


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
        ed = import_job.errors_detail or {}
        # is_polling=True: Nur inneren Content liefern (fuer innerHTML-Swap in Wrapper).
        # Beim naechsten Poll-Cycle erkennt der Status-Endpoint "cancelled" → is_polling=False
        # → Wrapper wird per outerHTML ersetzt (Polling stoppt).
        return templates.TemplateResponse(
            "components/import_progress.html",
            {
                "request": request,
                "import_job": import_job,
                "pipeline_override": ed.get("pipeline", {}),
                "pipeline_status_override": ed.get("pipeline_status", ""),
            },
        )

    return ImportJobResponse.model_validate(import_job)


@router.post(
    "/import/{import_id}/cancel-pipeline",
    summary="Laufende Pipeline abbrechen",
)
async def cancel_pipeline(
    import_id: UUID,
):
    """Bricht eine laufende Post-Import-Pipeline sofort ab."""
    from app.state import request_cancel, get_progress

    progress = get_progress(str(import_id))
    if not progress:
        return JSONResponse(
            status_code=404,
            content={"message": "Keine laufende Pipeline fuer diesen Import"},
        )

    request_cancel(str(import_id))
    return {"message": "Pipeline-Abbruch angefordert", "import_id": str(import_id)}


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
    background_tasks: BackgroundTasks,
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

    # Auto-Geocoding im Hintergrund (Adresse → Koordinaten)
    from app.services.geocoding_service import process_job_after_create
    background_tasks.add_task(process_job_after_create, job.id)

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
    data: BatchDeleteRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Löscht mehrere Jobs auf einmal (Soft-Delete).

    Maximal 100 Jobs pro Anfrage.
    """
    logger.info(f"Batch delete: {len(data.ids)} Jobs angefragt")
    job_service = JobService(db)
    deleted_count = await job_service.batch_delete(data.ids)
    logger.info(f"Batch delete: {deleted_count} Jobs geloescht")
    return {"deleted_count": deleted_count}


@router.post(
    "/batch/to-pipeline",
    summary="Mehrere Jobs zur Interview-Pipeline hinzufuegen",
)
@rate_limit(RateLimitTier.WRITE)
async def batch_add_jobs_to_pipeline(
    data: BatchDeleteRequest,  # Wiederverwendung des Schemas fuer IDs
    db: AsyncSession = Depends(get_db),
):
    """
    Erstellt ATSJobs aus mehreren importierten Jobs und fuegt sie zur Pipeline hinzu.

    Maximal 100 Jobs pro Anfrage.
    """
    from sqlalchemy import select
    from app.services.ats_job_service import ATSJobService

    if len(data.ids) > Limits.BATCH_DELETE_MAX:
        raise ConflictException(
            message=f"Maximal {Limits.BATCH_DELETE_MAX} Jobs pro Batch erlaubt"
        )

    # Jobs laden
    result = await db.execute(
        select(Job).where(
            Job.id.in_(data.ids),
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
            source_job_id=job.id,  # Verknuepfung fuer Cascading Delete
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
    Falls bereits ein ATSJob mit dieser source_job_id existiert, wird dieser zurueckgegeben.
    """
    from app.services.ats_job_service import ATSJobService
    from app.models.ats_job import ATSJob

    job_service = JobService(db)
    job = await job_service.get_job(job_id)

    if not job:
        raise NotFoundException(message="Job nicht gefunden")

    # Pruefen ob bereits ein ATSJob mit dieser source_job_id existiert
    existing_result = await db.execute(
        select(ATSJob).where(
            ATSJob.source_job_id == job_id,
            ATSJob.deleted_at.is_(None),
        )
    )
    existing_ats_job = existing_result.scalar_one_or_none()

    if existing_ats_job:
        return {
            "message": f"'{job.position}' ist bereits in der Pipeline",
            "ats_job_id": str(existing_ats_job.id),
            "job_id": str(job_id),
            "already_exists": True,
        }

    # ATSJob erstellen mit Daten aus dem importierten Job
    ats_service = ATSJobService(db)
    ats_job = await ats_service.create_job(
        title=job.position,
        company_id=job.company_id,
        location_city=job.work_location_city or job.city,
        employment_type=job.employment_type,
        description=job.job_text,
        source=f"Import: {job.job_url}" if job.job_url else "CSV-Import",
        source_job_id=job.id,  # Verknuepfung fuer Cascading Delete
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


@router.delete(
    "/maintenance/delete-by-timerange",
    summary="Jobs in einem Zeitraum hard-deleten",
    tags=["Maintenance"],
)
async def delete_jobs_by_timerange(
    created_after: str = Query(..., description="ISO datetime, z.B. 2026-02-11T17:57:00Z"),
    created_before: str = Query(..., description="ISO datetime, z.B. 2026-02-11T17:58:00Z"),
    dry_run: bool = Query(default=True, description="True = nur zaehlen, nicht loeschen"),
):
    """
    Hard-Delete von Jobs in einem Zeitraum (fuer Cleanup nach fehlerhaften Imports).
    Loescht auch zugehoerige Matches. Nutzt eigene DB-Session (kein Depends).
    """
    from sqlalchemy import select, delete as sql_delete, func
    from datetime import datetime as dt
    from app.database import async_session_maker

    after = dt.fromisoformat(created_after.replace("Z", "+00:00"))
    before = dt.fromisoformat(created_before.replace("Z", "+00:00"))

    async with async_session_maker() as db:
        # Zaehlen
        count_q = select(func.count(Job.id)).where(
            Job.created_at >= after,
            Job.created_at <= before,
        )
        result = await db.execute(count_q)
        count = result.scalar()

        if dry_run:
            return {
                "dry_run": True,
                "jobs_found": count,
                "time_range": f"{created_after} bis {created_before}",
                "message": f"{count} Jobs gefunden. Setze dry_run=false zum Loeschen.",
            }

        # Job-IDs sammeln
        job_ids_q = select(Job.id).where(
            Job.created_at >= after,
            Job.created_at <= before,
        )
        job_ids_result = await db.execute(job_ids_q)
        job_ids = [row[0] for row in job_ids_result.all()]

        # Zugehoerige ATS-Jobs loeschen (Foreign Key: source_job_id)
        ats_deleted = 0
        try:
            from app.models.ats_job import ATSJob
            ats_del = sql_delete(ATSJob).where(ATSJob.source_job_id.in_(job_ids))
            ats_result = await db.execute(ats_del)
            ats_deleted = ats_result.rowcount
        except Exception:
            pass  # Tabelle existiert evtl. nicht

        # Zugehoerige Matches loeschen
        from app.models.match import Match
        match_del = sql_delete(Match).where(Match.job_id.in_(job_ids))
        match_result = await db.execute(match_del)
        matches_deleted = match_result.rowcount

        # Jobs hard-deleten
        job_del = sql_delete(Job).where(Job.id.in_(job_ids))
        job_result = await db.execute(job_del)
        jobs_deleted = job_result.rowcount

        await db.commit()

    return {
        "dry_run": False,
        "jobs_deleted": jobs_deleted,
        "matches_deleted": matches_deleted,
        "ats_jobs_deleted": ats_deleted,
        "time_range": f"{created_after} bis {created_before}",
    }


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


@router.post(
    "/maintenance/ensure-columns",
    summary="Fehlende Spalten aus Migrationen 019-021 direkt per SQL erstellen",
    tags=["Maintenance"],
)
@rate_limit(RateLimitTier.ADMIN)
async def ensure_migration_columns(
    db: AsyncSession = Depends(get_db),
):
    """Erstellt fehlende Spalten falls Alembic-Migrationen nicht gelaufen sind."""
    from sqlalchemy import text

    created = []

    columns_to_ensure = [
        # Migration 019: Deep Classification
        ("jobs", "classification_data", "JSONB"),
        ("jobs", "quality_score", "VARCHAR(20)"),
        # Migration 020: Drive Times
        ("matches", "drive_time_car_min", "INTEGER"),
        ("matches", "drive_time_transit_min", "INTEGER"),
        ("matches", "distance_km", "FLOAT"),
        # Migration 021: Outreach
        ("matches", "outreach_status", "VARCHAR(50)"),
        ("matches", "outreach_sent_at", "TIMESTAMPTZ"),
        ("matches", "outreach_responded_at", "TIMESTAMPTZ"),
        ("matches", "candidate_feedback", "TEXT"),
        ("matches", "presentation_status", "VARCHAR(50)"),
        ("matches", "presentation_sent_at", "TIMESTAMPTZ"),
    ]

    for table, col, col_type in columns_to_ensure:
        check = await db.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = :table AND column_name = :col"
        ), {"table": table, "col": col})
        if check.fetchone() is None:
            await db.execute(text(
                f'ALTER TABLE {table} ADD COLUMN {col} {col_type}'
            ))
            created.append(f"{table}.{col}")

    await db.commit()

    return {
        "status": "completed",
        "columns_created": created,
        "total_created": len(created),
    }


@router.post(
    "/maintenance/run-pipeline",
    summary="Pipeline nachholen fuer Jobs ohne Geocoding/Profiling/Embedding/Matching",
    tags=["Maintenance"],
)
async def run_pipeline_backfill(
    request: Request,
):
    """
    Fuehrt die komplette Post-Import-Pipeline fuer alle Jobs aus,
    die noch nicht verarbeitet wurden (Backfill).
    Nutzt eigene DB-Session (kein Depends, keine vergifteten Sessions).

    Pipeline: Kategorisierung → Classification → Geocoding (v4: stoppt hier)
    """
    import time

    start = time.time()
    pipeline = await _execute_pipeline_steps()
    duration_s = round(time.time() - start, 1)

    return {
        "status": "completed",
        "duration_seconds": duration_s,
        "pipeline": pipeline,
    }


@router.post(
    "/maintenance/cleanup-orphan-ats-jobs",
    summary="Verwaiste ATSJobs soft-deleten",
    tags=["Maintenance"],
)
async def cleanup_orphan_ats_jobs(
    db: AsyncSession = Depends(get_db),
):
    """
    Soft-deleted ATSJobs deren Quell-Job nicht mehr existiert oder geloescht wurde.

    Betrifft:
    - ATSJobs mit source_job_id die auf geloeschte Jobs zeigen
    - ATSJobs ohne source_job_id (alte Daten vor Migration)
    """
    from sqlalchemy import select, text
    from app.models.ats_job import ATSJob

    now = datetime.now(timezone.utc)

    # Pruefen ob deleted_at Spalte existiert
    try:
        check_result = await db.execute(
            text("SELECT column_name FROM information_schema.columns WHERE table_name = 'ats_jobs' AND column_name = 'deleted_at'")
        )
        has_deleted_at = check_result.fetchone() is not None

        if not has_deleted_at:
            return {
                "status": "skipped",
                "message": "Migration 011 muss erst laufen (deleted_at Spalte fehlt)",
            }

        # Pruefen ob source_job_id Spalte existiert
        check_result2 = await db.execute(
            text("SELECT column_name FROM information_schema.columns WHERE table_name = 'ats_jobs' AND column_name = 'source_job_id'")
        )
        has_source_job_id = check_result2.fetchone() is not None

        if not has_source_job_id:
            return {
                "status": "skipped",
                "message": "Migration 011 muss erst laufen (source_job_id Spalte fehlt)",
            }
    except Exception as e:
        logger.error(f"Fehler beim Pruefen der Spalten: {e}")
        return {
            "status": "error",
            "message": f"Fehler: {str(e)}",
        }

    deleted_count = 0
    deleted_with_dead_source = 0
    deleted_without_source = 0

    # 1. ATSJobs mit source_job_id deren Quell-Job geloescht wurde
    from app.models.job import Job
    result1 = await db.execute(
        select(ATSJob)
        .outerjoin(Job, ATSJob.source_job_id == Job.id)
        .where(
            ATSJob.deleted_at.is_(None),
            ATSJob.source_job_id.isnot(None),
            # Entweder Job existiert nicht mehr ODER Job ist soft-deleted
            (Job.id.is_(None) | Job.deleted_at.isnot(None))
        )
    )
    orphan_with_dead_source = result1.scalars().all()
    for ats_job in orphan_with_dead_source:
        ats_job.deleted_at = now
        deleted_count += 1
        deleted_with_dead_source += 1

    # 2. ATSJobs ohne source_job_id (alte Daten vor Migration)
    result2 = await db.execute(
        select(ATSJob).where(
            ATSJob.deleted_at.is_(None),
            ATSJob.source_job_id.is_(None),
        )
    )
    orphan_unlinked = result2.scalars().all()
    for ats_job in orphan_unlinked:
        ats_job.deleted_at = now
        deleted_count += 1
        deleted_without_source += 1

    await db.commit()

    return {
        "status": "completed",
        "deleted_with_dead_source": deleted_with_dead_source,
        "deleted_without_source": deleted_without_source,
        "total_deleted": deleted_count,
    }


# ══════════════════════════════════════════════════════════════════
# Phase 8: Deep Classification — Backfill + Debug/Status Endpoints
# ══════════════════════════════════════════════════════════════════


# Alter classification_status Endpoint entfernt — ersetzt durch neuen weiter unten
# mit live_progress + db_status (nach reclassify-finance Endpoint)


@router.get(
    "/maintenance/unclassified-ids",
    summary="IDs aller unklassifizierten Finance-Jobs",
    tags=["Maintenance"],
)
@rate_limit(RateLimitTier.STANDARD)
async def get_unclassified_ids(
    db: AsyncSession = Depends(get_db),
):
    """Gibt die IDs aller Finance-Jobs ohne classification_data zurueck."""
    from sqlalchemy import text

    result = await db.execute(text(
        "SELECT id::text FROM jobs "
        "WHERE hotlist_category = 'FINANCE' AND deleted_at IS NULL "
        "AND classification_data IS NULL "
        "ORDER BY created_at DESC"
    ))
    ids = [row[0] for row in result.fetchall()]
    return {"count": len(ids), "ids": ids}


@router.post(
    "/maintenance/reclassify-finance",
    summary="Alle FINANCE-Jobs deep-klassifizieren (Backfill)",
    tags=["Maintenance"],
)
@rate_limit(RateLimitTier.ADMIN)
async def reclassify_finance_jobs(
    request: Request,
    force: bool = False,
):
    """
    Startet Bulk Deep Classification fuer Jobs als Background-Task.
    Fortschritt abrufbar via GET /jobs/maintenance/classification-status.
    """
    global _job_classification_progress

    if _job_classification_progress["running"]:
        return {
            "status": "already_running",
            "message": "Job-Klassifizierung laeuft bereits",
            "progress": _job_classification_progress,
        }

    import asyncio
    asyncio.create_task(_run_job_classification_background(force=force))

    return {
        "status": "started",
        "message": "Job-Klassifizierung als Background-Task gestartet. Status via GET /jobs/maintenance/classification-status",
    }


async def _run_job_classification_background(force: bool = False) -> None:
    """Background-Task fuer Job-Klassifizierung.

    Verarbeitet Jobs EINZELN mit eigener DB-Session pro Job.
    10 parallel via Semaphore. Jeder Job: Load -> Classify -> Save.
    """
    global _job_classification_progress
    import asyncio

    log = logging.getLogger(__name__)

    _job_classification_progress = {
        "running": True,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "total": 0,
        "processed": 0,
        "classified": 0,
        "errors": 0,
        "cost_usd": 0.0,
        "last_update": datetime.now(timezone.utc).isoformat(),
        "result": None,
    }

    try:
        from app.database import async_session_maker
        from app.services.finance_classifier_service import FinanceClassifierService
        from sqlalchemy import and_, select
        from app.models.job import Job

        # 1. IDs laden (kurze Session)
        async with async_session_maker() as db:
            query = (
                select(Job.id)
                .where(and_(
                    Job.hotlist_category == "FINANCE",
                    Job.deleted_at.is_(None),
                ))
            )
            if not force:
                query = query.where(Job.classification_data.is_(None))
            result = await db.execute(query)
            job_ids = [row[0] for row in result.fetchall()]

        total = len(job_ids)
        _job_classification_progress["total"] = total
        log.info(f"Job-Klassifizierung: {total} IDs geladen (force={force})")

        if total == 0:
            _job_classification_progress["result"] = {"total": 0, "classified": 0, "message": "Keine Jobs"}
            return

        start_time = datetime.now(timezone.utc)
        semaphore = asyncio.Semaphore(10)  # 10 parallel (gpt-4o vertraegt das)

        stats = {
            "classified": 0, "errors": 0, "processed": 0,
            "quality_high": 0, "quality_medium": 0, "quality_low": 0,
            "input_tokens": 0, "output_tokens": 0,
            "roles": {},
            "last_errors": [],
        }

        async def _classify_one(jid):
            async with semaphore:
                try:
                    async with async_session_maker() as db2:
                        classifier = FinanceClassifierService(db2)
                        job = (await db2.execute(
                            select(Job).where(Job.id == jid)
                        )).scalar_one_or_none()
                        if not job:
                            stats["errors"] += 1
                            return

                        classification = await classifier.classify_job(job)
                        stats["input_tokens"] += classification.input_tokens
                        stats["output_tokens"] += classification.output_tokens

                        if classification.success:
                            classifier.apply_to_job(job, classification)
                            await db2.commit()
                            stats["classified"] += 1
                            # Quality tracking
                            qs = classification.quality_score
                            if qs == "high":
                                stats["quality_high"] += 1
                            elif qs == "medium":
                                stats["quality_medium"] += 1
                            elif qs == "low":
                                stats["quality_low"] += 1
                            # Role tracking
                            for role in (classification.roles or []):
                                stats["roles"][role] = stats["roles"].get(role, 0) + 1
                        else:
                            stats["errors"] += 1
                            err_msg = f"{jid}: {classification.error}"
                            log.warning(f"Job {err_msg}")
                            if len(stats["last_errors"]) < 5:
                                stats["last_errors"].append(err_msg)

                        await classifier.close()

                except Exception as e:
                    err_msg = f"{jid}: {str(e)[:200]}"
                    log.error(f"Fehler Job {err_msg}")
                    stats["errors"] += 1
                    if len(stats["last_errors"]) < 5:
                        stats["last_errors"].append(err_msg)

                finally:
                    stats["processed"] += 1
                    _job_classification_progress["processed"] = stats["processed"]
                    _job_classification_progress["classified"] = stats["classified"]
                    _job_classification_progress["errors"] = stats["errors"]
                    input_cost = (stats["input_tokens"] / 1_000_000) * 2.50
                    output_cost = (stats["output_tokens"] / 1_000_000) * 10.0
                    _job_classification_progress["cost_usd"] = round(input_cost + output_cost, 4)
                    _job_classification_progress["last_update"] = datetime.now(timezone.utc).isoformat()

        # In Chunks von 20 verarbeiten
        chunk_size = 20
        for chunk_start in range(0, total, chunk_size):
            chunk = job_ids[chunk_start:chunk_start + chunk_size]
            await asyncio.gather(*[_classify_one(jid) for jid in chunk])

            elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
            done = stats["processed"]
            rate = done / elapsed if elapsed > 0 else 0
            eta = (total - done) / rate if rate > 0 else 0

            log.info(
                f"Job-Klassifizierung: {done}/{total} "
                f"({stats['classified']} klass., {stats['errors']} err, "
                f"${_job_classification_progress['cost_usd']:.3f}, "
                f"{rate:.1f}/s, ETA {eta:.0f}s)"
            )

            await asyncio.sleep(0.5)

        # Ergebnis
        duration = (datetime.now(timezone.utc) - start_time).total_seconds()

        _job_classification_progress["result"] = {
            "total": total,
            "classified": stats["classified"],
            "errors": stats["errors"],
            "quality_high": stats["quality_high"],
            "quality_medium": stats["quality_medium"],
            "quality_low": stats["quality_low"],
            "cost_usd": _job_classification_progress["cost_usd"],
            "duration_seconds": round(duration, 1),
            "roles_distribution": stats["roles"],
            "last_errors": stats["last_errors"],
        }
        log.info(f"Job-Klassifizierung fertig: {stats['classified']}/{total} in {duration:.0f}s")

    except Exception as e:
        log.error(f"Job-Klassifizierung fehlgeschlagen: {e}")
        _job_classification_progress["result"] = {"error": str(e)}

    finally:
        _job_classification_progress["running"] = False
        _job_classification_progress["last_update"] = datetime.now(timezone.utc).isoformat()


@router.get(
    "/maintenance/classification-status",
    summary="Status der Job-Klassifizierung",
    tags=["Maintenance"],
)
@rate_limit(RateLimitTier.STANDARD)
async def job_classification_status(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Zeigt den kombinierten Status: DB-Stand + Live-Fortschritt der laufenden Job-Klassifizierung.
    """
    from sqlalchemy import text

    try:
        r1 = await db.execute(text(
            "SELECT COUNT(*) FROM jobs WHERE hotlist_category = 'FINANCE' AND deleted_at IS NULL"
        ))
        total_finance = r1.scalar() or 0

        r2 = await db.execute(text(
            "SELECT COUNT(*) FROM jobs WHERE hotlist_category = 'FINANCE' AND deleted_at IS NULL AND classification_data IS NOT NULL"
        ))
        classified = r2.scalar() or 0

        r3 = await db.execute(text(
            "SELECT COUNT(*) FROM jobs WHERE deleted_at IS NULL"
        ))
        total_all = r3.scalar() or 0

        unclassified = total_finance - classified

    except Exception as e:
        return {
            "db_status": {"error": str(e)},
            "live_progress": _job_classification_progress,
        }

    return {
        "db_status": {
            "total_jobs": total_all,
            "total_finance": total_finance,
            "classified": classified,
            "unclassified": unclassified,
            "classification_percent": round((classified / total_finance * 100), 1) if total_finance > 0 else 0,
        },
        "live_progress": _job_classification_progress,
    }


@router.post(
    "/classify/{job_id}",
    summary="Einzelnen Job deep-klassifizieren",
    tags=["Classification"],
)
@rate_limit(RateLimitTier.STANDARD)
async def classify_single_job(
    request: Request,
    job_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Deep Classification fuer einen einzelnen Job."""
    from uuid import UUID as PyUUID
    from app.services.finance_classifier_service import FinanceClassifierService

    try:
        uuid_id = PyUUID(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Ungueltige Job-ID")

    result = await db.execute(select(Job).where(Job.id == uuid_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job nicht gefunden")

    classifier = FinanceClassifierService(db)
    classification = await classifier.classify_job(job)

    if classification.success:
        classifier.apply_to_job(job, classification)
        await db.commit()

    return {
        "job_id": job_id,
        "success": classification.success,
        "error": classification.error,
        "primary_role": classification.primary_role,
        "roles": classification.roles,
        "sub_level": classification.sub_level,
        "quality_score": classification.quality_score,
        "quality_reason": classification.quality_reason,
        "original_title": classification.original_title,
        "corrected_title": classification.corrected_title,
        "title_was_corrected": classification.title_was_corrected,
        "reasoning": classification.reasoning,
        "cost_usd": classification.cost_usd,
    }


@router.get(
    "/debug/classification/{job_id}",
    summary="Klassifizierungs-Details eines Jobs",
    tags=["Debug"],
)
async def debug_classification(
    job_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Zeigt alle classification_data und quality_score eines Jobs."""
    from uuid import UUID as PyUUID

    try:
        uuid_id = PyUUID(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Ungueltige Job-ID")

    result = await db.execute(select(Job).where(Job.id == uuid_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job nicht gefunden")

    return {
        "job_id": job_id,
        "position": job.position,
        "company_name": job.company_name,
        "hotlist_job_title": job.hotlist_job_title,
        "hotlist_job_titles": job.hotlist_job_titles,
        "quality_score": getattr(job, "quality_score", None),
        "classification_data": getattr(job, "classification_data", None),
    }


@router.get(
    "/debug/classification/stats",
    summary="Klassifizierungs-Statistik (high/medium/low)",
    tags=["Debug"],
)
async def debug_classification_stats(
    db: AsyncSession = Depends(get_db),
):
    """Verteilung der Quality Scores fuer alle FINANCE-Jobs."""
    from sqlalchemy import func as sqlfunc

    # Gesamtzahl FINANCE-Jobs
    total_result = await db.execute(
        select(sqlfunc.count(Job.id)).where(
            Job.hotlist_category == "FINANCE",
            Job.deleted_at.is_(None),
        )
    )
    total = total_result.scalar() or 0

    # Mit classification_data
    classified_result = await db.execute(
        select(sqlfunc.count(Job.id)).where(
            Job.hotlist_category == "FINANCE",
            Job.deleted_at.is_(None),
            Job.classification_data.isnot(None),
        )
    )
    classified = classified_result.scalar() or 0

    # Quality-Verteilung
    quality_result = await db.execute(
        select(Job.quality_score, sqlfunc.count(Job.id)).where(
            Job.hotlist_category == "FINANCE",
            Job.deleted_at.is_(None),
            Job.quality_score.isnot(None),
        ).group_by(Job.quality_score)
    )
    quality_dist = {row[0]: row[1] for row in quality_result.all()}

    return {
        "total_finance_jobs": total,
        "classified": classified,
        "not_classified": total - classified,
        "quality_distribution": {
            "high": quality_dist.get("high", 0),
            "medium": quality_dist.get("medium", 0),
            "low": quality_dist.get("low", 0),
        },
    }


@router.get(
    "/debug/openai-test",
    summary="Testet OpenAI API direkt mit minimalen Daten",
    tags=["Debug"],
)
@rate_limit(RateLimitTier.ADMIN)
async def debug_openai_test(request: Request):
    """Raw-Test gegen OpenAI API — zeigt exakten Response inkl. Fehler."""
    import httpx
    from app.config import settings

    api_key = settings.openai_api_key
    key_preview = f"{api_key[:8]}...{api_key[-4:]}" if len(api_key) > 12 else "ZU_KURZ"

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "gpt-4o-mini",
                    "messages": [{"role": "user", "content": "Say OK"}],
                    "max_tokens": 5,
                },
            )
            return {
                "api_key_preview": key_preview,
                "api_key_length": len(api_key),
                "status_code": resp.status_code,
                "headers": dict(resp.headers),
                "body": resp.json(),
            }
    except Exception as e:
        return {
            "api_key_preview": key_preview,
            "api_key_length": len(api_key),
            "error": f"{type(e).__name__}: {str(e)}",
        }


@router.get(
    "/debug/classification/sample",
    summary="10 zufaellige klassifizierte Jobs",
    tags=["Debug"],
)
async def debug_classification_sample(
    limit: int = 10,
    db: AsyncSession = Depends(get_db),
):
    """Zeigt zufaellige klassifizierte FINANCE-Jobs mit Vorher/Nachher."""
    from sqlalchemy import func as sqlfunc

    result = await db.execute(
        select(Job).where(
            Job.hotlist_category == "FINANCE",
            Job.deleted_at.is_(None),
            Job.classification_data.isnot(None),
        ).order_by(sqlfunc.random()).limit(limit)
    )
    jobs = result.scalars().all()

    return {
        "count": len(jobs),
        "samples": [
            {
                "id": str(job.id),
                "position": job.position,
                "company_name": job.company_name,
                "quality_score": getattr(job, "quality_score", None),
                "classification_data": getattr(job, "classification_data", None),
            }
            for job in jobs
        ],
    }


@router.get(
    "/debug/classification/mismatches",
    summary="Jobs wo Titel korrigiert wurde",
    tags=["Debug"],
)
async def debug_classification_mismatches(
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
):
    """Zeigt Jobs wo der Original-Titel vom klassifizierten Titel abweicht."""
    from sqlalchemy import func as sqlfunc, cast, String

    result = await db.execute(
        select(Job).where(
            Job.hotlist_category == "FINANCE",
            Job.deleted_at.is_(None),
            Job.classification_data.isnot(None),
        ).limit(200)
    )
    jobs = result.scalars().all()

    mismatches = []
    for job in jobs:
        cd = getattr(job, "classification_data", None)
        if cd and isinstance(cd, dict) and cd.get("title_was_corrected"):
            mismatches.append({
                "id": str(job.id),
                "position": job.position,
                "company_name": job.company_name,
                "original_title": cd.get("original_title"),
                "corrected_title": cd.get("corrected_title"),
                "primary_role": cd.get("primary_role"),
                "reasoning": cd.get("reasoning"),
                "quality_score": cd.get("quality_score"),
            })
            if len(mismatches) >= limit:
                break

    return {
        "count": len(mismatches),
        "mismatches": mismatches,
    }


# ══════════════════════════════════════════════════════════════
# Job-Vorstellungs-PDF (Sincirus Branded)
# ══════════════════════════════════════════════════════════════


@router.get("/{job_id}/vorstellung-pdf")
async def get_job_vorstellung_pdf(
    job_id: UUID,
    candidate_id: UUID = Query(None, description="Optional: Kandidat fuer personalisierte Version"),
    match_id: UUID = Query(None, description="Optional: Match fuer Fahrzeit + Score"),
    db: AsyncSession = Depends(get_db),
):
    """Generiert ein Sincirus Branded Job-Vorstellungs-PDF.

    Ohne candidate_id: Generisches Job-PDF.
    Mit candidate_id/match_id: Personalisiert mit Fahrzeit + Einschaetzung.
    """
    from fastapi.responses import Response
    from app.services.job_vorstellung_pdf_service import JobVorstellungPdfService

    service = JobVorstellungPdfService(db)
    try:
        pdf_bytes = await service.generate_job_vorstellung_pdf(
            job_id=job_id,
            candidate_id=candidate_id,
            match_id=match_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    # Job-Position fuer Dateiname laden
    job = await db.execute(select(Job.position).where(Job.id == job_id))
    position = (job.scalar_one_or_none() or "Stelle").replace(" ", "_").replace("/", "_")
    filename = f"Stellenprofil_{position}.pdf"

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


# ══════════════════════════════════════════════════════════════
# Phase 10: Google Maps Fahrzeit — Debug-Endpoints
# ══════════════════════════════════════════════════════════════


@router.get("/debug/drive-times/{job_id}")
async def debug_drive_times(
    job_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Zeigt Fahrzeit-Daten für alle Matches eines Jobs."""
    from app.models.match import Match

    result = await db.execute(
        select(Match).where(
            Match.job_id == job_id,
            Match.v2_score.isnot(None),
        ).order_by(Match.v2_score.desc()).limit(50)
    )
    matches = result.scalars().all()

    if not matches:
        raise HTTPException(status_code=404, detail="Keine Matches für diesen Job gefunden")

    data = []
    for m in matches:
        data.append({
            "match_id": str(m.id),
            "candidate_id": str(m.candidate_id) if m.candidate_id else None,
            "v2_score": m.v2_score,
            "distance_km": m.distance_km,
            "drive_time_car_min": m.drive_time_car_min,
            "drive_time_transit_min": m.drive_time_transit_min,
            "has_drive_time": m.drive_time_car_min is not None,
        })

    with_drive_time = sum(1 for d in data if d["has_drive_time"])

    return {
        "job_id": str(job_id),
        "total_matches": len(data),
        "matches_with_drive_time": with_drive_time,
        "matches_without_drive_time": len(data) - with_drive_time,
        "matches": data,
    }


@router.get("/debug/drive-times/cache-stats")
async def debug_drive_time_cache_stats():
    """Zeigt Cache-Statistiken des Distance Matrix Service."""
    from app.services.distance_matrix_service import distance_matrix_service

    return distance_matrix_service.get_cache_stats()


@router.get("/debug/drive-times/summary")
async def debug_drive_time_summary(
    db: AsyncSession = Depends(get_db),
):
    """Zeigt Übersicht aller Matches mit Fahrzeit-Daten."""
    from app.models.match import Match

    # Gesamtzahl Matches
    total_result = await db.execute(
        select(func.count(Match.id)).where(Match.v2_score.isnot(None))
    )
    total = total_result.scalar() or 0

    # Matches mit Fahrzeit
    with_dt_result = await db.execute(
        select(func.count(Match.id)).where(
            Match.v2_score.isnot(None),
            Match.drive_time_car_min.isnot(None),
        )
    )
    with_drive_time = with_dt_result.scalar() or 0

    # Durchschnittliche Fahrzeit
    avg_result = await db.execute(
        select(
            func.avg(Match.drive_time_car_min).label("avg_car"),
            func.avg(Match.drive_time_transit_min).label("avg_transit"),
            func.min(Match.drive_time_car_min).label("min_car"),
            func.max(Match.drive_time_car_min).label("max_car"),
        ).where(Match.drive_time_car_min.isnot(None))
    )
    avg_row = avg_result.first()

    return {
        "total_v2_matches": total,
        "matches_with_drive_time": with_drive_time,
        "matches_without_drive_time": total - with_drive_time,
        "coverage_percent": round(with_drive_time / total * 100, 1) if total > 0 else 0,
        "avg_car_min": round(avg_row[0], 1) if avg_row and avg_row[0] else None,
        "avg_transit_min": round(avg_row[1], 1) if avg_row and avg_row[1] else None,
        "min_car_min": avg_row[2] if avg_row else None,
        "max_car_min": avg_row[3] if avg_row else None,
    }


# ═══════════════════════════════════════════════════════════════
# Job Tasks Backfill — Taetigkeiten aus job_text extrahieren
# ═══════════════════════════════════════════════════════════════

_backfill_tasks_status = {
    "running": False,
    "total": 0,
    "done": 0,
    "errors": 0,
    "cost_usd": 0.0,
    "started_at": None,
    "finished_at": None,
}


@router.post(
    "/maintenance/backfill-job-tasks",
    summary="job_tasks fuer alle FINANCE-Jobs extrahieren (Backfill)",
    tags=["Maintenance"],
)
@rate_limit(RateLimitTier.ADMIN)
async def backfill_job_tasks(request: Request):
    """
    Re-klassifiziert FINANCE-Jobs die noch kein job_tasks haben.
    Laeuft im Hintergrund — Fortschritt via GET /maintenance/backfill-job-tasks/status.
    """
    import asyncio

    if _backfill_tasks_status["running"]:
        return JSONResponse(
            status_code=409,
            content={"error": "Backfill laeuft bereits", **_backfill_tasks_status},
        )

    async def _run_backfill():
        try:
            from app.database import async_session_maker
            from app.services.finance_classifier_service import FinanceClassifierService
            from sqlalchemy import and_

            _backfill_tasks_status["running"] = True
            _backfill_tasks_status["done"] = 0
            _backfill_tasks_status["errors"] = 0
            _backfill_tasks_status["cost_usd"] = 0.0
            _backfill_tasks_status["started_at"] = datetime.now(timezone.utc).isoformat()
            _backfill_tasks_status["finished_at"] = None

            # 1. IDs laden (Jobs mit job_text aber ohne job_tasks)
            async with async_session_maker() as db:
                result = await db.execute(
                    select(Job.id).where(and_(
                        Job.hotlist_category == "FINANCE",
                        Job.deleted_at.is_(None),
                        Job.job_tasks.is_(None),
                        Job.job_text.isnot(None),
                    ))
                )
                job_ids = [row[0] for row in result.fetchall()]

            total = len(job_ids)
            _backfill_tasks_status["total"] = total
            logger.info(f"Job Tasks Backfill: {total} Jobs ohne job_tasks")

            if total == 0:
                return

            semaphore = asyncio.Semaphore(2)

            async def _classify_one(jid):
                async with semaphore:
                    try:
                        async with async_session_maker() as db2:
                            classifier = FinanceClassifierService(db2)
                            job = (await db2.execute(
                                select(Job).where(Job.id == jid)
                            )).scalar_one_or_none()
                            if not job:
                                return
                            classification = await classifier.classify_job(job)
                            if classification.success:
                                classifier.apply_to_job(job, classification)
                                await db2.commit()
                                _backfill_tasks_status["cost_usd"] += classification.cost_usd
                            else:
                                _backfill_tasks_status["errors"] += 1
                                logger.warning(f"Backfill Job {jid}: {classification.error}")
                            await classifier.close()
                    except Exception as e:
                        logger.error(f"Backfill Job {jid} Fehler: {e}")
                        _backfill_tasks_status["errors"] += 1
                    finally:
                        _backfill_tasks_status["done"] += 1

            # In Chunks von 10 mit Pause
            for i in range(0, total, 10):
                chunk = job_ids[i:i + 10]
                await asyncio.gather(*[_classify_one(jid) for jid in chunk])
                done = _backfill_tasks_status["done"]
                logger.info(f"Job Tasks Backfill: {done}/{total} ({_backfill_tasks_status['errors']} err, ${_backfill_tasks_status['cost_usd']:.4f})")
                await asyncio.sleep(2)

            logger.info(f"Job Tasks Backfill fertig: {_backfill_tasks_status}")
        except Exception as e:
            logger.error(f"Backfill Fehler: {e}")
        finally:
            _backfill_tasks_status["running"] = False
            _backfill_tasks_status["finished_at"] = datetime.now(timezone.utc).isoformat()

    asyncio.create_task(_run_backfill())
    return {"status": "started", "message": "Job Tasks Backfill laeuft im Hintergrund"}


@router.get(
    "/maintenance/backfill-job-tasks/status",
    summary="Status des Job Tasks Backfill",
    tags=["Maintenance"],
)
async def backfill_job_tasks_status():
    """Live-Fortschritt des job_tasks Backfill."""
    remaining = _backfill_tasks_status["total"] - _backfill_tasks_status["done"]
    return {
        **_backfill_tasks_status,
        "remaining": remaining,
    }


@router.get(
    "/maintenance/backfill-job-tasks/live",
    summary="Live-Status-Seite fuer Job Tasks Backfill",
    tags=["Maintenance"],
    response_class=HTMLResponse,
)
async def backfill_job_tasks_live(request: Request):
    """HTML-Seite mit Live-Fortschritt und Start-Button."""
    return HTMLResponse(content="""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<title>Job Tasks Backfill</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 600px; margin: 60px auto; padding: 0 20px; background: #0f1117; color: #e4e4e7; }
  h1 { font-size: 20px; margin-bottom: 24px; }
  .card { background: #1a1b23; border: 1px solid #2a2b35; border-radius: 12px; padding: 24px; margin-bottom: 16px; }
  .row { display: flex; justify-content: space-between; margin-bottom: 8px; font-size: 14px; }
  .label { color: #a1a1aa; }
  .value { font-weight: 600; }
  .green { color: #10b981; }
  .red { color: #ef4444; }
  .amber { color: #f59e0b; }
  .bar-bg { background: #2a2b35; border-radius: 6px; height: 8px; margin: 16px 0; overflow: hidden; }
  .bar { height: 100%; background: #6366f1; border-radius: 6px; transition: width 0.5s; }
  button { width: 100%; padding: 12px; border: none; border-radius: 8px; font-size: 14px; font-weight: 600; cursor: pointer; background: #6366f1; color: #fff; }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  .status { text-align: center; font-size: 13px; color: #a1a1aa; margin-top: 12px; }
  a { color: #6366f1; text-decoration: none; }
</style>
</head>
<body>
<h1>Job Tasks Backfill</h1>
<div class="card" id="info">Lade...</div>
<button id="startBtn" onclick="startBackfill()" disabled>Backfill starten</button>
<div class="status" id="statusText"></div>
<p style="margin-top:24px;font-size:12px;"><a href="/action-board">&larr; Zurueck zum Action Board</a></p>

<script>
async function loadStatus() {
  try {
    const r = await fetch('/api/jobs/maintenance/backfill-job-tasks/status');
    const d = await r.json();
    const pct = d.total > 0 ? Math.round(d.done / d.total * 100) : 0;
    document.getElementById('info').innerHTML = `
      <div class="row"><span class="label">Status</span><span class="value ${d.running ? 'amber' : (d.done > 0 ? 'green' : '')}">${d.running ? 'Laeuft...' : (d.done > 0 && !d.running ? 'Fertig' : 'Bereit')}</span></div>
      <div class="row"><span class="label">Gesamt</span><span class="value">${d.total}</span></div>
      <div class="row"><span class="label">Erledigt</span><span class="value green">${d.done}</span></div>
      <div class="row"><span class="label">Verbleibend</span><span class="value">${d.remaining}</span></div>
      <div class="row"><span class="label">Fehler</span><span class="value ${d.errors > 0 ? 'red' : ''}">${d.errors}</span></div>
      <div class="row"><span class="label">Kosten</span><span class="value">$${(d.cost_usd || 0).toFixed(4)}</span></div>
      ${d.total > 0 ? '<div class="bar-bg"><div class="bar" style="width:' + pct + '%"></div></div><div style="text-align:center;font-size:12px;color:#a1a1aa;">' + pct + '%</div>' : ''}
      ${d.started_at ? '<div class="row"><span class="label">Gestartet</span><span class="value" style="font-size:12px;">' + new Date(d.started_at).toLocaleString('de-DE') + '</span></div>' : ''}
      ${d.finished_at ? '<div class="row"><span class="label">Fertig</span><span class="value" style="font-size:12px;">' + new Date(d.finished_at).toLocaleString('de-DE') + '</span></div>' : ''}
    `;
    document.getElementById('startBtn').disabled = d.running;
    document.getElementById('startBtn').textContent = d.running ? 'Laeuft...' : 'Backfill starten';
    if (d.running) setTimeout(loadStatus, 2000);
  } catch(e) { document.getElementById('info').textContent = 'Fehler: ' + e.message; }
}

async function startBackfill() {
  document.getElementById('startBtn').disabled = true;
  document.getElementById('statusText').textContent = 'Wird gestartet...';
  try {
    const r = await fetch('/api/jobs/maintenance/backfill-job-tasks', {method: 'POST'});
    const d = await r.json();
    document.getElementById('statusText').textContent = d.message || d.error || 'Gestartet';
    setTimeout(loadStatus, 1000);
  } catch(e) { document.getElementById('statusText').textContent = 'Fehler: ' + e.message; }
}

loadStatus();
</script>
</body>
</html>""")
