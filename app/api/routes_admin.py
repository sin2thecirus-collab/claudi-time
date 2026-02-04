"""Admin API Routes - Endpoints für Background-Jobs und System-Verwaltung."""

import asyncio
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


# ==================== Debug: Verbindungstests ====================

@router.get(
    "/test-openai",
    summary="OpenAI-Verbindung testen",
)
async def test_openai_connection():
    """Testet ob der OpenAI API-Key gültig ist."""
    import httpx

    key = settings.openai_api_key
    result = {
        "configured": bool(key),
        "key_length": len(key) if key else 0,
        "key_prefix": key[:8] + "..." if key and len(key) > 8 else "zu kurz oder leer",
    }

    if not key:
        result["error"] = "OPENAI_API_KEY nicht konfiguriert"
        return result

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {key}"},
                timeout=10.0,
            )
            result["status_code"] = resp.status_code
            if resp.status_code == 200:
                result["connection_test"] = "ERFOLGREICH"
            else:
                result["connection_test"] = "FEHLGESCHLAGEN"
                result["error"] = resp.text[:200]
    except Exception as e:
        result["connection_test"] = "FEHLGESCHLAGEN"
        result["error"] = str(e)

    return result


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


# Globaler Status für CV-Parsing Background Task
_cv_parsing_status: dict = {
    "running": False,
    "parsed": 0,
    "failed": 0,
    "total_to_parse": 0,
    "total_tokens": 0,
    "errors": [],
    "recently_parsed": [],
    "started_at": None,
    "finished_at": None,
    "current_candidate": None,
}


@router.post(
    "/reset-cv-parsing",
    summary="CV-Parsing zurücksetzen (alle erneut parsen)",
)
async def reset_cv_parsing(
    db: AsyncSession = Depends(get_db),
    before: str | None = Query(
        default=None,
        description="Nur CVs zurücksetzen die VOR diesem Zeitpunkt geparst wurden (ISO-Format, z.B. 2026-01-28T18:08:50Z)",
    ),
):
    """Setzt cv_parsed_at auf NULL für Kandidaten, damit sie erneut geparst werden.

    - Ohne 'before': ALLE geparsten CVs zurücksetzen
    - Mit 'before': Nur CVs die VOR dem Stichtag geparst wurden (für Re-Parsing mit neuem Prompt)
    """
    from datetime import datetime, timezone
    from sqlalchemy import update

    from app.models.candidate import Candidate

    query = update(Candidate).where(
        Candidate.cv_parsed_at.isnot(None),
        Candidate.cv_parse_failed.is_(False),
    )

    if before:
        cutoff = datetime.fromisoformat(before.replace("Z", "+00:00"))
        query = query.where(Candidate.cv_parsed_at < cutoff)

    result = await db.execute(query.values(cv_parsed_at=None))
    await db.commit()

    return {
        "success": True,
        "message": f"CV-Parsing zurückgesetzt für {result.rowcount} Kandidaten"
                   + (f" (geparst vor {before})" if before else " (alle)"),
        "reset_count": result.rowcount,
        "cutoff": before,
    }


@router.post(
    "/reset-parsing-status",
    summary="CV-Parsing Status zurücksetzen",
)
async def force_reset_parsing_status():
    """Setzt den globalen Parsing-Status zurück (falls nach Neustart hängengeblieben)."""
    global _cv_parsing_status
    _cv_parsing_status = {
        "running": False,
        "parsed": 0,
        "failed": 0,
        "total_to_parse": 0,
        "total_tokens": 0,
        "errors": [],
        "recently_parsed": [],
        "started_at": None,
        "finished_at": None,
        "current_candidate": None,
    }
    return {"success": True, "message": "Parsing-Status zurückgesetzt"}


