"""FastAPI Hauptanwendung für das Matching-Tool."""

# Build: 2026-02-09-v1 - Matching Engine v2.5 + Background Migrations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from app.config import settings
from app.database import engine, init_db
from app.auth import (
    AuthMiddleware,
    SecurityHeadersMiddleware,
    JWT_COOKIE_NAME,
    CSRF_COOKIE_NAME,
    check_login_rate_limit,
    create_access_token,
    generate_csrf_token,
    record_login_attempt,
    verify_password,
    hash_password,
    _get_client_ip,
)

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
    logger.info("Starte Matching-Tool...")

    # WICHTIG: Users-Tabelle + Admin SYNCHRON erstellen,
    # damit Login sofort nach dem Health-Check funktioniert
    try:
        from app.database import _ensure_users_table
        async with engine.begin() as conn:
            await conn.run_sync(lambda _: None)  # DB-Verbindung testen
        await _ensure_users_table()
        logger.info("Users-Tabelle + Admin-User bereit.")
    except Exception as e:
        logger.error(f"KRITISCH: Users-Tabelle konnte nicht erstellt werden: {e}")

    # Restliche Migrationen im Hintergrund starten
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

# ── Middleware-Stack (Reihenfolge: zuletzt registriert = zuerst ausgefuehrt) ──
# 1. CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.is_development else [],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 2. Auth-Middleware (prueft JWT Cookie oder API-Key)
app.add_middleware(AuthMiddleware)

# 3. Security Headers (X-Frame-Options, HSTS, etc.)
app.add_middleware(SecurityHeadersMiddleware)


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

# Jinja2-Filter: UTC → deutsche Zeit (Europe/Berlin)
from zoneinfo import ZoneInfo

def to_berlin(dt_value):
    """Konvertiert UTC datetime nach Europe/Berlin (MEZ/MESZ)."""
    if dt_value is None:
        return dt_value
    berlin = ZoneInfo("Europe/Berlin")
    if dt_value.tzinfo is None:
        from datetime import timezone
        dt_value = dt_value.replace(tzinfo=timezone.utc)
    return dt_value.astimezone(berlin)

templates.env.filters["to_berlin"] = to_berlin


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


# ── TEMPORÄR: Debug-Endpoint zum Prüfen des Admin-Users (wird nach Fix entfernt) ──
@app.get("/auth-debug", tags=["System"])
async def auth_debug():
    """Zeigt Debug-Info zum Auth-System (TEMPORÄR — wird nach Login-Fix entfernt)."""
    import os
    info = {
        # Direkt aus os.environ lesen (umgeht pydantic)
        "raw_env_ADMIN_EMAIL": os.environ.get("ADMIN_EMAIL", "(nicht gesetzt)"),
        "raw_env_ADMIN_PASSWORD_len": len(os.environ.get("ADMIN_PASSWORD", "")),
        "raw_env_API_ACCESS_KEY_set": bool(os.environ.get("API_ACCESS_KEY")),
        # Alle ENV-Keys die "ADMIN" oder "admin" enthalten
        "env_keys_with_admin": [k for k in os.environ.keys() if "admin" in k.lower()],
        # Pydantic Settings
        "pydantic_admin_email": settings.admin_email if settings.admin_email else "(leer)",
        "pydantic_admin_password_length": len(settings.admin_password) if settings.admin_password else 0,
        "users_in_db": 0,
        "admin_user_found": False,
        "admin_email_in_db": None,
        "password_verify_test": None,
    }
    try:
        async with engine.begin() as conn:
            # Pruefen ob users-Tabelle existiert
            result = await conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name = 'users'"
                )
            )
            info["users_table_exists"] = result.fetchone() is not None

            if info["users_table_exists"]:
                # Alle User zaehlen
                result = await conn.execute(text("SELECT COUNT(*) FROM users"))
                info["users_in_db"] = result.scalar()

                # Admin-User suchen
                result = await conn.execute(
                    text("SELECT email, hashed_password, role FROM users LIMIT 5")
                )
                users = result.fetchall()
                if users:
                    info["admin_user_found"] = True
                    info["admin_email_in_db"] = users[0][0]
                    info["admin_role"] = users[0][2]
                    # Passwort-Verify testen
                    if settings.admin_password:
                        info["password_verify_test"] = verify_password(
                            settings.admin_password, users[0][1]
                        )
    except Exception as e:
        info["db_error"] = str(e)

    # Wenn kein Admin-User existiert, versuche ihn JETZT zu erstellen
    if info["users_in_db"] == 0 and settings.admin_email and settings.admin_password:
        try:
            admin_email = settings.admin_email.strip().lower()
            pw = settings.admin_password
            info["pw_type"] = type(pw).__name__
            info["pw_bytes_len"] = len(pw.encode("utf-8")) if isinstance(pw, str) else "not_str"
            info["pw_repr"] = repr(pw[:3]) + "..." if len(pw) > 3 else repr(pw)
            hashed = hash_password(pw)
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO users (id, email, hashed_password, role) "
                        "VALUES (gen_random_uuid(), :email, :pw, 'admin') "
                        "ON CONFLICT (email) DO UPDATE SET hashed_password = :pw"
                    ),
                    {"email": admin_email, "pw": hashed},
                )
            info["admin_created_now"] = True
            info["admin_created_email"] = admin_email
            info["users_in_db"] = 1
        except Exception as e:
            info["admin_create_error"] = str(e)

    return info


