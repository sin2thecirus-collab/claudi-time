"""Tests für Matching-Logik."""

from datetime import datetime, timezone

import pytest

from app.models.match import Match, MatchStatus
from app.services.keyword_matcher import (
    ACCOUNTING_KEYWORDS,
    TECHNICAL_KEYWORDS,
    KeywordMatcher,
    KeywordMatchResult,
)
from tests.conftest import (
    SAMPLE_JOB_TEXT_ACCOUNTING,
    SAMPLE_JOB_TEXT_TECHNICAL,
    JobFactory,
    CandidateFactory,
    MatchFactory,
)


class TestKeywordMatcher:
    """Tests für den KeywordMatcher Service."""

    @pytest.fixture
    def matcher(self) -> KeywordMatcher:
        """Erstellt eine KeywordMatcher-Instanz."""
        return KeywordMatcher()

    def test_extract_accounting_keywords(self, matcher: KeywordMatcher):
        """Extrahiert Buchhaltungs-Keywords aus Job-Text."""
        keywords = matcher.extract_keywords_from_text(SAMPLE_JOB_TEXT_ACCOUNTING)

        assert "sap" in keywords
        assert "datev" in keywords
        assert "bilanzbuchhalter" in keywords
        assert "debitorenbuchhaltung" in keywords
        assert "kreditorenbuchhaltung" in keywords
        assert "hgb" in keywords
        assert "ifrs" in keywords

    def test_extract_technical_keywords(self, matcher: KeywordMatcher):
        """Extrahiert technische Keywords aus Job-Text."""
        keywords = matcher.extract_keywords_from_text(SAMPLE_JOB_TEXT_TECHNICAL)

        assert "elektriker" in keywords
        assert "sps" in keywords
        assert "elektroniker" in keywords
        assert "simatic" in keywords
        assert "knx" in keywords
        assert "bma" in keywords
        assert "führerschein klasse b" in keywords

    def test_extract_keywords_empty_text(self, matcher: KeywordMatcher):
        """Leerer Text liefert leere Liste."""
        keywords = matcher.extract_keywords_from_text("")
        assert keywords == []

    def test_extract_keywords_none_text(self, matcher: KeywordMatcher):
        """None-Text liefert leere Liste."""
        keywords = matcher.extract_keywords_from_text(None)
        assert keywords == []

    def test_extract_keywords_no_match(self, matcher: KeywordMatcher):
        """Text ohne Keywords liefert leere Liste."""
        keywords = matcher.extract_keywords_from_text("Lorem ipsum dolor sit amet.")
        assert keywords == []

    def test_find_matching_keywords(self, matcher: KeywordMatcher):
        """Findet übereinstimmende Keywords."""
        candidate_skills = ["SAP", "DATEV", "Buchhaltung", "Excel"]
        job_text = SAMPLE_JOB_TEXT_ACCOUNTING

        matched = matcher.find_matching_keywords(candidate_skills, job_text)

        assert "sap" in matched
        assert "datev" in matched
        # Excel ist kein Match (nicht im Job-Text als Keyword)

    def test_find_matching_keywords_case_insensitive(self, matcher: KeywordMatcher):
        """Matching ist case-insensitive."""
        candidate_skills = ["sap", "DATEV", "Buchhaltung"]
        job_text = "Wir suchen jemanden mit SAP und datev Kenntnissen."

        matched = matcher.find_matching_keywords(candidate_skills, job_text)

        assert "sap" in matched
        assert "datev" in matched

    def test_find_matching_keywords_empty_skills(self, matcher: KeywordMatcher):
        """Leere Skills-Liste liefert leere Matches."""
        matched = matcher.find_matching_keywords([], SAMPLE_JOB_TEXT_ACCOUNTING)
        assert matched == []

    def test_find_matching_keywords_empty_job_text(self, matcher: KeywordMatcher):
        """Leerer Job-Text liefert leere Matches."""
        matched = matcher.find_matching_keywords(["SAP", "DATEV"], "")
        assert matched == []

    def test_find_matching_keywords_none_values(self, matcher: KeywordMatcher):
        """None-Werte liefern leere Matches."""
        assert matcher.find_matching_keywords(None, SAMPLE_JOB_TEXT_ACCOUNTING) == []
        assert matcher.find_matching_keywords(["SAP"], None) == []

    def test_calculate_score_full_match(self, matcher: KeywordMatcher):
        """Vollständiger Match ergibt Score 1.0."""
        matched = ["sap", "datev", "buchhaltung"]
        score = matcher.calculate_score(matched, total_skills=3)
        assert score == 1.0

    def test_calculate_score_partial_match(self, matcher: KeywordMatcher):
        """Teilweiser Match ergibt anteiligen Score."""
        matched = ["sap", "datev"]
        score = matcher.calculate_score(matched, total_skills=4)
        assert score == 0.5

    def test_calculate_score_no_match(self, matcher: KeywordMatcher):
        """Kein Match ergibt Score 0.0."""
        score = matcher.calculate_score([], total_skills=5)
        assert score == 0.0

    def test_calculate_score_zero_skills(self, matcher: KeywordMatcher):
        """Keine Skills ergibt Score 0.0."""
        score = matcher.calculate_score(["sap"], total_skills=0)
        assert score == 0.0

    def test_calculate_score_capped_at_one(self, matcher: KeywordMatcher):
        """Score wird auf 1.0 begrenzt."""
        # Mehr Matches als Skills (theoretisch)
        score = matcher.calculate_score(["sap", "datev", "buchhaltung"], total_skills=2)
        assert score == 1.0

    def test_match_full_workflow(self, matcher: KeywordMatcher):
        """Vollständiger Matching-Workflow."""
        candidate_skills = ["SAP", "DATEV", "Buchhaltung", "Excel"]
        job_text = SAMPLE_JOB_TEXT_ACCOUNTING

        result = matcher.match(candidate_skills, job_text)

        assert isinstance(result, KeywordMatchResult)
        assert len(result.matched_keywords) > 0
        assert result.total_candidate_skills == 4
        assert 0.0 <= result.keyword_score <= 1.0
        assert result.match_count == len(result.matched_keywords)

    def test_match_with_empty_skills(self, matcher: KeywordMatcher):
        """Match mit leeren Skills."""
        result = matcher.match([], SAMPLE_JOB_TEXT_ACCOUNTING)

        assert result.matched_keywords == []
        assert result.total_candidate_skills == 0
        assert result.keyword_score == 0.0

    def test_match_with_whitespace_skills(self, matcher: KeywordMatcher):
        """Match mit Whitespace in Skills."""
        candidate_skills = ["  SAP  ", " DATEV ", "  "]
        result = matcher.match(candidate_skills, SAMPLE_JOB_TEXT_ACCOUNTING)

        # Leere Skills werden gefiltert
        assert result.total_candidate_skills == 2

    def test_extract_job_requirements(self, matcher: KeywordMatcher):
        """Extrahiert strukturierte Anforderungen aus Job-Text."""
        requirements = matcher.extract_job_requirements(SAMPLE_JOB_TEXT_ACCOUNTING)

        assert "software" in requirements
        assert "tasks" in requirements
        assert "qualifications" in requirements
        assert "technical" in requirements

        assert "sap" in requirements["software"]
        assert "datev" in requirements["software"]
        assert "bilanzbuchhalter" in requirements["qualifications"]

    def test_extract_job_requirements_empty(self, matcher: KeywordMatcher):
        """Leerer Job-Text liefert leere Kategorien."""
        requirements = matcher.extract_job_requirements("")

        assert requirements == {
            "software": [],
            "tasks": [],
            "qualifications": [],
            "technical": [],
        }


