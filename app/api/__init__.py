"""API-Routen für das Matching-Tool."""

from app.api.exception_handlers import (
    AppException,
    ConflictException,
    CRMException,
    DatabaseException,
    ExternalServiceException,
    GeocodingException,
    NotFoundException,
    OpenAIException,
    RateLimitException,
    register_exception_handlers,
)
from app.api.rate_limiter import (
    InMemoryRateLimiter,
    RateLimitTier,
    check_rate_limit,
    rate_limit,
    rate_limiter,
)
from app.api.routes_jobs import router as jobs_router
from app.api.routes_candidates import router as candidates_router
from app.api.routes_matches import router as matches_router
from app.api.routes_filters import router as filters_router
from app.api.routes_settings import router as settings_router
from app.api.routes_admin import router as admin_router

__all__ = [
    # Exceptions
    "AppException",
    "NotFoundException",
    "ConflictException",
    "RateLimitException",
    "DatabaseException",
    "ExternalServiceException",
    "OpenAIException",
    "GeocodingException",
    "CRMException",
    "register_exception_handlers",
    # Rate Limiter
    "InMemoryRateLimiter",
    "RateLimitTier",
    "rate_limit",
    "rate_limiter",
    "check_rate_limit",
]
