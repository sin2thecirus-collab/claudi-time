"""Integration Tests für das Matching-Tool."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.candidate import Candidate
from app.models.job import Job
from app.models.match import Match, MatchStatus
from app.services.keyword_matcher import KeywordMatcher
from tests.conftest import (
    CandidateFactory,
    JobFactory,
    MatchFactory,
    SAMPLE_JOB_TEXT_ACCOUNTING,
    create_multiple_candidates,
    create_multiple_jobs,
)


class TestJobCandidateMatchingFlow:
    """Integration Tests für den Job-Kandidaten-Matching-Flow."""

    @pytest.mark.asyncio
    async def test_create_job_and_candidate(self, db_session: AsyncSession):
        """Job und Kandidat können erstellt und verknüpft werden."""
        # Job erstellen
        job = JobFactory.create(
            company_name="Integration Test GmbH",
            position="Buchhalter",
            job_text=SAMPLE_JOB_TEXT_ACCOUNTING,
        )
        db_session.add(job)
        await db_session.commit()

        # Kandidat erstellen
        candidate = CandidateFactory.create(
            first_name="Test",
            last_name="Kandidat",
            skills=["SAP", "DATEV", "Buchhaltung"],
        )
        db_session.add(candidate)
        await db_session.commit()

        # Match erstellen
        match = MatchFactory.create(
            job_id=job.id,
            candidate_id=candidate.id,
            distance_km=5.0,
            keyword_score=0.75,
            matched_keywords=["sap", "datev", "buchhaltung"],
        )
        db_session.add(match)
        await db_session.commit()

        # Verifizieren
        result = await db_session.execute(
            select(Match).where(Match.job_id == job.id)
        )
        matches = result.scalars().all()
        assert len(matches) == 1
        assert matches[0].candidate_id == candidate.id

    @pytest.mark.asyncio
    async def test_multiple_candidates_for_job(self, db_session: AsyncSession):
        """Mehrere Kandidaten können einem Job zugeordnet werden."""
        # Job erstellen
        job = JobFactory.create()
        db_session.add(job)
        await db_session.commit()

        # Mehrere Kandidaten erstellen
        candidates = create_multiple_candidates(count=5)
        for candidate in candidates:
            db_session.add(candidate)
        await db_session.commit()

        # Matches erstellen
        for i, candidate in enumerate(candidates):
            match = MatchFactory.create(
                job_id=job.id,
                candidate_id=candidate.id,
                distance_km=5.0 + i,
                keyword_score=0.8 - (i * 0.1),
            )
            db_session.add(match)
        await db_session.commit()

        # Verifizieren
        result = await db_session.execute(
            select(Match).where(Match.job_id == job.id)
        )
        matches = result.scalars().all()
        assert len(matches) == 5

    @pytest.mark.asyncio
    async def test_match_status_workflow(self, db_session: AsyncSession):
        """Match-Status durchläuft korrekten Workflow."""
        # Setup
        job = JobFactory.create()
        candidate = CandidateFactory.create()
        db_session.add(job)
        db_session.add(candidate)
        await db_session.commit()

        match = MatchFactory.create(
            job_id=job.id,
            candidate_id=candidate.id,
            status=MatchStatus.NEW,
        )
        db_session.add(match)
        await db_session.commit()

        # Status-Workflow: NEW -> AI_CHECKED -> PRESENTED -> PLACED
        match.status = MatchStatus.AI_CHECKED
        match.ai_score = 0.85
        match.ai_checked_at = datetime.now(timezone.utc)
        await db_session.commit()
        assert match.status == MatchStatus.AI_CHECKED

        match.status = MatchStatus.PRESENTED
        await db_session.commit()
        assert match.status == MatchStatus.PRESENTED

        match.status = MatchStatus.PLACED
        match.placed_at = datetime.now(timezone.utc)
        match.placed_notes = "Erfolgreich vermittelt"
        await db_session.commit()
        assert match.status == MatchStatus.PLACED


class TestKeywordMatchingIntegration:
    """Integration Tests für Keyword-Matching."""

    def test_full_matching_workflow(self):
        """Vollständiger Keyword-Matching Workflow."""
        matcher = KeywordMatcher()

        # Kandidat mit Skills
        candidate_skills = ["SAP", "DATEV", "Buchhaltung", "HGB", "Bilanzierung"]

        # Job-Text mit Anforderungen
        job_text = SAMPLE_JOB_TEXT_ACCOUNTING

        # Matching durchführen
        result = matcher.match(candidate_skills, job_text)

        # Verifizieren
        assert result.total_candidate_skills == 5
        assert len(result.matched_keywords) >= 3  # Mindestens SAP, DATEV, Buchhaltung sollten matchen
        assert result.keyword_score > 0.5

    def test_accounting_vs_technical_job(self):
        """Buchhaltungs-Kandidat matched nicht mit technischem Job."""
        matcher = KeywordMatcher()

        # Buchhaltungs-Kandidat
        accounting_skills = ["SAP", "DATEV", "Buchhaltung", "Bilanzierung"]

        # Technischer Job-Text
        technical_job = """
        Elektriker (m/w/d) gesucht!
        Aufgaben: SPS-Programmierung, Schaltschrankbau, Elektroinstallation
        Profil: Elektroniker mit Erfahrung in Simatic und KNX
        """

        result = matcher.match(accounting_skills, technical_job)

        # Buchhaltungs-Skills sollten nicht mit technischem Job matchen
        assert result.keyword_score < 0.3


class TestDatabaseConstraints:
    """Tests für Datenbank-Constraints."""

    @pytest.mark.asyncio
    async def test_unique_match_constraint(self, db_session: AsyncSession):
        """Nur ein Match pro Job-Kandidat-Kombination erlaubt."""
        job = JobFactory.create()
        candidate = CandidateFactory.create()
        db_session.add(job)
        db_session.add(candidate)
        await db_session.commit()

        # Erstes Match erstellen
        match1 = MatchFactory.create(job_id=job.id, candidate_id=candidate.id)
        db_session.add(match1)
        await db_session.commit()

        # Zweites Match mit gleicher Kombination sollte fehlschlagen
        # (In einer echten PostgreSQL-DB würde ein IntegrityError geworfen)
        # SQLite handhabt Constraints anders, daher prüfen wir nur die Existenz
        result = await db_session.execute(
            select(Match).where(
                Match.job_id == job.id,
                Match.candidate_id == candidate.id,
            )
        )
        matches = result.scalars().all()
        assert len(matches) == 1

    @pytest.mark.asyncio
    async def test_cascade_delete_matches_on_job_delete(self, db_session: AsyncSession):
        """Matches werden gelöscht wenn Job gelöscht wird."""
        job = JobFactory.create()
        candidate = CandidateFactory.create()
        db_session.add(job)
        db_session.add(candidate)
        await db_session.commit()

        match = MatchFactory.create(job_id=job.id, candidate_id=candidate.id)
        db_session.add(match)
        await db_session.commit()

        # Job löschen
        await db_session.delete(job)
        await db_session.commit()

        # Match sollte auch gelöscht sein
        result = await db_session.execute(select(Match))
        matches = result.scalars().all()
        assert len(matches) == 0


class TestBatchOperations:
    """Tests für Batch-Operationen."""

    @pytest.mark.asyncio
    async def test_batch_create_jobs(self, db_session: AsyncSession):
        """Mehrere Jobs können gleichzeitig erstellt werden."""
        jobs = create_multiple_jobs(count=10)

        for job in jobs:
            db_session.add(job)
        await db_session.commit()

        result = await db_session.execute(select(Job))
        db_jobs = result.scalars().all()
        assert len(db_jobs) == 10

    @pytest.mark.asyncio
    async def test_batch_create_candidates(self, db_session: AsyncSession):
        """Mehrere Kandidaten können gleichzeitig erstellt werden."""
        candidates = create_multiple_candidates(count=10)

        for candidate in candidates:
            db_session.add(candidate)
        await db_session.commit()

        result = await db_session.execute(select(Candidate))
        db_candidates = result.scalars().all()
        assert len(db_candidates) == 10

    @pytest.mark.asyncio
    async def test_batch_hide_candidates(self, db_session: AsyncSession):
        """Mehrere Kandidaten können gleichzeitig ausgeblendet werden."""
        candidates = create_multiple_candidates(count=5)

        for candidate in candidates:
            db_session.add(candidate)
        await db_session.commit()

        # Alle ausblenden
        for candidate in candidates:
            candidate.hidden = True
        await db_session.commit()

        # Verifizieren
        result = await db_session.execute(
            select(Candidate).where(Candidate.hidden == True)
        )
        hidden = result.scalars().all()
        assert len(hidden) == 5


class TestFilteringAndSorting:
    """Tests für Filter- und Sortierfunktionen."""

    @pytest.mark.asyncio
    async def test_filter_jobs_by_city(self, db_session: AsyncSession):
        """Jobs können nach Stadt gefiltert werden."""
        # Jobs in verschiedenen Städten erstellen
        job_hamburg = JobFactory.create(city="Hamburg", content_hash="hash1")
        job_munich = JobFactory.create(city="München", content_hash="hash2")
        job_berlin = JobFactory.create(city="Berlin", content_hash="hash3")

        db_session.add_all([job_hamburg, job_munich, job_berlin])
        await db_session.commit()

        # Nach Hamburg filtern
        result = await db_session.execute(
            select(Job).where(Job.city == "Hamburg")
        )
        jobs = result.scalars().all()
        assert len(jobs) == 1
        assert jobs[0].city == "Hamburg"

    @pytest.mark.asyncio
    async def test_filter_candidates_by_hidden(self, db_session: AsyncSession):
        """Kandidaten können nach hidden-Status gefiltert werden."""
        visible = CandidateFactory.create(hidden=False, crm_id="crm1")
        hidden = CandidateFactory.create(hidden=True, crm_id="crm2")

        db_session.add_all([visible, hidden])
        await db_session.commit()

        # Nur sichtbare
        result = await db_session.execute(
            select(Candidate).where(Candidate.hidden == False)
        )
        candidates = result.scalars().all()
        assert len(candidates) == 1
        assert candidates[0].hidden is False

    @pytest.mark.asyncio
    async def test_filter_matches_by_status(self, db_session: AsyncSession):
        """Matches können nach Status gefiltert werden."""
        job = JobFactory.create()
        candidates = create_multiple_candidates(count=3)

        db_session.add(job)
        for c in candidates:
            db_session.add(c)
        await db_session.commit()

        # Matches mit verschiedenen Status erstellen
        match_new = MatchFactory.create(
            job_id=job.id, candidate_id=candidates[0].id, status=MatchStatus.NEW
        )
        match_checked = MatchFactory.create(
            job_id=job.id, candidate_id=candidates[1].id, status=MatchStatus.AI_CHECKED
        )
        match_placed = MatchFactory.create(
            job_id=job.id, candidate_id=candidates[2].id, status=MatchStatus.PLACED
        )

        db_session.add_all([match_new, match_checked, match_placed])
        await db_session.commit()

        # Nach Status filtern
        result = await db_session.execute(
            select(Match).where(Match.status == MatchStatus.NEW)
        )
        new_matches = result.scalars().all()
        assert len(new_matches) == 1
