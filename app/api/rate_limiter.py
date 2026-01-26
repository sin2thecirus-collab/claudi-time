"""In-Memory Rate-Limiter für das Matching-Tool."""

import time
from collections import defaultdict
from dataclasses import dataclass
from enum import Enum
from functools import wraps
from typing import Callable

from fastapi import Request

from app.api.exception_handlers import RateLimitException


class RateLimitTier(str, Enum):
    """Rate-Limit-Stufen für verschiedene Endpoint-Typen."""

    # Standard-Endpoints (lesen)
    STANDARD = "standard"  # 100 Requests/Minute

    # Schreib-Operationen
    WRITE = "write"  # 30 Requests/Minute

    # KI-Operationen (teuer)
    AI = "ai"  # 10 Requests/Minute

    # Import-Operationen
    IMPORT = "import"  # 5 Requests/Minute

    # Admin-Trigger (manuell)
    ADMIN = "admin"  # 10 Requests/Minute


@dataclass
class RateLimitConfig:
    """Konfiguration für eine Rate-Limit-Stufe."""

    requests: int  # Anzahl erlaubter Requests
    window_seconds: int  # Zeitfenster in Sekunden


# Rate-Limit-Konfigurationen
RATE_LIMITS: dict[RateLimitTier, RateLimitConfig] = {
    RateLimitTier.STANDARD: RateLimitConfig(requests=100, window_seconds=60),
    RateLimitTier.WRITE: RateLimitConfig(requests=30, window_seconds=60),
    RateLimitTier.AI: RateLimitConfig(requests=10, window_seconds=60),
    RateLimitTier.IMPORT: RateLimitConfig(requests=5, window_seconds=60),
    RateLimitTier.ADMIN: RateLimitConfig(requests=10, window_seconds=60),
}


class InMemoryRateLimiter:
    """
    Einfacher In-Memory Rate-Limiter.

    Für Einzelnutzer-Anwendung ausreichend.
    Bei Skalierung auf Redis umstellen.
    """

    def __init__(self):
        # {client_key: [(timestamp, tier), ...]}
        self._requests: dict[str, list[tuple[float, RateLimitTier]]] = defaultdict(list)
        self._last_cleanup = time.time()
        self._cleanup_interval = 300  # 5 Minuten

    def _get_client_key(self, request: Request) -> str:
        """
        Ermittelt einen eindeutigen Schlüssel für den Client.

        Da Einzelnutzer-App: IP ist ausreichend.
        """
        # X-Forwarded-For für Proxies (Railway, etc.)
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    def _cleanup_old_entries(self) -> None:
        """Entfernt abgelaufene Einträge aus dem Speicher."""
        now = time.time()

        # Nur alle 5 Minuten aufräumen
        if now - self._last_cleanup < self._cleanup_interval:
            return

        max_window = max(config.window_seconds for config in RATE_LIMITS.values())

        for client_key in list(self._requests.keys()):
            self._requests[client_key] = [
                (ts, tier)
                for ts, tier in self._requests[client_key]
                if now - ts < max_window
            ]
            # Leere Listen entfernen
            if not self._requests[client_key]:
                del self._requests[client_key]

        self._last_cleanup = now

    def is_rate_limited(
        self,
        request: Request,
        tier: RateLimitTier = RateLimitTier.STANDARD,
    ) -> tuple[bool, int]:
        """
        Prüft, ob ein Request rate-limited ist.

        Returns:
            (is_limited, retry_after_seconds)
        """
        self._cleanup_old_entries()

        client_key = self._get_client_key(request)
        config = RATE_LIMITS[tier]
        now = time.time()
        window_start = now - config.window_seconds

        # Zähle Requests im Zeitfenster für diese Tier
        tier_requests = [
            (ts, t)
            for ts, t in self._requests[client_key]
            if ts > window_start and t == tier
        ]

        if len(tier_requests) >= config.requests:
            # Rate-Limit erreicht
            oldest_request = min(ts for ts, _ in tier_requests)
            retry_after = int(oldest_request + config.window_seconds - now) + 1
            return True, max(retry_after, 1)

        # Request erlaubt - hinzufügen
        self._requests[client_key].append((now, tier))
        return False, 0

    def get_remaining(
        self,
        request: Request,
        tier: RateLimitTier = RateLimitTier.STANDARD,
    ) -> int:
        """Gibt die verbleibenden Requests für einen Client zurück."""
        client_key = self._get_client_key(request)
        config = RATE_LIMITS[tier]
        now = time.time()
        window_start = now - config.window_seconds

        tier_requests = [
            (ts, t)
            for ts, t in self._requests[client_key]
            if ts > window_start and t == tier
        ]

        return max(0, config.requests - len(tier_requests))


# Singleton-Instanz
rate_limiter = InMemoryRateLimiter()


def rate_limit(tier: RateLimitTier = RateLimitTier.STANDARD) -> Callable:
    """
    Decorator für Rate-Limiting von Endpoints.

    Beispiel:
        @router.get("/jobs")
        @rate_limit(RateLimitTier.STANDARD)
        async def list_jobs(request: Request):
            ...
    """

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Request aus den Argumenten holen
            request = kwargs.get("request")
            if request is None:
                for arg in args:
                    if isinstance(arg, Request):
                        request = arg
                        break

            if request:
                is_limited, retry_after = rate_limiter.is_rate_limited(request, tier)
                if is_limited:
                    raise RateLimitException(
                        f"Zu viele Anfragen. Bitte {retry_after} Sekunden warten."
                    )

            return await func(*args, **kwargs)

        return wrapper

    return decorator


async def check_rate_limit(
    request: Request,
    tier: RateLimitTier = RateLimitTier.STANDARD,
) -> None:
    """
    Dependency für Rate-Limiting.

    Beispiel:
        @router.get("/jobs")
        async def list_jobs(
            request: Request,
            _: None = Depends(lambda r: check_rate_limit(r, RateLimitTier.STANDARD))
        ):
            ...
    """
    is_limited, retry_after = rate_limiter.is_rate_limited(request, tier)
    if is_limited:
        raise RateLimitException(
            f"Zu viele Anfragen. Bitte {retry_after} Sekunden warten."
        )