class TestMatchModel:
    """Tests für das Match-Model."""

    def test_match_is_ai_checked_true(self):
        """Match mit AI-Check ist als geprüft markiert."""
        match = MatchFactory.create(
            ai_checked_at=datetime.now(timezone.utc),
            ai_score=0.85,
        )
        assert match.is_ai_checked is True

    def test_match_is_ai_checked_false(self):
        """Match ohne AI-Check ist nicht als geprüft markiert."""
        match = MatchFactory.create(ai_checked_at=None)
        assert match.is_ai_checked is False

    def test_match_is_excellent_true(self):
        """Match mit ≤5km und ≥3 Keywords ist exzellent."""
        match = MatchFactory.create(
            distance_km=3.5,
            matched_keywords=["SAP", "DATEV", "Buchhaltung", "Excel"],
        )
        assert match.is_excellent is True

    def test_match_is_excellent_false_distance(self):
        """Match mit >5km ist nicht exzellent."""
        match = MatchFactory.create(
            distance_km=6.0,
            matched_keywords=["SAP", "DATEV", "Buchhaltung", "Excel"],
        )
        assert match.is_excellent is False

    def test_match_is_excellent_false_keywords(self):
        """Match mit <3 Keywords ist nicht exzellent."""
        match = MatchFactory.create(
            distance_km=3.5,
            matched_keywords=["SAP", "DATEV"],
        )
        assert match.is_excellent is False

    def test_match_is_excellent_boundary_distance(self):
        """Match mit genau 5km ist exzellent."""
        match = MatchFactory.create(
            distance_km=5.0,
            matched_keywords=["SAP", "DATEV", "Buchhaltung"],
        )
        assert match.is_excellent is True

    def test_match_is_excellent_boundary_keywords(self):
        """Match mit genau 3 Keywords ist exzellent."""
        match = MatchFactory.create(
            distance_km=4.0,
            matched_keywords=["SAP", "DATEV", "Buchhaltung"],
        )
        assert match.is_excellent is True

    def test_match_is_excellent_none_distance(self):
        """Match ohne Distanz ist nicht exzellent."""
        match = MatchFactory.create(
            distance_km=None,
            matched_keywords=["SAP", "DATEV", "Buchhaltung", "Excel"],
        )
        assert match.is_excellent is False

    def test_match_is_excellent_none_keywords(self):
        """Match ohne Keywords ist nicht exzellent."""
        match = MatchFactory.create(
            distance_km=3.5,
            matched_keywords=None,
        )
        assert match.is_excellent is False

    def test_match_status_default(self):
        """Default-Status ist NEW."""
        match = MatchFactory.create()
        assert match.status == MatchStatus.NEW

    def test_match_status_values(self):
        """Alle Status-Werte sind valide."""
        assert MatchStatus.NEW.value == "new"
        assert MatchStatus.AI_CHECKED.value == "ai_checked"
        assert MatchStatus.PRESENTED.value == "presented"
        assert MatchStatus.REJECTED.value == "rejected"
        assert MatchStatus.PLACED.value == "placed"


