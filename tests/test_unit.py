"""Unit Tests - Laufen ohne Datenbank."""

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest


# ==================== VALIDATION TESTS ====================

class TestPostalCodeValidation:
    """Tests für PLZ-Validierung."""

    def test_valid_postal_code(self):
        """Gültige PLZ wird akzeptiert."""
        from app.schemas.validators import validate_postal_code
        assert validate_postal_code("20095") == "20095"
        assert validate_postal_code("80331") == "80331"
        assert validate_postal_code("01067") == "01067"

    def test_postal_code_with_whitespace(self):
        """PLZ mit Whitespace wird getrimmt."""
        from app.schemas.validators import validate_postal_code
        assert validate_postal_code("  20095  ") == "20095"

    def test_none_postal_code(self):
        """None wird akzeptiert."""
        from app.schemas.validators import validate_postal_code
        assert validate_postal_code(None) is None

    def test_empty_postal_code(self):
        """Leere Strings werden zu None."""
        from app.schemas.validators import validate_postal_code
        assert validate_postal_code("") is None
        assert validate_postal_code("   ") is None

    def test_invalid_postal_code_too_short(self):
        """Zu kurze PLZ wird abgelehnt."""
        from app.schemas.validators import validate_postal_code
        with pytest.raises(ValueError, match="5 Ziffern"):
            validate_postal_code("2009")

    def test_invalid_postal_code_too_long(self):
        """Zu lange PLZ wird abgelehnt."""
        from app.schemas.validators import validate_postal_code
        with pytest.raises(ValueError, match="5 Ziffern"):
            validate_postal_code("200950")

    def test_invalid_postal_code_with_letters(self):
        """PLZ mit Buchstaben wird abgelehnt."""
        from app.schemas.validators import validate_postal_code
        with pytest.raises(ValueError, match="5 Ziffern"):
            validate_postal_code("2009A")


class TestCityValidation:
    """Tests für Städte-Validierung."""

    def test_valid_city(self):
        """Gültiger Stadtname wird akzeptiert."""
        from app.schemas.validators import validate_city
        assert validate_city("Hamburg") == "Hamburg"
        assert validate_city("München") == "München"

    def test_city_with_whitespace(self):
        """Stadt mit Whitespace wird getrimmt."""
        from app.schemas.validators import validate_city
        assert validate_city("  Hamburg  ") == "Hamburg"

    def test_none_city(self):
        """None wird akzeptiert."""
        from app.schemas.validators import validate_city
        assert validate_city(None) is None

    def test_city_too_short(self):
        """Zu kurze Stadt wird abgelehnt."""
        from app.schemas.validators import validate_city
        with pytest.raises(ValueError, match="mindestens 2 Zeichen"):
            validate_city("A")


class TestSearchTermValidation:
    """Tests für Suchbegriff-Validierung."""

    def test_valid_search_term(self):
        """Gültiger Suchbegriff wird akzeptiert."""
        from app.schemas.validators import validate_search_term
        assert validate_search_term("Buchhalter") == "Buchhalter"

    def test_search_term_too_short(self):
        """Zu kurzer Suchbegriff wird abgelehnt."""
        from app.schemas.validators import validate_search_term
        with pytest.raises(ValueError, match="mindestens"):
            validate_search_term("A")


class TestUUIDListValidation:
    """Tests für UUID-Listen-Validierung."""

    def test_valid_uuid_list(self):
        """Gültige UUID-Liste wird akzeptiert."""
        from app.schemas.validators import validate_uuid_list
        uuids = [uuid.uuid4(), uuid.uuid4()]
        result = validate_uuid_list(uuids)
        assert result == uuids

    def test_empty_uuid_list(self):
        """Leere Liste wird abgelehnt."""
        from app.schemas.validators import validate_uuid_list
        with pytest.raises(ValueError, match="nicht leer"):
            validate_uuid_list([])


# ==================== KEYWORD MATCHER TESTS ====================