@router.post(
    "/parse-all-cvs",
    summary="Alle CVs parsen (Background Task)",
)
async def parse_all_cvs(
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    batch_size: int = Query(default=10, description="Kandidaten pro Batch"),
    max_candidates: int = Query(default=5000, description="Maximale Anzahl"),
):
    """
    Startet CV-Parsing als Background Task.
    Gibt sofort eine Antwort zurück. Status über GET /parse-all-cvs/status abrufen.
    """
    from sqlalchemy import select, func

    from app.models.candidate import Candidate

    global _cv_parsing_status

    if _cv_parsing_status["running"]:
        return {
            "success": False,
            "message": "CV-Parsing läuft bereits",
            "status": _cv_parsing_status,
        }

    # Anzahl zu parsender Kandidaten ermitteln
    count_result = await db.execute(
        select(func.count(Candidate.id)).where(
            Candidate.cv_url.isnot(None),
            Candidate.cv_url != "",
            Candidate.cv_parsed_at.is_(None),
        )
    )
    total_to_parse = count_result.scalar() or 0

    if total_to_parse == 0:
        return {"success": True, "message": "Keine CVs zum Parsen", "total": 0}

    # Background Task starten
    background_tasks.add_task(
        _run_cv_parsing, batch_size, min(total_to_parse, max_candidates)
    )

    return {
        "success": True,
        "message": f"CV-Parsing gestartet für {min(total_to_parse, max_candidates)} Kandidaten",
        "total_to_parse": total_to_parse,
        "max_candidates": max_candidates,
        "status_url": "/api/admin/parse-all-cvs/status",
    }


@router.get(
    "/parse-all-cvs/status",
    summary="CV-Parsing Status abfragen",
)
async def get_cv_parsing_status(db: AsyncSession = Depends(get_db)):
    """Gibt den aktuellen Status des CV-Parsing Background Tasks zurück,
    inklusive tatsächlicher DB-Werte."""
    from sqlalchemy import select, func

    from app.models.candidate import Candidate

    # Tatsächliche DB-Werte abfragen
    total_result = await db.execute(select(func.count(Candidate.id)))
    total_candidates = total_result.scalar() or 0

    parsed_result = await db.execute(
        select(func.count(Candidate.id)).where(
            Candidate.cv_parsed_at.isnot(None),
        )
    )
    parsed_in_db = parsed_result.scalar() or 0

    with_cv_url_result = await db.execute(
        select(func.count(Candidate.id)).where(
            Candidate.cv_url.isnot(None),
            Candidate.cv_url != "",
        )
    )
    with_cv_url = with_cv_url_result.scalar() or 0

    unparsed_result = await db.execute(
        select(func.count(Candidate.id)).where(
            Candidate.cv_url.isnot(None),
            Candidate.cv_url != "",
            Candidate.cv_parsed_at.is_(None),
        )
    )
    unparsed = unparsed_result.scalar() or 0

    return {
        **_cv_parsing_status,
        "db_stats": {
            "total_candidates": total_candidates,
            "parsed_in_db": parsed_in_db,
            "with_cv_url": with_cv_url,
            "unparsed_with_cv_url": unparsed,
        },
    }


