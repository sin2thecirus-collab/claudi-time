"""FastAPI Hauptanwendung für das Matching-Tool."""

# Build: 2026-02-09-v1 - Matching Engine v2.5 + Background Migrations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.database import init_db

# Logging konfigurieren
logging.basicConfig(
    level=logging.DEBUG if settings.is_development else logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Globaler Status fuer DB-Migrationen
_db_ready = False
_db_migration_task: asyncio.Task | None = None


async def _run_migrations():
    """Fuehrt DB-Migrationen im Hintergrund aus."""
    global _db_ready
    try:
        await init_db()
        _db_ready = True
        logger.info("Datenbankverbindung + Migrationen erfolgreich abgeschlossen")
    except Exception as e:
        _db_ready = True  # App trotzdem als ready markieren
        logger.warning(f"DB-Migration teilweise fehlgeschlagen (App laeuft trotzdem): {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup und Shutdown Events."""
    global _db_migration_task
    # Startup: Migrationen im Hintergrund starten
    logger.info("Starte Matching-Tool...")
    _db_migration_task = asyncio.create_task(_run_migrations())

    yield

    # Shutdown: Migration-Task abbrechen falls noch laeuft
    if _db_migration_task and not _db_migration_task.done():
        _db_migration_task.cancel()
    logger.info("Beende Matching-Tool...")


# FastAPI App initialisieren
app = FastAPI(
    title="Matching-Tool",
    description="Matching-Tool für Recruiter - Verbindet Jobs mit passenden Kandidaten",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.is_development else None,
    redoc_url="/redoc" if settings.is_development else None,
)

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.is_development else [],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Request-ID Middleware
@app.middleware("http")
async def add_request_id(request: Request, call_next):
    """Fügt eine eindeutige Request-ID zu jedem Request hinzu."""
    request_id = str(uuid.uuid4())
    request.state.request_id = request_id

    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id

    return response


# Static Files konfigurieren
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# Templates konfigurieren
templates = Jinja2Templates(directory="app/templates")


# Health-Check Endpoint
@app.get("/health", tags=["System"])
async def health_check():
    """Prüft, ob die Anwendung läuft. Antwortet sofort (DB-Migrationen laufen im Hintergrund)."""
    return {
        "status": "healthy",
        "version": "1.0.0",
        "environment": settings.environment,
        "db_ready": _db_ready,
    }


@app.post("/admin/reset-pool", tags=["System"])
async def reset_db_pool():
    """Setzt den DB-Connection-Pool zurueck und killt haengende Verbindungen."""
    from app.database import engine
    from sqlalchemy import text

    try:
        # 1. Pool komplett zuruecksetzen (alle idle Connections schliessen)
        await engine.dispose()

        # 2. Haengende Connections auf DB-Ebene killen
        async with engine.begin() as conn:
            result = await conn.execute(text("""
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE state = 'idle in transaction'
                  AND pid != pg_backend_pid()
                  AND query_start < NOW() - INTERVAL '30 seconds'
            """))
            killed = result.fetchall()

        return {
            "status": "ok",
            "pool_disposed": True,
            "idle_transactions_killed": len(killed),
        }
    except Exception as e:
        return {"status": "error", "detail": str(e)}


# Root wird jetzt vom Pages Router gehandhabt (Dashboard)


# Exception Handler
@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    """Generischer Exception Handler."""
    logger.error(f"Unbehandelter Fehler: {exc}", exc_info=True)

    return JSONResponse(
        status_code=500,
        content={
            "error": "internal_server_error",
            "message": "Ein interner Fehler ist aufgetreten.",
            "request_id": getattr(request.state, "request_id", None),
        },
    )


# API Router einbinden
from app.api import register_exception_handlers
from app.api.routes_jobs import router as jobs_router
from app.api.routes_candidates import router as candidates_router
from app.api.routes_matches import router as matches_router
from app.api.routes_filters import router as filters_router
from app.api.routes_settings import router as settings_router
from app.api.routes_admin import router as admin_router
from app.api.routes_pages import router as pages_router
from app.api.routes_statistics import router as statistics_router
from app.api.routes_alerts import router as alerts_router
from app.api.routes_hotlisten import router as hotlisten_router
from app.api.routes_match_center import router as match_center_router
from app.api.routes_companies import router as companies_router
from app.api.routes_smart_match import router as smart_match_router
from app.api.routes_titel_zuweisung import router as titel_zuweisung_router
from app.api.routes_matching_v2 import router as matching_v2_router
from app.api.routes_status import router as status_router

# ATS Router
from app.api.routes_ats_jobs import router as ats_jobs_router
from app.api.routes_ats_pipeline import router as ats_pipeline_router
from app.api.routes_ats_call_notes import router as ats_call_notes_router
from app.api.routes_ats_todos import router as ats_todos_router
from app.api.routes_ats_pages import router as ats_pages_router
from app.api.routes_n8n_webhooks import router as n8n_webhooks_router

# Custom Exception Handlers registrieren
register_exception_handlers(app)

# Page Router registrieren (ohne Prefix fuer HTML-Seiten)
app.include_router(pages_router)
app.include_router(hotlisten_router)  # Hotlisten-Seiten + API (/hotlisten, /match-bereiche, /deepmatch)
app.include_router(match_center_router)  # Match Center (/match-center, /api/match-center)
app.include_router(titel_zuweisung_router)  # Titel-Zuweisung (/titel-zuweisung, /api/titel-zuweisung)
app.include_router(ats_pages_router)  # ATS Seiten (/ats, /ats/stellen, /ats/todos, /ats/anrufe)

# API Router registrieren (alle mit /api Prefix)
app.include_router(jobs_router, prefix="/api")
app.include_router(candidates_router, prefix="/api")
app.include_router(matches_router, prefix="/api")
app.include_router(filters_router, prefix="/api")
app.include_router(settings_router, prefix="/api")
app.include_router(admin_router, prefix="/api")
app.include_router(statistics_router, prefix="/api")
app.include_router(alerts_router, prefix="/api")
app.include_router(companies_router, prefix="/api")
app.include_router(smart_match_router)  # Smart-Match API (/api/smart-match/...)

# ATS API Router (alle mit /api Prefix)
app.include_router(ats_jobs_router, prefix="/api")
app.include_router(ats_pipeline_router, prefix="/api")
app.include_router(ats_call_notes_router, prefix="/api")
app.include_router(ats_todos_router, prefix="/api")
app.include_router(n8n_webhooks_router, prefix="/api")  # n8n Webhooks (/api/n8n/...)

# Matching Engine v2 (/api/v2/profiles/..., /api/v2/weights, /api/v2/rules)
app.include_router(matching_v2_router, prefix="/api/v2")

# Status & Query API (/api/status/overview, /api/status/geodaten, /api/status/profiling, ...)
app.include_router(status_router, prefix="/api")