class TestKeywordMatcher:
    """Tests für den KeywordMatcher Service."""

    def test_extract_accounting_keywords(self):
        """Extrahiert Buchhaltungs-Keywords aus Job-Text."""
        from app.services.keyword_matcher import KeywordMatcher

        matcher = KeywordMatcher()
        job_text = """
        Wir suchen einen Finanzbuchhalter mit SAP und DATEV Kenntnissen.
        Erfahrung in Debitorenbuchhaltung und HGB erforderlich.
        """

        keywords = matcher.extract_keywords_from_text(job_text)

        assert "sap" in keywords
        assert "datev" in keywords
        assert "debitorenbuchhaltung" in keywords
        assert "hgb" in keywords

    def test_extract_technical_keywords(self):
        """Extrahiert technische Keywords aus Job-Text."""
        from app.services.keyword_matcher import KeywordMatcher

        matcher = KeywordMatcher()
        job_text = """
        Elektriker gesucht für SPS-Programmierung.
        Erfahrung mit Simatic und KNX erforderlich.
        """

        keywords = matcher.extract_keywords_from_text(job_text)

        assert "elektriker" in keywords
        assert "sps" in keywords
        assert "simatic" in keywords
        assert "knx" in keywords

    def test_extract_keywords_empty_text(self):
        """Leerer Text liefert leere Liste."""
        from app.services.keyword_matcher import KeywordMatcher

        matcher = KeywordMatcher()
        keywords = matcher.extract_keywords_from_text("")
        assert keywords == []

    def test_extract_keywords_none_text(self):
        """None-Text liefert leere Liste."""
        from app.services.keyword_matcher import KeywordMatcher

        matcher = KeywordMatcher()
        keywords = matcher.extract_keywords_from_text(None)
        assert keywords == []

    def test_find_matching_keywords(self):
        """Findet übereinstimmende Keywords."""
        from app.services.keyword_matcher import KeywordMatcher

        matcher = KeywordMatcher()
        candidate_skills = ["SAP", "DATEV", "Buchhaltung"]
        job_text = "Wir suchen jemanden mit SAP und DATEV Kenntnissen."

        matched = matcher.find_matching_keywords(candidate_skills, job_text)

        assert "sap" in matched
        assert "datev" in matched

    def test_find_matching_keywords_case_insensitive(self):
        """Matching ist case-insensitive."""
        from app.services.keyword_matcher import KeywordMatcher

        matcher = KeywordMatcher()
        candidate_skills = ["sap", "DATEV"]
        job_text = "Wir suchen jemanden mit SAP und datev Kenntnissen."

        matched = matcher.find_matching_keywords(candidate_skills, job_text)

        assert "sap" in matched
        assert "datev" in matched

    def test_calculate_score_full_match(self):
        """Vollständiger Match ergibt Score 1.0."""
        from app.services.keyword_matcher import KeywordMatcher

        matcher = KeywordMatcher()
        matched = ["sap", "datev", "buchhaltung"]
        score = matcher.calculate_score(matched, total_skills=3)
        assert score == 1.0

    def test_calculate_score_partial_match(self):
        """Teilweiser Match ergibt anteiligen Score."""
        from app.services.keyword_matcher import KeywordMatcher

        matcher = KeywordMatcher()
        matched = ["sap", "datev"]
        score = matcher.calculate_score(matched, total_skills=4)
        assert score == 0.5

    def test_calculate_score_no_match(self):
        """Kein Match ergibt Score 0.0."""
        from app.services.keyword_matcher import KeywordMatcher

        matcher = KeywordMatcher()
        score = matcher.calculate_score([], total_skills=5)
        assert score == 0.0

    def test_match_full_workflow(self):
        """Vollständiger Matching-Workflow."""
        from app.services.keyword_matcher import KeywordMatcher, KeywordMatchResult

        matcher = KeywordMatcher()
        candidate_skills = ["SAP", "DATEV", "Buchhaltung", "Excel"]
        job_text = "Wir suchen Buchhalter mit SAP und DATEV Kenntnissen."

        result = matcher.match(candidate_skills, job_text)

        assert isinstance(result, KeywordMatchResult)
        assert len(result.matched_keywords) > 0
        assert result.total_candidate_skills == 4
        assert 0.0 <= result.keyword_score <= 1.0


class TestKeywordConstants:
    """Tests für Keyword-Konstanten."""

    def test_accounting_keywords_not_empty(self):
        """Buchhaltungs-Keywords sind definiert."""
        from app.services.keyword_matcher import ACCOUNTING_KEYWORDS

        assert len(ACCOUNTING_KEYWORDS) > 0
        assert "sap" in ACCOUNTING_KEYWORDS
        assert "datev" in ACCOUNTING_KEYWORDS

    def test_technical_keywords_not_empty(self):
        """Technische Keywords sind definiert."""
        from app.services.keyword_matcher import TECHNICAL_KEYWORDS

        assert len(TECHNICAL_KEYWORDS) > 0
        assert "elektriker" in TECHNICAL_KEYWORDS
        assert "sps" in TECHNICAL_KEYWORDS

    def test_keywords_lowercase(self):
        """Alle Keywords sind lowercase."""
        from app.services.keyword_matcher import ACCOUNTING_KEYWORDS, TECHNICAL_KEYWORDS

        for keyword in ACCOUNTING_KEYWORDS:
            assert keyword == keyword.lower()

        for keyword in TECHNICAL_KEYWORDS:
            assert keyword == keyword.lower()