async def _run_cv_parsing(batch_size: int, max_candidates: int):
    """Background Task für CV-Parsing. Verwendet eigene DB-Session."""
    from datetime import datetime, timezone

    from sqlalchemy import select, func

    from app.database import async_session_maker
    from app.models.candidate import Candidate
    from app.services.cv_parser_service import CVParserService

    global _cv_parsing_status

    _cv_parsing_status = {
        "running": True,
        "parsed": 0,
        "failed": 0,
        "total_to_parse": max_candidates,
        "total_tokens": 0,
        "errors": [],
        "recently_parsed": [],
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
        "current_candidate": None,
    }

    try:
        async with async_session_maker() as db:
            parser = CVParserService(db)

            offset = 0
            while offset < max_candidates:
                result = await db.execute(
                    select(Candidate)
                    .where(
                        Candidate.cv_url.isnot(None),
                        Candidate.cv_url != "",
                        Candidate.cv_parsed_at.is_(None),
                        Candidate.cv_parse_failed.is_(False),  # Überspringe fehlgeschlagene PDFs
                    )
                    .order_by(Candidate.id)
                    .limit(batch_size)
                )
                candidates = result.scalars().all()

                if not candidates:
                    break

                for candidate in candidates:
                    # Stop-Mechanismus prüfen
                    if _cv_parsing_status.get("stop_requested"):
                        logger.info("CV-Parsing wurde manuell gestoppt.")
                        _cv_parsing_status["running"] = False
                        _cv_parsing_status["finished_at"] = datetime.now(timezone.utc).isoformat()
                        _cv_parsing_status["current_candidate"] = None
                        await db.commit()
                        return

                    _cv_parsing_status["current_candidate"] = (
                        f"{candidate.first_name} {candidate.last_name}"
                    )
                    try:
                        pdf_bytes = await parser.download_cv(candidate.cv_url)
                        cv_text = parser.extract_text_from_pdf(pdf_bytes)
                        parse_result = await parser.parse_cv_text(cv_text)

                        if parse_result.success and parse_result.data:
                            await parser._update_candidate_from_cv(
                                candidate, parse_result.data, cv_text
                            )
                            _cv_parsing_status["total_tokens"] += parse_result.tokens_used
                            _cv_parsing_status["parsed"] += 1
                            _cv_parsing_status["recently_parsed"].append(
                                f"{candidate.first_name} {candidate.last_name}"
                            )
                            _cv_parsing_status["recently_parsed"] = _cv_parsing_status["recently_parsed"][-10:]
                        else:
                            # Als fehlgeschlagen markieren - wird übersprungen bis neuer CV hochgeladen
                            candidate.cv_parse_failed = True
                            _cv_parsing_status["failed"] += 1
                            _cv_parsing_status["errors"].append(
                                f"{candidate.first_name} {candidate.last_name}: {parse_result.error}"
                            )

                    except Exception as e:
                        # Als fehlgeschlagen markieren - wird übersprungen bis neuer CV hochgeladen
                        candidate.cv_parse_failed = True
                        _cv_parsing_status["failed"] += 1
                        _cv_parsing_status["errors"].append(
                            f"{candidate.first_name} {candidate.last_name}: {e}"
                        )

                    # Kurze Pause zwischen Kandidaten - gibt DB-Pool frei
                    await asyncio.sleep(0.3)

                await db.commit()
                offset += batch_size
                logger.info(
                    f"CV-Parsing: {_cv_parsing_status['parsed']} geparst, "
                    f"{_cv_parsing_status['failed']} fehlgeschlagen ({offset}/{max_candidates})"
                )
                # Pause zwischen Batches damit DB-Pool für API-Requests frei bleibt
                await asyncio.sleep(1.0)

            await parser.close()

    except Exception as e:
        logger.error(f"CV-Parsing Background Task fehlgeschlagen: {e}", exc_info=True)
        _cv_parsing_status["errors"].append(f"FATAL: {e}")

    _cv_parsing_status["running"] = False
    _cv_parsing_status["finished_at"] = datetime.now(timezone.utc).isoformat()
    _cv_parsing_status["current_candidate"] = None
    # Nur die letzten 50 Fehler behalten
    _cv_parsing_status["errors"] = _cv_parsing_status["errors"][-50:]

    logger.info(
        f"=== CV-PARSING FERTIG === {_cv_parsing_status['parsed']} geparst, "
        f"{_cv_parsing_status['failed']} fehlgeschlagen"
    )


@router.post(
    "/stop-cv-parsing",
    summary="Laufendes CV-Parsing stoppen",
)
async def stop_cv_parsing():
    """Stoppt das laufende CV-Parsing nach dem aktuellen Kandidaten."""
    global _cv_parsing_status
    if _cv_parsing_status.get("running"):
        _cv_parsing_status["stop_requested"] = True
        return {"success": True, "message": "Stop-Signal gesendet. Parsing wird nach aktuellem Kandidaten gestoppt."}
    return {"success": False, "message": "Kein CV-Parsing läuft aktuell."}


@router.post(
    "/restore-timestamps",
    summary="Timestamps für bereits geparste Kandidaten wiederherstellen",
)
async def restore_timestamps(db: AsyncSession = Depends(get_db)):
    """Setzt cv_parsed_at für alle Kandidaten, die bereits current_position haben aber kein cv_parsed_at."""
    from datetime import datetime, timezone

    from sqlalchemy import update, func

    from app.models.candidate import Candidate

    now = datetime.now(timezone.utc)
    result = await db.execute(
        update(Candidate)
        .where(
            Candidate.cv_parsed_at.is_(None),
            Candidate.current_position.isnot(None),
            Candidate.current_position != "",
        )
        .values(cv_parsed_at=now)
    )
    await db.commit()
    updated = result.rowcount

    return {
        "success": True,
        "message": f"Timestamps für {updated} Kandidaten wiederhergestellt.",
        "updated_count": updated,
    }


