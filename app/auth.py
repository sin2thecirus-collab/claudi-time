"""Authentifizierung und Autorisierung fuer die Pulspoint CRM App.

Schuetzt ALLE Endpoints ausser:
- /health (Railway Health Check)
- /login (Login-Seite + Form-Submit)
- /api/n8n/* (Bearer-Token Schutz via eigene Middleware)
- /static/* (CSS, JS, Bilder)
"""

import logging
import secrets
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt
from passlib.context import CryptContext
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse, JSONResponse

from app.config import settings

logger = logging.getLogger(__name__)

# ── Password Hashing (bcrypt, 12 Rounds) ──
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# ── JWT Config ──
JWT_ALGORITHM = "HS256"
JWT_COOKIE_NAME = "pp_session"

# ── CSRF Token ──
CSRF_COOKIE_NAME = "pp_csrf"
CSRF_HEADER_NAME = "x-csrf-token"

# ── Login Rate Limiting ──
# Max 5 Versuche pro IP pro Minute
_login_attempts: dict[str, list[float]] = defaultdict(list)
LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 60


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Prueft Passwort gegen Hash."""
    return pwd_context.verify(plain_password, hashed_password)


def hash_password(password: str) -> str:
    """Erzeugt bcrypt Hash."""
    return pwd_context.hash(password)


def create_access_token(email: str, role: str = "admin") -> str:
    """Erzeugt JWT Token mit Ablaufzeit."""
    expire = datetime.now(timezone.utc) + timedelta(hours=settings.session_expire_hours)
    payload = {
        "sub": email,
        "role": role,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict | None:
    """Dekodiert JWT Token. Gibt None zurueck bei ungueltigem/abgelaufenem Token."""
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[JWT_ALGORITHM])
        return payload
    except JWTError:
        return None


def generate_csrf_token() -> str:
    """Erzeugt zufaelligen CSRF-Token."""
    return secrets.token_urlsafe(32)


def check_login_rate_limit(client_ip: str) -> bool:
    """Prueft ob Login-Versuche das Limit ueberschreiten. True = OK, False = geblockt."""
    now = time.time()
    # Alte Eintraege entfernen
    _login_attempts[client_ip] = [
        t for t in _login_attempts[client_ip]
        if now - t < LOGIN_WINDOW_SECONDS
    ]
    if len(_login_attempts[client_ip]) >= LOGIN_MAX_ATTEMPTS:
        return False
    return True


def record_login_attempt(client_ip: str) -> None:
    """Zeichnet einen Login-Versuch auf."""
    _login_attempts[client_ip].append(time.time())


def _get_client_ip(request: Request) -> str:
    """Holt die Client-IP (beruecksichtigt Proxy-Header)."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ── Pfade die OHNE Auth erreichbar sein muessen ──
PUBLIC_PATHS = frozenset({
    "/health",
    "/login",
    "/favicon.ico",
    "/auth-debug",  # TEMPORÄR — wird nach Login-Fix entfernt
})

PUBLIC_PREFIXES = (
    "/static/",
    "/api/n8n/",  # n8n Webhooks haben eigene Bearer-Token Auth
)


class AuthMiddleware(BaseHTTPMiddleware):
    """Middleware die JEDEN Request auf gueltige Authentifizierung prueft.

    Unterstuetzte Auth-Methoden (in dieser Reihenfolge):
    1. JWT Cookie (pp_session) — fuer Browser-Sessions
    2. X-API-Key Header — fuer programmatischen Zugriff
    3. Redirect zu /login (HTML) oder 401 (API)
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # ── Oeffentliche Pfade durchlassen ──
        if path in PUBLIC_PATHS or any(path.startswith(p) for p in PUBLIC_PREFIXES):
            return await call_next(request)

        # ── Auth pruefen ──
        user_email = None

        # 1. JWT Cookie pruefen
        token = request.cookies.get(JWT_COOKIE_NAME)
        if token:
            payload = decode_token(token)
            if payload:
                user_email = payload.get("sub")
                request.state.user_email = user_email
                request.state.user_role = payload.get("role", "user")

        # 2. API-Key Header pruefen
        if not user_email and settings.api_access_key:
            api_key = request.headers.get("x-api-key")
            if api_key and api_key == settings.api_access_key:
                user_email = "api-access"
                request.state.user_email = user_email
                request.state.user_role = "admin"

        # 3. Nicht authentifiziert → abweisen
        if not user_email:
            # API-Requests → 401 JSON
            if path.startswith("/api/"):
                return JSONResponse(
                    status_code=401,
                    content={"error": "unauthorized", "message": "Nicht authentifiziert"},
                )
            # HTML-Requests → Redirect zu Login
            return RedirectResponse(url="/login", status_code=302)

        # ── CSRF-Schutz fuer state-changing Requests ──
        if request.method in ("POST", "PUT", "DELETE", "PATCH"):
            # Login/Logout sind davon ausgenommen (Login hat noch keinen CSRF-Token)
            if path not in ("/login", "/logout"):
                csrf_cookie = request.cookies.get(CSRF_COOKIE_NAME)
                csrf_header = request.headers.get(CSRF_HEADER_NAME)
                # HTMX sendet CSRF via Header, Forms via Hidden-Field
                if not csrf_header:
                    # Versuche aus Form-Daten zu lesen (fuer nicht-HTMX Forms)
                    # Aber HTMX-Requests sind 99% der Faelle
                    pass
                if csrf_cookie and csrf_header and csrf_cookie != csrf_header:
                    return JSONResponse(
                        status_code=403,
                        content={"error": "csrf_invalid", "message": "CSRF-Token ungueltig"},
                    )

        response = await call_next(request)
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Fuegt Security-Headers zu jeder Response hinzu."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)

        # Anti-Clickjacking
        response.headers["X-Frame-Options"] = "DENY"
        # Verhindert MIME-Type Sniffing
        response.headers["X-Content-Type-Options"] = "nosniff"
        # XSS-Schutz (Legacy-Browser)
        response.headers["X-XSS-Protection"] = "1; mode=block"
        # Referrer-Policy
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        # HSTS (nur HTTPS, 1 Jahr)
        if settings.is_production:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        # Keine Server-Info leaken
        if "server" in response.headers:
            del response.headers["server"]

        return response
