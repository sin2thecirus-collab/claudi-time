"""Exception Handlers für das Matching-Tool."""

import logging
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.schemas.errors import ErrorCode, ErrorResponse, ValidationErrorDetail

logger = logging.getLogger(__name__)


class AppException(Exception):
    """Basis-Exception für die Anwendung."""

    def __init__(
        self,
        error_code: ErrorCode,
        message: str,
        status_code: int = status.HTTP_400_BAD_REQUEST,
        details: list[ValidationErrorDetail] | None = None,
    ):
        self.error_code = error_code
        self.message = message
        self.status_code = status_code
        self.details = details
        super().__init__(message)


class NotFoundException(AppException):
    """Exception für nicht gefundene Ressourcen."""

    def __init__(
        self,
        message: str = "Ressource nicht gefunden",
        error_code: ErrorCode = ErrorCode.NOT_FOUND,
    ):
        super().__init__(
            error_code=error_code,
            message=message,
            status_code=status.HTTP_404_NOT_FOUND,
        )


class ConflictException(AppException):
    """Exception für Konflikte (z.B. Duplikate)."""

    def __init__(
        self,
        message: str = "Konflikt mit bestehender Ressource",
        error_code: ErrorCode = ErrorCode.CONFLICT,
    ):
        super().__init__(
            error_code=error_code,
            message=message,
            status_code=status.HTTP_409_CONFLICT,
        )


class RateLimitException(AppException):
    """Exception für Rate-Limiting."""

    def __init__(self, message: str = "Zu viele Anfragen. Bitte warten."):
        super().__init__(
            error_code=ErrorCode.RATE_LIMIT_EXCEEDED,
            message=message,
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        )


class DatabaseException(AppException):
    """Exception für Datenbankfehler."""

    def __init__(self, message: str = "Datenbankfehler. Bitte später erneut versuchen."):
        super().__init__(
            error_code=ErrorCode.DATABASE_ERROR,
            message=message,
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )


class ExternalServiceException(AppException):
    """Exception für externe Service-Fehler."""

    def __init__(
        self,
        service: str,
        error_code: ErrorCode,
        message: str | None = None,
        status_code: int = status.HTTP_504_GATEWAY_TIMEOUT,
    ):
        super().__init__(
            error_code=error_code,
            message=message or f"{service}-Service nicht erreichbar",
            status_code=status_code,
        )


class OpenAIException(ExternalServiceException):
    """Exception für OpenAI-Fehler."""

    def __init__(self, message: str = "KI-Service nicht erreichbar. Bitte später versuchen."):
        super().__init__(
            service="OpenAI",
            error_code=ErrorCode.OPENAI_TIMEOUT,
            message=message,
        )


class GeocodingException(ExternalServiceException):
    """Exception für Geocoding-Fehler."""

    def __init__(self, message: str = "Geocoding-Service nicht erreichbar."):
        super().__init__(
            service="Geocoding",
            error_code=ErrorCode.GEOCODING_TIMEOUT,
            message=message,
        )


class CRMException(ExternalServiceException):
    """Exception für CRM-Fehler."""

    def __init__(self, message: str = "CRM-Service nicht erreichbar."):
        super().__init__(
            service="CRM",
            error_code=ErrorCode.CRM_SERVICE_ERROR,
            message=message,
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )


def _get_request_id(request: Request) -> str | None:
    """Holt die Request-ID aus den Headers."""
    return request.headers.get("X-Request-ID")


def _create_error_response(
    error_code: ErrorCode,
    message: str,
    request: Request,
    details: list[ValidationErrorDetail] | None = None,
) -> dict[str, Any]:
    """Erstellt ein einheitliches Fehler-Response-Format."""
    return ErrorResponse(
        error=error_code,
        message=message,
        details=details,
        request_id=_get_request_id(request),
    ).model_dump()


async def app_exception_handler(request: Request, exc: AppException) -> JSONResponse:
    """Handler für AppException."""
    logger.warning(
        f"AppException: {exc.error_code} - {exc.message}",
        extra={"request_id": _get_request_id(request)},
    )
    return JSONResponse(
        status_code=exc.status_code,
        content=_create_error_response(
            error_code=exc.error_code,
            message=exc.message,
            request=request,
            details=exc.details,
        ),
    )


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Handler für Pydantic-Validierungsfehler."""
    details = []
    for error in exc.errors():
        field = ".".join(str(loc) for loc in error["loc"] if loc != "body")
        details.append(
            ValidationErrorDetail(
                field=field or "body",
                message=error["msg"],
                value=error.get("input"),
            )
        )

    logger.info(
        f"Validation error: {len(details)} errors",
        extra={"request_id": _get_request_id(request)},
    )

    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=_create_error_response(
            error_code=ErrorCode.VALIDATION_ERROR,
            message="Validierungsfehler in der Anfrage",
            request=request,
            details=details,
        ),
    )


async def integrity_error_handler(request: Request, exc: IntegrityError) -> JSONResponse:
    """Handler für Datenbank-Integritätsfehler (z.B. Duplikate)."""
    logger.warning(
        f"IntegrityError: {exc}",
        extra={"request_id": _get_request_id(request)},
    )
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content=_create_error_response(
            error_code=ErrorCode.DUPLICATE_ENTRY,
            message="Ein Eintrag mit diesen Daten existiert bereits",
            request=request,
        ),
    )


async def database_exception_handler(
    request: Request, exc: SQLAlchemyError
) -> JSONResponse:
    """Handler für allgemeine Datenbankfehler."""
    logger.error(
        f"Database error: {exc}",
        extra={"request_id": _get_request_id(request)},
        exc_info=True,
    )
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content=_create_error_response(
            error_code=ErrorCode.DATABASE_ERROR,
            message="Datenbankfehler. Bitte später erneut versuchen.",
            request=request,
        ),
    )


async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Handler für alle unbehandelten Exceptions."""
    logger.error(
        f"Unhandled exception: {exc}",
        extra={"request_id": _get_request_id(request)},
        exc_info=True,
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=_create_error_response(
            error_code=ErrorCode.INTERNAL_ERROR,
            message="Interner Serverfehler",
            request=request,
        ),
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Registriert alle Exception-Handler bei der FastAPI-App."""
    app.add_exception_handler(AppException, app_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(IntegrityError, integrity_error_handler)
    app.add_exception_handler(SQLAlchemyError, database_exception_handler)
    app.add_exception_handler(Exception, generic_exception_handler)
