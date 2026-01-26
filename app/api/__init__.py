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
