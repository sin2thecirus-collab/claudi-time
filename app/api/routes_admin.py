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


# Globaler Status fuer R2-CV-Migration Background Task
_r2_migration_status: dict = {
    "running": False,
    "migrated": 0,
    "failed": 0,
    "skipped": 0,
    "total_to_migrate": 0,
    "errors": [],
    "recently_migrated": [],
    "started_at": None,
    "finished_at": None,
    "current_candidate": None,
}


@router.post(
    "/migrate-all-cvs-to-r2",
    summary="Alle CVs nach R2 migrieren (Background Task)",
)
async def migrate_all_cvs_to_r2(
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    batch_size: int = Query(default=20, description="Kandidaten pro Batch"),
    max_candidates: int = Query(default=5000, description="Maximale Anzahl"),
):
    """
    Startet die vollstaendige CV-Migration nach R2 als Background Task.
    Holt PDFs von CRM-URLs und laedt sie nach R2 hoch.
    Dateinamen werden automatisch mit Kandidatennamen versehen.
    Status ueber GET /api/admin/migrate-all-cvs-to-r2/status abrufen.
    """
    from sqlalchemy import select, func
    from app.models.candidate import Candidate

    global _r2_migration_status

    if _r2_migration_status["running"]:
        return {
            "success": False,
            "message": "R2-Migration laeuft bereits",
            "status": _r2_migration_status,
        }

    # Anzahl zu migrierender Kandidaten ermitteln
    count_result = await db.execute(
        select(func.count(Candidate.id)).where(
            Candidate.cv_url.isnot(None),
            Candidate.cv_url != "",
            Candidate.cv_stored_path.is_(None),
            Candidate.deleted_at.is_(None),
        )
    )
    total_to_migrate = count_result.scalar() or 0

    if total_to_migrate == 0:
        return {"success": True, "message": "Keine CVs zum Migrieren", "total": 0}

    # Background Task starten
    background_tasks.add_task(
        _run_r2_migration, batch_size, min(total_to_migrate, max_candidates)
    )

    return {
        "success": True,
        "message": f"R2-Migration gestartet fuer {min(total_to_migrate, max_candidates)} Kandidaten",
        "total_to_migrate": total_to_migrate,
        "max_candidates": max_candidates,
        "status_url": "/api/admin/migrate-all-cvs-to-r2/status",
    }


@router.get(
    "/migrate-all-cvs-to-r2/status",
    summary="R2-Migration Status abfragen",
)
async def get_r2_migration_status(db: AsyncSession = Depends(get_db)):
    """Gibt den aktuellen Status der R2-CV-Migration zurueck."""
    from sqlalchemy import select, func
    from app.models.candidate import Candidate

    # Tatsaechliche DB-Werte abfragen
    in_r2_result = await db.execute(
        select(func.count(Candidate.id)).where(
            Candidate.cv_stored_path.isnot(None),
            Candidate.deleted_at.is_(None),
        )
    )
    in_r2 = in_r2_result.scalar() or 0

    remaining_result = await db.execute(
        select(func.count(Candidate.id)).where(
            Candidate.cv_url.isnot(None),
            Candidate.cv_url != "",
            Candidate.cv_stored_path.is_(None),
            Candidate.deleted_at.is_(None),
        )
    )
    remaining = remaining_result.scalar() or 0

    total_with_cv_result = await db.execute(
        select(func.count(Candidate.id)).where(
            Candidate.cv_url.isnot(None),
            Candidate.deleted_at.is_(None),
        )
    )
    total_with_cv = total_with_cv_result.scalar() or 0

    return {
        "status": _r2_migration_status,
        "db": {
            "total_with_cv": total_with_cv,
            "in_r2": in_r2,
            "remaining": remaining,
            "percent_complete": round((in_r2 / total_with_cv * 100), 1) if total_with_cv > 0 else 0,
        },
    }


@router.post(
    "/stop-r2-migration",
    summary="Laufende R2-Migration stoppen",
)
async def stop_r2_migration():
    """Stoppt die laufende R2-Migration nach dem aktuellen Kandidaten."""
    global _r2_migration_status
    if _r2_migration_status.get("running"):
        _r2_migration_status["stop_requested"] = True
        return {"success": True, "message": "Stop-Signal gesendet. Migration wird nach aktuellem Kandidaten gestoppt."}
    return {"success": False, "message": "Keine R2-Migration laeuft aktuell."}


@router.post(
    "/reset-r2-migration-status",
    summary="R2-Migration Status zuruecksetzen (nach Neustart/Crash)",
)
async def reset_r2_migration_status():
    """Setzt den globalen R2-Migration-Status zurueck (falls nach Neustart haengengeblieben)."""
    global _r2_migration_status
    _r2_migration_status = {
        "running": False,
        "migrated": 0,
        "failed": 0,
        "skipped": 0,
        "total_to_migrate": 0,
        "errors": [],
        "recently_migrated": [],
        "started_at": None,
        "finished_at": None,
        "current_candidate": None,
    }
    return {"success": True, "message": "R2-Migration-Status zurueckgesetzt"}


