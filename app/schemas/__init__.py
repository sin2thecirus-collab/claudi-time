"""Pydantic Schemas für das Matching-Tool."""

from app.schemas.candidate import (
    CandidateCreate,
    CandidateListResponse,
    CandidateResponse,
    CandidateUpdate,
    CandidateWithMatch,
    CVParseResult,
    EducationEntry,
    WorkHistoryEntry,
)
from app.schemas.errors import ErrorCode, ErrorResponse, ValidationErrorDetail
from app.schemas.filters import (
    CandidateFilterParams,
    CandidateSortBy,
    FilterOptionsResponse,
    FilterPresetCreate,
    FilterPresetResponse,
    JobFilterParams,
    JobSortBy,
    SortOrder,
)
from app.schemas.job import (
    ImportJobResponse,
    JobCreate,
    JobImportRow,
    JobListResponse,
    JobResponse,
    JobUpdate,
)
from app.schemas.match import (
    AICheckRequest,
    AICheckResponse,
    AICheckResultItem,
    MatchListResponse,
    MatchPlacedUpdate,
    MatchResponse,
    MatchStatusUpdate,
    MatchWithDetails,
)
from app.schemas.pagination import PaginatedResponse, PaginationParams
from app.schemas.validators import (
    BatchDeleteRequest,
    BatchHideRequest,
    CityName,
    PostalCode,
    SearchTerm,
)

__all__ = [
    # Job
    "JobCreate",
    "JobUpdate",
    "JobResponse",
    "JobListResponse",
    "JobImportRow",
    "ImportJobResponse",
    # Candidate
    "CandidateCreate",
    "CandidateUpdate",
    "CandidateResponse",
    "CandidateListResponse",
    "CandidateWithMatch",
    "WorkHistoryEntry",
    "EducationEntry",
    "CVParseResult",
    # Match
    "MatchResponse",
    "MatchWithDetails",
    "MatchListResponse",
    "AICheckRequest",
    "AICheckResponse",
    "AICheckResultItem",
    "MatchStatusUpdate",
    "MatchPlacedUpdate",
    # Filter
    "JobFilterParams",
    "CandidateFilterParams",
    "FilterPresetCreate",
    "FilterPresetResponse",
    "FilterOptionsResponse",
    "JobSortBy",
    "CandidateSortBy",
    "SortOrder",
    # Pagination
    "PaginationParams",
    "PaginatedResponse",
    # Errors
    "ErrorCode",
    "ErrorResponse",
    "ValidationErrorDetail",
    # Validators
    "BatchDeleteRequest",
    "BatchHideRequest",
    "PostalCode",
    "CityName",
    "SearchTerm",
]
