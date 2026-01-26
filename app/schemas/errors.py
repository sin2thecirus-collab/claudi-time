"""Error Schemas für das Matching-Tool."""

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ErrorCode(str, Enum):
    """Fehlercodes für das Matching-Tool."""

    # Validierungsfehler (400)
    VALIDATION_ERROR = "validation_error"
    INVALID_UUID = "invalid_uuid"
    INVALID_FILE_FORMAT = "invalid_file_format"
    FILE_TOO_LARGE = "file_too_large"
    TOO_MANY_ROWS = "too_many_rows"
    MISSING_REQUIRED_FIELD = "missing_required_field"
    INVALID_FILTER = "invalid_filter"
    BATCH_LIMIT_EXCEEDED = "batch_limit_exceeded"

    # Authentifizierung (401)
    UNAUTHORIZED = "unauthorized"
    INVALID_API_KEY = "invalid_api_key"

    # Autorisierung (403)
    FORBIDDEN = "forbidden"

    # Nicht gefunden (404)
    NOT_FOUND = "not_found"
    JOB_NOT_FOUND = "job_not_found"
    CANDIDATE_NOT_FOUND = "candidate_not_found"
    MATCH_NOT_FOUND = "match_not_found"
    IMPORT_JOB_NOT_FOUND = "import_job_not_found"

    # Konflikt (409)
    DUPLICATE_ENTRY = "duplicate_entry"
    CONFLICT = "conflict"
    IMPORT_ALREADY_RUNNING = "import_already_running"
    JOB_ALREADY_RUNNING = "job_already_running"

    # Rate Limiting (429)
    RATE_LIMIT_EXCEEDED = "rate_limit_exceeded"

    # Server-Fehler (500)
    INTERNAL_ERROR = "internal_error"

    # Service Unavailable (503)
    DATABASE_ERROR = "database_error"
    CRM_SERVICE_ERROR = "crm_service_error"

    # Gateway Timeout (504)
    OPENAI_TIMEOUT = "openai_timeout"
    GEOCODING_TIMEOUT = "geocoding_timeout"
    CRM_TIMEOUT = "crm_timeout"


class ValidationErrorDetail(BaseModel):
    """Detail eines Validierungsfehlers."""

    field: str = Field(description="Betroffenes Feld")
    message: str = Field(description="Fehlermeldung")
    value: Any | None = Field(default=None, description="Ungültiger Wert")


class ErrorResponse(BaseModel):
    """Standard-Fehler-Response."""

    error: ErrorCode = Field(description="Fehlercode")
    message: str = Field(description="Fehlermeldung")
    details: list[ValidationErrorDetail] | None = Field(
        default=None,
        description="Details bei Validierungsfehlern",
    )
    request_id: str | None = Field(
        default=None,
        description="Request-ID für Debugging",
    )

    model_config = {"json_schema_extra": {"examples": [
        {
            "error": "validation_error",
            "message": "Validierungsfehler in der Anfrage",
            "details": [
                {
                    "field": "company_name",
                    "message": "Pflichtfeld fehlt",
                    "value": None,
                }
            ],
            "request_id": "abc123",
        }
    ]}}