async def _run_r2_migration(batch_size: int, max_candidates: int):
    """Background Task: Migriert CVs von CRM-URLs nach R2 Storage."""
    import httpx
    from datetime import datetime, timezone
    from sqlalchemy import select
    from app.database import async_session_maker
    from app.models.candidate import Candidate
    from app.services.r2_storage_service import R2StorageService

    global _r2_migration_status

    _r2_migration_status = {
        "running": True,
        "migrated": 0,
        "failed": 0,
        "skipped": 0,
        "total_to_migrate": max_candidates,
        "errors": [],
        "recently_migrated": [],
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
        "current_candidate": None,
    }

    try:
        r2 = R2StorageService()
        if not r2.is_available:
            _r2_migration_status["running"] = False
            _r2_migration_status["errors"].append("R2 Storage nicht konfiguriert")
            return

        # EIN httpx Client fuer alle Downloads (Connection-Pool)
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(20.0, connect=10.0),
            limits=httpx.Limits(max_connections=5, max_keepalive_connections=3),
        ) as http_client:

            async with async_session_maker() as db:
                processed = 0
                while processed < max_candidates:
                    result = await db.execute(
                        select(Candidate)
                        .where(
                            Candidate.cv_url.isnot(None),
                            Candidate.cv_url != "",
                            Candidate.cv_stored_path.is_(None),
                            Candidate.deleted_at.is_(None),
                        )
                        .order_by(Candidate.id)
                        .limit(batch_size)
                    )
                    candidates = result.scalars().all()

                    if not candidates:
                        break

                    for candidate in candidates:
                        # Stop-Mechanismus pruefen
                        if _r2_migration_status.get("stop_requested"):
                            logger.info("R2-Migration wurde manuell gestoppt.")
                            _r2_migration_status["running"] = False
                            _r2_migration_status["finished_at"] = datetime.now(timezone.utc).isoformat()
                            _r2_migration_status["current_candidate"] = None
                            await db.commit()
                            return

                        name = f"{candidate.first_name or ''} {candidate.last_name or ''}".strip() or "Unbekannt"
                        _r2_migration_status["current_candidate"] = name
                        processed += 1

                        try:
                            # CV von CRM-URL herunterladen (20s Timeout)
                            response = await http_client.get(candidate.cv_url)

                            if response.status_code != 200:
                                _r2_migration_status["failed"] += 1
                                _r2_migration_status["errors"].append(
                                    f"{name}: HTTP {response.status_code}"
                                )
                                continue

                            if len(response.content) < 100:
                                _r2_migration_status["skipped"] += 1
                                _r2_migration_status["errors"].append(
                                    f"{name}: Datei zu klein ({len(response.content)} Bytes)"
                                )
                                continue

                            # Nach R2 hochladen (Dateiname = Kandidatenname)
                            key = r2.upload_cv(
                                str(candidate.id),
                                response.content,
                                first_name=candidate.first_name,
                                last_name=candidate.last_name,
                                hotlist_category=candidate.hotlist_category,
                            )
                            candidate.cv_stored_path = key
                            _r2_migration_status["migrated"] += 1
                            _r2_migration_status["recently_migrated"].append(
                                f"{name} -> {key}"
                            )
                            _r2_migration_status["recently_migrated"] = _r2_migration_status["recently_migrated"][-10:]

                        except httpx.TimeoutException:
                            _r2_migration_status["failed"] += 1
                            _r2_migration_status["errors"].append(f"{name}: Timeout (20s)")
                        except Exception as e:
                            _r2_migration_status["failed"] += 1
                            _r2_migration_status["errors"].append(f"{name}: {e}")

                        # Kurze Pause - gibt DB-Pool und Netzwerk frei
                        await asyncio.sleep(0.3)

                    await db.commit()
                    logger.info(
                        f"R2-Migration: {_r2_migration_status['migrated']} migriert, "
                        f"{_r2_migration_status['failed']} fehlgeschlagen ({processed}/{max_candidates})"
                    )
                    # Pause zwischen Batches
                    await asyncio.sleep(0.5)

    except Exception as e:
        logger.error(f"R2-Migration Background Task fehlgeschlagen: {e}", exc_info=True)
        _r2_migration_status["errors"].append(f"FATAL: {e}")

    _r2_migration_status["running"] = False
    _r2_migration_status["finished_at"] = datetime.now(timezone.utc).isoformat()
    _r2_migration_status["current_candidate"] = None
    # Nur die letzten 50 Fehler behalten
    _r2_migration_status["errors"] = _r2_migration_status["errors"][-50:]

    logger.info(
        f"=== R2-MIGRATION FERTIG === {_r2_migration_status['migrated']} migriert, "
        f"{_r2_migration_status['failed']} fehlgeschlagen, "
        f"{_r2_migration_status['skipped']} uebersprungen"
    )


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


