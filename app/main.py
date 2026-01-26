"""FastAPI Hauptanwendung für das Matching-Tool."""

# Build: 2026-01-26-v3 - CRM-Sync Progress Tracking Fix

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup und Shutdown Events."""
    # Startup
    logger.info("Starte Matching-Tool...")
    try:
        await init_db()
        logger.info("Datenbankverbindung erfolgreich hergestellt")
    except Exception as e:
        logger.error(f"Datenbankverbindung fehlgeschlagen: {e}")
        raise

    yield

    # Shutdown
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


# Templates konfigurieren
templates = Jinja2Templates(directory="app/templates")


# Health-Check Endpoint
@app.get("/health", tags=["System"])
async def health_check():
    """Prüft, ob die Anwendung läuft."""
    return {
        "status": "healthy",
        "version": "1.0.0",
        "environment": settings.environment,
    }


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

# Custom Exception Handlers registrieren
register_exception_handlers(app)

# Page Router registrieren (ohne Prefix fuer HTML-Seiten)
app.include_router(pages_router)

# API Router registrieren (alle mit /api Prefix)
app.include_router(jobs_router, prefix="/api")
app.include_router(candidates_router, prefix="/api")
app.include_router(matches_router, prefix="/api")
app.include_router(filters_router, prefix="/api")
app.include_router(settings_router, prefix="/api")
app.include_router(admin_router, prefix="/api")
app.include_router(statistics_router, prefix="/api")
app.include_router(alerts_router, prefix="/api")
