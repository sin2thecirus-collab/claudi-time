"""Tests für CRUD-Operationen."""

from datetime import date, datetime, timedelta, timezone

import pytest

from app.models.candidate import Candidate
from app.models.job import Job
from app.models.match import Match, MatchStatus
from tests.conftest import (
    CandidateFactory,
    JobFactory,
    MatchFactory,
    create_multiple_candidates,
    create_multiple_jobs,
)


class TestJobModel:
    """Tests für das Job-Model."""

    def test_job_creation(self):
        """Job kann erstellt werden."""
        job = JobFactory.create()

        assert job.id is not None
        assert job.company_name == "Test GmbH"
        assert job.position == "Buchhalter/in"
        assert job.city == "Hamburg"

    def test_job_is_deleted_false(self):
        """Job ohne deleted_at ist nicht gelöscht."""
        job = JobFactory.create(deleted_at=None)
        assert job.is_deleted is False

    def test_job_is_deleted_true(self):
        """Job mit deleted_at ist gelöscht."""
        job = JobFactory.create(deleted_at=datetime.now(timezone.utc))
        assert job.is_deleted is True

    def test_job_is_expired_false_no_date(self):
        """Job ohne expires_at ist nicht abgelaufen."""
        job = JobFactory.create(expires_at=None)
        assert job.is_expired is False

    def test_job_is_expired_false_future(self):
        """Job mit zukünftigem expires_at ist nicht abgelaufen."""
        future = datetime.now(timezone.utc) + timedelta(days=30)
        job = JobFactory.create(expires_at=future)
        assert job.is_expired is False

    def test_job_is_expired_true_past(self):
        """Job mit vergangenem expires_at ist abgelaufen."""
        past = datetime.now(timezone.utc) - timedelta(days=1)
        job = JobFactory.create(expires_at=past)
        assert job.is_expired is True

    def test_job_display_city_work_location(self):
        """display_city bevorzugt work_location_city."""
        job = JobFactory.create(city="Hamburg", work_location_city="München")
        assert job.display_city == "München"

    def test_job_display_city_fallback(self):
        """display_city fällt auf city zurück."""
        job = JobFactory.create(city="Hamburg", work_location_city=None)
        assert job.display_city == "Hamburg"

    def test_job_display_city_unknown(self):
        """display_city zeigt Unbekannt wenn beide leer."""
        job = JobFactory.create(city=None, work_location_city=None)
        assert job.display_city == "Unbekannt"


class TestCandidateModel:
    """Tests für das Candidate-Model."""

    def test_candidate_creation(self):
        """Kandidat kann erstellt werden."""
        candidate = CandidateFactory.create()

        assert candidate.id is not None
        assert candidate.first_name == "Max"
        assert candidate.last_name == "Mustermann"
        assert candidate.city == "Hamburg"

    def test_candidate_full_name(self):
        """full_name kombiniert Vor- und Nachname."""
        candidate = CandidateFactory.create(first_name="Max", last_name="Mustermann")
        assert candidate.full_name == "Max Mustermann"

    def test_candidate_full_name_first_only(self):
        """full_name nur mit Vorname."""
        candidate = CandidateFactory.create(first_name="Max", last_name=None)
        assert candidate.full_name == "Max"

    def test_candidate_full_name_last_only(self):
        """full_name nur mit Nachname."""
        candidate = CandidateFactory.create(first_name=None, last_name="Mustermann")
        assert candidate.full_name == "Mustermann"

    def test_candidate_full_name_empty(self):
        """full_name ohne Namen ist Unbekannt."""
        candidate = CandidateFactory.create(first_name=None, last_name=None)
        assert candidate.full_name == "Unbekannt"

    def test_candidate_age_calculation(self):
        """age wird korrekt aus birth_date berechnet."""
        # Kandidat geboren 1985
        candidate = CandidateFactory.create(birth_date=date(1985, 5, 15))
        age = candidate.age

        # Alter sollte zwischen 39 und 41 sein (abhängig vom aktuellen Datum)
        assert age is not None
        assert 35 <= age <= 45

    def test_candidate_age_none(self):
        """age ist None wenn birth_date fehlt."""
        candidate = CandidateFactory.create(birth_date=None)
        assert candidate.age is None

    def test_candidate_hidden_default(self):
        """hidden ist standardmäßig False."""
        candidate = CandidateFactory.create()
        assert candidate.hidden is False

    def test_candidate_hidden_true(self):
        """hidden kann auf True gesetzt werden."""
        candidate = CandidateFactory.create(hidden=True)
        assert candidate.hidden is True

    def test_candidate_skills(self):
        """Skills werden korrekt gespeichert."""
        skills = ["SAP", "DATEV", "Excel"]
        candidate = CandidateFactory.create(skills=skills)
        assert candidate.skills == skills