@router.post(
    "/reset-failed-cvs",
    summary="Fehlgeschlagene CV-Parsing-Einträge zurücksetzen",
)
async def reset_failed_cvs(db: AsyncSession = Depends(get_db)):
    """Setzt cv_parsed_at und cv_text für alle Kandidaten zurück, deren cv_text mit 'FEHLER:' beginnt."""
    from sqlalchemy import update

    from app.models.candidate import Candidate

    result = await db.execute(
        update(Candidate)
        .where(Candidate.cv_text.like("FEHLER:%"))
        .values(cv_parsed_at=None, cv_text=None)
    )
    await db.commit()

    return {
        "success": True,
        "reset_count": result.rowcount,
    }


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
                        manual = existing.manual_overrides or {}
                        for key, value in mapped.items():
                            if key != "crm_id" and value is not None:
                                if key in manual:
                                    continue
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
    "/migrate-columns",
    summary="DB-Migration: Spalten vergrößern für CRM-Daten",
)
async def migrate_columns(db: AsyncSession = Depends(get_db)):
    """Ändert zu kurze VARCHAR-Spalten auf TEXT oder größere Werte."""
    from sqlalchemy import text

    migrations = [
        ("cv_url", "ALTER TABLE candidates ALTER COLUMN cv_url TYPE TEXT"),
        ("street_address", "ALTER TABLE candidates ALTER COLUMN street_address TYPE TEXT"),
        ("postal_code", "ALTER TABLE candidates ALTER COLUMN postal_code TYPE VARCHAR(50)"),
        ("city", "ALTER TABLE candidates ALTER COLUMN city TYPE VARCHAR(255)"),
        ("current_position", "ALTER TABLE candidates ALTER COLUMN current_position TYPE TEXT"),
        ("current_company", "ALTER TABLE candidates ALTER COLUMN current_company TYPE TEXT"),
        ("phone", "ALTER TABLE candidates ALTER COLUMN phone TYPE VARCHAR(100)"),
        ("email", "ALTER TABLE candidates ALTER COLUMN email TYPE VARCHAR(500)"),
        ("crm_id", "ALTER TABLE candidates ALTER COLUMN crm_id TYPE VARCHAR(255)"),
    ]

    results = []
    for name, sql in migrations:
        try:
            await db.execute(text(sql))
            results.append({"column": name, "status": "ok"})
        except Exception as e:
            results.append({"column": name, "status": "error", "error": str(e)})

    await db.commit()
    return {"success": True, "migrations": results}


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
    auto_pipeline: bool = Query(
        default=True,
        description="Auto-Pipeline nach Sync ausfuehren (Kategorisierung → Klassifizierung → Stale-Markierung)"
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
    - auto_pipeline=True: Nach Sync automatisch Kategorisierung → Klassifizierung → Stale-Markierung
    """
    job_runner = JobRunnerService(db)

    if await job_runner.is_running(JobType.CRM_SYNC):
        raise ConflictException(message="CRM-Sync läuft bereits")

    job_source = JobSource.CRON if source == "cron" else JobSource.MANUAL
    job_run = await job_runner.start_job(JobType.CRM_SYNC, job_source)

    background_tasks.add_task(_run_crm_sync, db, job_run.id, full_sync, parse_cvs, auto_pipeline)

    pipeline_msg = " + Auto-Pipeline" if auto_pipeline else ""
    return JobTriggerResponse(
        message=f"CRM-Sync gestartet{' mit CV-Parsing' if parse_cvs else ''}{pipeline_msg}",
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

            # Ergebnis ist ein dict mit "jobs" und "candidates" Sub-Dicts
            jobs_r = result.get("jobs", {})
            cands_r = result.get("candidates", {})

            await job_runner.complete_job(
                job_run_id,
                items_total=jobs_r.get("total", 0) + cands_r.get("total", 0),
                items_successful=jobs_r.get("successful", 0) + cands_r.get("successful", 0),
                items_failed=jobs_r.get("failed", 0) + cands_r.get("failed", 0),
            )
            await db.commit()
        except Exception as e:
            logger.error(f"Geocoding fehlgeschlagen: {e}")
            await job_runner.fail_job(job_run_id, str(e))
            await db.commit()


async def _run_crm_sync(
    db_unused: AsyncSession,
    job_run_id: UUID,
    full_sync: bool,
    parse_cvs: bool = True,
    auto_pipeline: bool = True,
):
    """Führt CRM-Sync im Hintergrund aus.

    Importiert Kandidaten direkt aus der Recruit CRM API in die Datenbank.
    Verwendet den bewährten direkten Import-Ansatz (ohne CRMSyncService),
    da dieser nachweislich funktioniert.

    Args:
        db_unused: Nicht verwenden - wird aus Kompatibilitätsgründen beibehalten
        job_run_id: ID des Job-Runs für Progress-Tracking
        full_sync: True für Initial-Sync (alle Kandidaten)
        parse_cvs: Aktuell ignoriert (CV-Parsing wird separat implementiert)
        auto_pipeline: True = nach Sync automatisch Pipeline ausfuehren
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
                            # Gelöschter Kandidat → komplett überspringen
                            if existing.deleted_at is not None:
                                logger.debug(f"Kandidat {crm_id} ist gelöscht, überspringe Sync")
                                total_processed += 1
                                continue

                            # Update vorhandenen Kandidaten
                            # Manuell bearbeitete Felder nicht ueberschreiben
                            manual = existing.manual_overrides or {}
                            for key, value in mapped.items():
                                if key != "crm_id" and value is not None:
                                    if key in manual:
                                        continue
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

            # === AUTO CV-PARSING nach CRM-Sync ===
            # Alle ungeparsten CVs sofort parsen (finaler Prompt aus cv_parser_service.py)
            from app.services.cv_parser_service import CVParserService

            unparsed_result = await db.execute(
                select(Candidate)
                .where(
                    Candidate.cv_url.isnot(None),
                    Candidate.cv_url != "",
                    Candidate.cv_parsed_at.is_(None),
                    Candidate.cv_parse_failed.is_(False),
                )
                .order_by(Candidate.id)
            )
            unparsed_candidates = unparsed_result.scalars().all()

            if unparsed_candidates:
                logger.info(f"=== AUTO CV-PARSING START === {len(unparsed_candidates)} ungeparste CVs")
                parser = CVParserService(db)
                cv_parsed = 0
                cv_failed = 0

                for candidate in unparsed_candidates:
                    try:
                        pdf_bytes = await parser.download_cv(candidate.cv_url)
                        cv_text = parser.extract_text_from_pdf(pdf_bytes)
                        parse_result = await parser.parse_cv_text(cv_text)

                        if parse_result.success and parse_result.data:
                            await parser._update_candidate_from_cv(
                                candidate, parse_result.data, cv_text
                            )
                            cv_parsed += 1
                        else:
                            candidate.cv_parse_failed = True
                            cv_failed += 1
                            logger.warning(
                                f"CV-Parsing fehlgeschlagen: {candidate.first_name} {candidate.last_name}: {parse_result.error}"
                            )

                    except Exception as e:
                        candidate.cv_parse_failed = True
                        cv_failed += 1
                        logger.warning(
                            f"CV-Parsing Fehler: {candidate.first_name} {candidate.last_name}: {e}"
                        )

                    # Sofort nach jedem Kandidaten committen
                    await db.commit()
                    await asyncio.sleep(0.3)

                await parser.close()
                logger.info(
                    f"=== AUTO CV-PARSING FERTIG === {cv_parsed} geparst, {cv_failed} fehlgeschlagen"
                )
            else:
                logger.info("=== AUTO CV-PARSING === Keine ungeparsten CVs vorhanden")

            # === AUTO PIPELINE nach CRM-Sync + CV-Parsing ===
            if auto_pipeline:
                try:
                    logger.info("=== AUTO PIPELINE START ===")
                    from app.services.pipeline_service import PipelineService

                    pipeline = PipelineService(db)
                    pipeline_result = await pipeline.run_auto_pipeline()
                    logger.info(f"=== AUTO PIPELINE FERTIG === {pipeline_result}")
                except Exception as pipeline_error:
                    logger.error(f"Auto-Pipeline fehlgeschlagen: {pipeline_error}", exc_info=True)
                    # Pipeline-Fehler soll CRM-Sync nicht als fehlgeschlagen markieren
            else:
                logger.info("=== AUTO PIPELINE === Deaktiviert (auto_pipeline=False)")

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


# ==================== MT Neustart: Cleanup for Restart ====================


@router.post("/cleanup-for-restart")
async def cleanup_for_restart(
    dry_run: bool = Query(default=True, description="True = nur zaehlen, nicht loeschen"),
    archive_matches: bool = Query(default=True, description="AI-bewertete Matches vorher archivieren"),
    db: AsyncSession = Depends(get_db),
):
    """Loescht alle Matches und Jobs fuer einen sauberen Neustart.

    - Matches mit AI-Scores werden vorher in mt_training_data archiviert
    - Jobs werden soft-deleted (deleted_at = now())
    - Kandidaten + Embeddings bleiben erhalten
    - Lern-Daten bleiben erhalten
    """
    from sqlalchemy import delete, func, select, update

    from app.models.match import Match
    from app.models.job import Job

    # Zaehlen
    matches_count = (await db.execute(select(func.count(Match.id)))).scalar() or 0
    jobs_count = (await db.execute(
        select(func.count(Job.id)).where(Job.deleted_at.is_(None))
    )).scalar() or 0

    # AI-bewertete Matches zaehlen (die archiviert werden sollen)
    ai_matches_count = (await db.execute(
        select(func.count(Match.id)).where(Match.ai_score.isnot(None))
    )).scalar() or 0

    if dry_run:
        return {
            "dry_run": True,
            "matches_to_delete": matches_count,
            "ai_matches_to_archive": ai_matches_count,
            "jobs_to_soft_delete": jobs_count,
            "candidates_kept": "alle",
            "embeddings_kept": "alle",
        }

    archived = 0

    # Schritt 1: AI-bewertete Matches in mt_training_data archivieren
    if archive_matches and ai_matches_count > 0:
        from sqlalchemy import text

        # Archiviere Match-Daten als Lern-Eintraege
        result = await db.execute(
            select(Match).where(Match.ai_score.isnot(None))
        )
        ai_matches = result.scalars().all()

        for m in ai_matches:
            await db.execute(text("""
                INSERT INTO mt_training_data (entity_type, entity_id, input_text, assigned_titles, predicted_titles, was_correct, reasoning, created_at)
                VALUES ('match_archive', :entity_id, :input_text, :assigned_titles, :predicted_titles, :was_correct, :reasoning, NOW())
            """), {
                "entity_id": m.id,
                "input_text": f"Match: Job={m.job_id} ↔ Kandidat={m.candidate_id}, Score={m.ai_score}",
                "assigned_titles": "[]",
                "predicted_titles": f'["{m.ai_score}"]',
                "was_correct": m.user_feedback == "good" if m.user_feedback else None,
                "reasoning": m.ai_explanation,
            })
            archived += 1

    # Schritt 2: Alle Matches loeschen
    await db.execute(delete(Match))

    # Schritt 3: Alle Jobs soft-deleten
    await db.execute(
        update(Job)
        .where(Job.deleted_at.is_(None))
        .values(deleted_at=func.now())
    )

    await db.commit()

    return {
        "dry_run": False,
        "matches_deleted": matches_count,
        "ai_matches_archived": archived,
        "jobs_soft_deleted": jobs_count,
        "candidates_kept": "alle",
        "embeddings_kept": "alle",
        "status": "Cleanup abgeschlossen. Bereit fuer Neustart.",
    }


# ==================== Migrations On-Demand ====================


@router.post("/run-migrations")
async def run_migrations():
    """Fuehrt ausstehende Alembic-Migrationen aus (on-demand)."""
    import subprocess

    try:
        result = subprocess.run(
            ["alembic", "upgrade", "head"],
            capture_output=True,
            text=True,
            timeout=60,
        )

        return {
            "success": result.returncode == 0,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
        }