# ── Login-Seite ──
@app.get("/login", tags=["Auth"])
async def login_page(request: Request):
    """Zeigt die Login-Seite an."""
    # Bereits eingeloggt? → Dashboard
    token = request.cookies.get(JWT_COOKIE_NAME)
    if token:
        from app.auth import decode_token
        payload = decode_token(token)
        if payload:
            return RedirectResponse(url="/", status_code=302)

    csrf_token = generate_csrf_token()
    response = templates.TemplateResponse("login.html", {
        "request": request,
        "error": None,
        "csrf_token": csrf_token,
    })
    response.set_cookie(
        CSRF_COOKIE_NAME, csrf_token,
        httponly=True, samesite="strict",
        secure=settings.is_production,
        max_age=3600,
    )
    return response


@app.post("/login", tags=["Auth"])
async def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
):
    """Verarbeitet den Login."""
    client_ip = _get_client_ip(request)

    # Rate-Limit pruefen
    if not check_login_rate_limit(client_ip):
        logger.warning(f"Login Rate-Limit erreicht fuer IP {client_ip}")
        csrf_token = generate_csrf_token()
        response = templates.TemplateResponse("login.html", {
            "request": request,
            "error": "Zu viele Login-Versuche. Bitte warte eine Minute.",
            "csrf_token": csrf_token,
        }, status_code=429)
        response.set_cookie(
            CSRF_COOKIE_NAME, csrf_token,
            httponly=True, samesite="strict",
            secure=settings.is_production,
        )
        return response

    record_login_attempt(client_ip)

    # User in DB suchen und Passwort pruefen
    user = None
    login_email = email.strip().lower()
    try:
        async with engine.begin() as conn:
            result = await conn.execute(
                text("SELECT email, hashed_password, role FROM users WHERE email = :email"),
                {"email": login_email},
            )
            user = result.fetchone()
    except Exception as e:
        logger.error(f"Login DB-Fehler: {e}")

    if not user:
        logger.warning(f"Login: Kein User mit E-Mail '{login_email}' gefunden")
    elif not verify_password(password, user[1]):
        logger.warning(f"Login: Passwort-Vergleich fehlgeschlagen fuer '{login_email}'")

    if user and verify_password(password, user[1]):
        # Erfolgreicher Login
        token = create_access_token(email=user[0], role=user[2])
        csrf_token = generate_csrf_token()

        logger.info(f"Login erfolgreich: {user[0]} von IP {client_ip}")

        response = RedirectResponse(url="/", status_code=302)
        response.set_cookie(
            JWT_COOKIE_NAME, token,
            httponly=True,
            samesite="strict",
            secure=settings.is_production,
            max_age=settings.session_expire_hours * 3600,
        )
        response.set_cookie(
            CSRF_COOKIE_NAME, csrf_token,
            httponly=False,  # JS muss CSRF-Token lesen koennen
            samesite="strict",
            secure=settings.is_production,
            max_age=settings.session_expire_hours * 3600,
        )
        return response

    # Fehlgeschlagener Login
    logger.warning(f"Login fehlgeschlagen fuer '{email}' von IP {client_ip}")
    csrf_token = generate_csrf_token()
    response = templates.TemplateResponse("login.html", {
        "request": request,
        "error": "E-Mail oder Passwort falsch.",
        "csrf_token": csrf_token,
    }, status_code=401)
    response.set_cookie(
        CSRF_COOKIE_NAME, csrf_token,
        httponly=True, samesite="strict",
        secure=settings.is_production,
    )
    return response


@app.post("/logout", tags=["Auth"])
async def logout(request: Request):
    """Loggt den User aus."""
    user_email = getattr(request.state, "user_email", "unknown")
    logger.info(f"Logout: {user_email}")

    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie(JWT_COOKIE_NAME)
    response.delete_cookie(CSRF_COOKIE_NAME)
    return response


@app.post("/admin/reset-pool", tags=["System"])
async def reset_db_pool():
    """Setzt den DB-Connection-Pool zurueck und killt haengende Verbindungen."""
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