class TestMatchModel:
    """Tests für das Match-Model."""

    def test_match_creation(self):
        """Match kann erstellt werden."""
        match = MatchFactory.create()

        assert match.id is not None
        assert match.job_id is not None
        assert match.candidate_id is not None

    def test_match_distance(self):
        """Distanz wird korrekt gespeichert."""
        match = MatchFactory.create(distance_km=12.5)
        assert match.distance_km == 12.5

    def test_match_keyword_score(self):
        """Keyword-Score wird korrekt gespeichert."""
        match = MatchFactory.create(keyword_score=0.75)
        assert match.keyword_score == 0.75

    def test_match_matched_keywords(self):
        """Gematchte Keywords werden korrekt gespeichert."""
        keywords = ["SAP", "DATEV"]
        match = MatchFactory.create(matched_keywords=keywords)
        assert match.matched_keywords == keywords

    def test_match_ai_data(self):
        """AI-Daten werden korrekt gespeichert."""
        match = MatchFactory.create(
            ai_score=0.85,
            ai_explanation="Sehr gute Passung",
            ai_strengths=["SAP-Erfahrung", "Langjährige Berufserfahrung"],
            ai_weaknesses=["Keine IFRS-Kenntnisse"],
            ai_checked_at=datetime.now(timezone.utc),
        )

        assert match.ai_score == 0.85
        assert match.ai_explanation == "Sehr gute Passung"
        assert len(match.ai_strengths) == 2
        assert len(match.ai_weaknesses) == 1
        assert match.ai_checked_at is not None

    def test_match_status_transitions(self):
        """Status-Übergänge funktionieren."""
        match = MatchFactory.create(status=MatchStatus.NEW)
        assert match.status == MatchStatus.NEW

        match.status = MatchStatus.AI_CHECKED
        assert match.status == MatchStatus.AI_CHECKED

        match.status = MatchStatus.PRESENTED
        assert match.status == MatchStatus.PRESENTED

        match.status = MatchStatus.PLACED
        assert match.status == MatchStatus.PLACED

    def test_match_placed_data(self):
        """Vermittlungsdaten werden korrekt gespeichert."""
        now = datetime.now(timezone.utc)
        match = MatchFactory.create(
            status=MatchStatus.PLACED,
            placed_at=now,
            placed_notes="Erfolgreich vermittelt am 01.01.2026",
        )

        assert match.placed_at == now
        assert match.placed_notes == "Erfolgreich vermittelt am 01.01.2026"


class TestFactories:
    """Tests für die Test-Factories."""

    def test_job_factory_defaults(self):
        """JobFactory erstellt Job mit Standardwerten."""
        job = JobFactory.create()

        assert job.company_name == "Test GmbH"
        assert job.position == "Buchhalter/in"
        assert job.city == "Hamburg"
        assert job.content_hash is not None

    def test_job_factory_custom_values(self):
        """JobFactory akzeptiert benutzerdefinierte Werte."""
        job = JobFactory.create(
            company_name="Firma ABC",
            position="Controller",
            city="München",
        )

        assert job.company_name == "Firma ABC"
        assert job.position == "Controller"
        assert job.city == "München"

    def test_candidate_factory_defaults(self):
        """CandidateFactory erstellt Kandidat mit Standardwerten."""
        candidate = CandidateFactory.create()

        assert candidate.first_name == "Max"
        assert candidate.last_name == "Mustermann"
        assert candidate.city == "Hamburg"
        assert candidate.skills is not None

    def test_candidate_factory_custom_values(self):
        """CandidateFactory akzeptiert benutzerdefinierte Werte."""
        candidate = CandidateFactory.create(
            first_name="Anna",
            last_name="Schmidt",
            city="München",
            skills=["Excel", "Word"],
        )

        assert candidate.first_name == "Anna"
        assert candidate.last_name == "Schmidt"
        assert candidate.city == "München"
        assert candidate.skills == ["Excel", "Word"]

    def test_match_factory_defaults(self):
        """MatchFactory erstellt Match mit Standardwerten."""
        match = MatchFactory.create()

        assert match.distance_km == 5.0
        assert match.keyword_score == 0.75
        assert match.status == MatchStatus.NEW

    def test_create_multiple_jobs(self):
        """create_multiple_jobs erstellt mehrere Jobs."""
        jobs = create_multiple_jobs(count=3)

        assert len(jobs) == 3
        assert jobs[0].company_name == "Firma 1"
        assert jobs[1].company_name == "Firma 2"
        assert jobs[2].company_name == "Firma 3"
        # Jeder Job hat eindeutigen content_hash
        hashes = [job.content_hash for job in jobs]
        assert len(set(hashes)) == 3

    def test_create_multiple_candidates(self):
        """create_multiple_candidates erstellt mehrere Kandidaten."""
        candidates = create_multiple_candidates(count=3)

        assert len(candidates) == 3
        assert candidates[0].first_name == "Vorname1"
        assert candidates[1].first_name == "Vorname2"
        assert candidates[2].first_name == "Vorname3"
        # Jeder Kandidat hat eindeutige crm_id
        crm_ids = [c.crm_id for c in candidates]
        assert len(set(crm_ids)) == 3


class TestSoftDelete:
    """Tests für Soft-Delete Funktionalität."""

    def test_soft_delete_job(self):
        """Job kann soft-deleted werden."""
        job = JobFactory.create(deleted_at=None)
        assert job.is_deleted is False

        job.deleted_at = datetime.now(timezone.utc)
        assert job.is_deleted is True

    def test_restore_soft_deleted_job(self):
        """Soft-deleted Job kann wiederhergestellt werden."""
        job = JobFactory.create(deleted_at=datetime.now(timezone.utc))
        assert job.is_deleted is True

        job.deleted_at = None
        assert job.is_deleted is False


class TestCandidateHide:
    """Tests für Kandidaten-Ausblenden Funktionalität."""

    def test_hide_candidate(self):
        """Kandidat kann ausgeblendet werden."""
        candidate = CandidateFactory.create(hidden=False)
        assert candidate.hidden is False

        candidate.hidden = True
        assert candidate.hidden is True

    def test_unhide_candidate(self):
        """Kandidat kann wieder eingeblendet werden."""
        candidate = CandidateFactory.create(hidden=True)
        assert candidate.hidden is True

        candidate.hidden = False
        assert candidate.hidden is False