class TestDistanceMatching:
    """Tests für Distanz-basiertes Matching."""

    def test_distance_within_radius(self):
        """Distanz innerhalb von 25km ist valide."""
        match = MatchFactory.create(distance_km=20.0)
        assert match.distance_km <= 25

    def test_distance_at_boundary(self):
        """Distanz genau bei 25km ist valide."""
        match = MatchFactory.create(distance_km=25.0)
        assert match.distance_km == 25.0

    def test_distance_over_radius(self):
        """Distanz über 25km (wird normalerweise nicht gespeichert)."""
        match = MatchFactory.create(distance_km=30.0)
        assert match.distance_km > 25


class TestKeywordConstants:
    """Tests für Keyword-Konstanten."""

    def test_accounting_keywords_not_empty(self):
        """Buchhaltungs-Keywords sind definiert."""
        assert len(ACCOUNTING_KEYWORDS) > 0
        assert "sap" in ACCOUNTING_KEYWORDS
        assert "datev" in ACCOUNTING_KEYWORDS
        assert "buchhaltung" in ACCOUNTING_KEYWORDS

    def test_technical_keywords_not_empty(self):
        """Technische Keywords sind definiert."""
        assert len(TECHNICAL_KEYWORDS) > 0
        assert "elektriker" in TECHNICAL_KEYWORDS
        assert "sps" in TECHNICAL_KEYWORDS

    def test_keywords_lowercase(self):
        """Alle Keywords sind lowercase."""
        for keyword in ACCOUNTING_KEYWORDS:
            assert keyword == keyword.lower()

        for keyword in TECHNICAL_KEYWORDS:
            assert keyword == keyword.lower()