async def _run_matching(db_unused: AsyncSession, job_run_id: UUID):
    """Führt Matching im Hintergrund aus — Pipeline V3.

    WICHTIG: db_unused wird nicht verwendet! Background Tasks müssen ihre eigene
    DB-Session erstellen, da die Request-Session bereits geschlossen sein könnte.
    """
    from app.database import async_session_maker
    from app.services.matching_pipeline_v3 import MatchingPipelineV3

    # Eigene DB-Session für Background Task erstellen
    async with async_session_maker() as db:
        job_runner = JobRunnerService(db)

        try:
            async with MatchingPipelineV3(db) as pipeline:
                result = await pipeline.run_all(skip_already_matched=False)

            await job_runner.complete_job(
                job_run_id,
                items_total=result.get("total_jobs", 0),
                items_successful=result.get("total_matches_created", 0),
                items_failed=result.get("jobs_failed", 0),
            )
            await db.commit()
        except Exception as e:
            logger.error(f"V3-Matching fehlgeschlagen: {e}", exc_info=True)
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


@router.get("/migration-status")
async def get_migration_status(
    db: AsyncSession = Depends(get_db),
):
    """Zeigt den aktuellen Migrationsstatus."""
    from sqlalchemy import text

    try:
        result = await db.execute(
            text("SELECT version_num FROM alembic_version")
        )
        current = result.scalar_one_or_none()

        # Pruefen ob Spalten existieren
        deleted_at_check = await db.execute(
            text("SELECT column_name FROM information_schema.columns WHERE table_name = 'ats_jobs' AND column_name = 'deleted_at'")
        )
        has_deleted_at = deleted_at_check.fetchone() is not None

        source_job_check = await db.execute(
            text("SELECT column_name FROM information_schema.columns WHERE table_name = 'ats_jobs' AND column_name = 'source_job_id'")
        )
        has_source_job_id = source_job_check.fetchone() is not None

        return {
            "alembic_version": current,
            "has_deleted_at": has_deleted_at,
            "has_source_job_id": has_source_job_id,
            "migration_011_complete": has_deleted_at and has_source_job_id,
        }
    except Exception as e:
        return {
            "error": str(e),
        }


@router.post("/fix-migration-011")
async def fix_migration_011(
    db: AsyncSession = Depends(get_db),
):
    """Fuegt fehlende Spalte deleted_at manuell hinzu falls Migration 011 unvollstaendig war."""
    from sqlalchemy import text

    try:
        # Pruefen ob deleted_at Spalte fehlt
        check = await db.execute(
            text("SELECT column_name FROM information_schema.columns WHERE table_name = 'ats_jobs' AND column_name = 'deleted_at'")
        )
        if check.fetchone() is not None:
            return {"status": "skipped", "message": "deleted_at Spalte existiert bereits"}

        # Spalte hinzufuegen
        await db.execute(
            text("ALTER TABLE ats_jobs ADD COLUMN deleted_at TIMESTAMP WITH TIME ZONE")
        )
        await db.commit()

        # Index erstellen falls nicht existiert
        await db.execute(
            text("CREATE INDEX IF NOT EXISTS ix_ats_jobs_deleted_at ON ats_jobs (deleted_at)")
        )
        await db.commit()

        return {"status": "success", "message": "deleted_at Spalte und Index hinzugefuegt"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.post("/run-migrations")
async def run_migrations(
    db: AsyncSession = Depends(get_db),
):
    """Fuehrt ausstehende Alembic-Migrationen aus (on-demand).

    Repariert vorher die alembic_version Tabelle falls noetig
    (001_initial -> 001 Umbenennung).
    """
    import subprocess
    from sqlalchemy import text

    # Fix: alembic_version Tabelle reparieren
    # Die DB hat alle Spalten bis 009, aber alembic_version war kaputt.
    # Setze auf 009 damit nur neue Migrationen (010+) laufen.
    try:
        result = await db.execute(
            text("SELECT version_num FROM alembic_version")
        )
        current = result.scalar_one_or_none()
        if current in ("001_initial", "001"):
            await db.execute(
                text("UPDATE alembic_version SET version_num = '009' WHERE version_num = :old"),
                {"old": current},
            )
            await db.commit()
    except Exception:
        pass  # Tabelle existiert nicht oder anderer Fehler

    # Dann Migrationen ausfuehren
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
