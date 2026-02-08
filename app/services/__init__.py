"""Business-Logik Services für das Matching-Tool."""

from app.services.csv_import_service import CSVImportService, run_csv_import
from app.services.csv_validator import (
    CSVValidator,
    ValidationError,
    ValidationResult,
    calculate_content_hash,
)
from app.services.geocoding_service import (
    GeocodingResult,
    GeocodingService,
    ProcessResult,
)
from app.services.job_service import JobService
from app.services.candidate_service import CandidateService
from app.services.cv_parser_service import CVParserService, ParseResult
from app.services.openai_service import OpenAIService, MatchEvaluation, OpenAIUsage
from app.services.keyword_matcher import (
    KeywordMatcher,
    KeywordMatchResult,
    keyword_matcher,
    ACCOUNTING_KEYWORDS,
    TECHNICAL_KEYWORDS,
)
from app.services.matching_service import (
    MatchingService,
    MatchingResult,
    BatchMatchingResult,
)
from app.services.statistics_service import StatisticsService, DashboardStats, TopFilter
from app.services.alert_service import AlertService

__all__ = [
    # CSV
    "CSVValidator",
    "ValidationError",
    "ValidationResult",
    "calculate_content_hash",
    "CSVImportService",
    "run_csv_import",
    # Geocoding
    "GeocodingService",
    "GeocodingResult",
    "ProcessResult",
    # Job
    "JobService",
    # Candidate
    "CandidateService",
    # CV-Parsing
    "CVParserService",
    "ParseResult",
    # OpenAI
    "OpenAIService",
    "MatchEvaluation",
    "OpenAIUsage",
    # Keyword-Matching
    "KeywordMatcher",
    "KeywordMatchResult",
    "keyword_matcher",
    "ACCOUNTING_KEYWORDS",
    "TECHNICAL_KEYWORDS",
    # Matching
    "MatchingService",
    "MatchingResult",
    "BatchMatchingResult",
    # Statistics
    "StatisticsService",
    "DashboardStats",
    "TopFilter",
    # Alerts
    "AlertService",
]