# ==================== CONFIG TESTS ====================

class TestLimits:
    """Tests für System-Limits."""

    def test_limits_defined(self):
        """Alle wichtigen Limits sind definiert."""
        from app.config import Limits

        assert Limits.CSV_MAX_FILE_SIZE_MB == 50
        assert Limits.CSV_MAX_ROWS == 10_000
        assert Limits.BATCH_DELETE_MAX == 100
        assert Limits.AI_CHECK_MAX_CANDIDATES == 50
        assert Limits.DEFAULT_RADIUS_KM == 25

    def test_limits_positive(self):
        """Alle Limits sind positive Zahlen."""
        from app.config import Limits

        assert Limits.CSV_MAX_FILE_SIZE_MB > 0
        assert Limits.CSV_MAX_ROWS > 0
        assert Limits.BATCH_DELETE_MAX > 0
        assert Limits.PAGE_SIZE_DEFAULT > 0


# ==================== MOCK MODEL TESTS ====================

class TestMockModels:
    """Tests für Mock-Models (ohne DB)."""

    def test_job_properties(self):
        """Job-Properties funktionieren."""
        # Mock Job
        class MockJob:
            def __init__(self):
                self.deleted_at = None
                self.expires_at = None
                self.city = "Hamburg"
                self.work_location_city = None

            @property
            def is_deleted(self):
                return self.deleted_at is not None

            @property
            def is_expired(self):
                if self.expires_at is None:
                    return False
                return self.expires_at < datetime.now(timezone.utc)

            @property
            def display_city(self):
                return self.work_location_city or self.city or "Unbekannt"

        job = MockJob()
        assert job.is_deleted is False
        assert job.is_expired is False
        assert job.display_city == "Hamburg"

        job.deleted_at = datetime.now(timezone.utc)
        assert job.is_deleted is True

        job.work_location_city = "München"
        assert job.display_city == "München"

    def test_candidate_properties(self):
        """Candidate-Properties funktionieren."""
        # Mock Candidate
        class MockCandidate:
            def __init__(self):
                self.first_name = "Max"
                self.last_name = "Mustermann"
                self.birth_date = date(1985, 5, 15)
                self.hidden = False
                self.created_at = datetime.now(timezone.utc)

            @property
            def full_name(self):
                parts = [self.first_name, self.last_name]
                return " ".join(p for p in parts if p) or "Unbekannt"

            @property
            def age(self):
                if not self.birth_date:
                    return None
                today = date.today()
                age = today.year - self.birth_date.year
                if (today.month, today.day) < (self.birth_date.month, self.birth_date.day):
                    age -= 1
                return age

        candidate = MockCandidate()
        assert candidate.full_name == "Max Mustermann"
        assert candidate.age is not None
        assert 35 <= candidate.age <= 45

        candidate.first_name = None
        assert candidate.full_name == "Mustermann"

        candidate.last_name = None
        assert candidate.full_name == "Unbekannt"

    def test_match_properties(self):
        """Match-Properties funktionieren."""
        # Mock Match
        class MockMatch:
            def __init__(self):
                self.distance_km = 5.0
                self.matched_keywords = ["SAP", "DATEV", "Buchhaltung"]
                self.ai_checked_at = None

            @property
            def is_ai_checked(self):
                return self.ai_checked_at is not None

            @property
            def is_excellent(self):
                distance_ok = self.distance_km is not None and self.distance_km <= 5
                keywords_ok = self.matched_keywords is not None and len(self.matched_keywords) >= 3
                return distance_ok and keywords_ok

        match = MockMatch()
        assert match.is_ai_checked is False
        assert match.is_excellent is True

        match.ai_checked_at = datetime.now(timezone.utc)
        assert match.is_ai_checked is True

        match.distance_km = 10.0
        assert match.is_excellent is False

        match.distance_km = 3.0
        match.matched_keywords = ["SAP"]
        assert match.is_excellent is False
