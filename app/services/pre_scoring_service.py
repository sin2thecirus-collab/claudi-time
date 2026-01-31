"""Pre-Scoring Service - Berechnet Vor-Scores für Matches.

Stufe 2 des Hotlisten-Systems:
- Vergleicht Kandidat ↔ Job anhand von:
  1. Kategorie-Übereinstimmung (FINANCE/ENGINEERING)
  2. Stadt-Übereinstimmung
  3. Job-Title-Ähnlichkeit
  4. Keyword-Score (aus bestehendem Matching)
  5. Distanz (aus bestehendem Matching)

Der Pre-Score ist KOSTENLOS (kein OpenAI) und dient als Filter
vor dem teuren DeepMatch (OpenAI).

Score-Bereich: 0.0 – 100.0
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.candidate import Candidate
from app.models.job import Job
from app.models.match import Match
from app.services.categorization_service import HotlistCategory

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# GEWICHTUNGEN
# ═══════════════════════════════════════════════════════════════

# Maximale Punkte pro Komponente (Summe = 100)
WEIGHT_CATEGORY: float = 30.0       # Gleiche Kategorie
WEIGHT_CITY: float = 25.0           # Gleiche Stadt
WEIGHT_JOB_TITLE: float = 20.0      # Gleicher normalisierter Job-Title
WEIGHT_KEYWORDS: float = 15.0       # Keyword-Score (0-1 → 0-15)
WEIGHT_DISTANCE: float = 10.0       # Distanz (0-25km → 10-0)


# ═══════════════════════════════════════════════════════════════
# DATENKLASSEN
# ═══════════════════════════════════════════════════════════════

@dataclass
class PreScoreBreakdown:
    """Aufschlüsselung des Pre-Scores."""
    category_score: float
    city_score: float
    job_title_score: float
    keyword_score: float
    distance_score: float
    total: float

    @property
    def is_good_match(self) -> bool:
        """Pre-Score >= 50 ist ein guter Match."""
        return self.total >= 50.0


@dataclass
class PreScoringResult:
    """Ergebnis einer Batch Pre-Scoring Operation."""
    total_matches: int
    scored: int
    skipped: int
    avg_score: float


# ═══════════════════════════════════════════════════════════════
# SERVICE
# ═══════════════════════════════════════════════════════════════

class PreScoringService:
    """
    Berechnet Pre-Scores für Kandidat-Job-Matches.

    Verwendet nur lokale Daten (keine API-Calls).
    """

    def __init__(self, db: AsyncSession):
        self.db = db

    # ──────────────────────────────────────────────────
    # Score-Berechnung
    # ──────────────────────────────────────────────────

    def calculate_pre_score(
        self,
        candidate: Candidate,
        job: Job,
        match: Match,
    ) -> PreScoreBreakdown:
        """
        Berechnet den Pre-Score für ein Kandidat-Job-Paar.

        Args:
            candidate: Kandidat
            job: Job
            match: Bestehendes Match-Objekt (für distance_km, keyword_score)

        Returns:
            PreScoreBreakdown mit Einzelwerten und Gesamt-Score
        """
        # 1. Kategorie-Übereinstimmung (30 Punkte)
        category_score = 0.0
        if (
            candidate.hotlist_category
            and job.hotlist_category
            and candidate.hotlist_category == job.hotlist_category
            and candidate.hotlist_category != HotlistCategory.SONSTIGE
        ):
            category_score = WEIGHT_CATEGORY

        # 2. Stadt-Übereinstimmung (25 Punkte)
        city_score = 0.0
        if candidate.hotlist_city and job.hotlist_city:
            if candidate.hotlist_city.lower() == job.hotlist_city.lower():
                city_score = WEIGHT_CITY

        # 3. Job-Title-Ähnlichkeit (20 Punkte) — Array-Intersection
        job_title_score = 0.0
        cand_titles = candidate.hotlist_job_titles or ([candidate.hotlist_job_title] if candidate.hotlist_job_title else [])
        job_titles = job.hotlist_job_titles or ([job.hotlist_job_title] if job.hotlist_job_title else [])
        if cand_titles and job_titles:
            cand_set = {t.lower() for t in cand_titles if t}
            job_set = {t.lower() for t in job_titles if t}
            if cand_set & job_set:  # Mindestens 1 gemeinsamer Titel
                job_title_score = WEIGHT_JOB_TITLE

        # 4. Keyword-Score (15 Punkte, aus bestehendem Matching)
        keyword_score = 0.0
        if match.keyword_score is not None and match.keyword_score > 0:
            # keyword_score ist 0.0 – 1.0, skalieren auf 0 – 15
            keyword_score = min(match.keyword_score, 1.0) * WEIGHT_KEYWORDS

        # 5. Distanz (10 Punkte, invers: näher = besser)
        distance_score = 0.0
        if match.distance_km is not None:
            if match.distance_km <= 5:
                distance_score = WEIGHT_DISTANCE  # Volle Punkte bei ≤5km
            elif match.distance_km <= 25:
                # Linear abfallend: 5km = 10, 25km = 0
                distance_score = WEIGHT_DISTANCE * (1 - (match.distance_km - 5) / 20)
            # > 25km = 0 Punkte

        total = category_score + city_score + job_title_score + keyword_score + distance_score

        return PreScoreBreakdown(
            category_score=round(category_score, 1),
            city_score=round(city_score, 1),
            job_title_score=round(job_title_score, 1),
            keyword_score=round(keyword_score, 1),
            distance_score=round(distance_score, 1),
            total=round(total, 1),
        )

    # ──────────────────────────────────────────────────
    # Einzelnes Match scoren
    # ──────────────────────────────────────────────────

    async def score_match(self, match_id) -> PreScoreBreakdown | None:
        """
        Berechnet den Pre-Score für ein einzelnes Match.

        Returns:
            PreScoreBreakdown oder None bei Fehler
        """
        result = await self.db.execute(
            select(Match, Candidate, Job)
            .join(Candidate, Match.candidate_id == Candidate.id)
            .join(Job, Match.job_id == Job.id)
            .where(Match.id == match_id)
        )
        row = result.first()
        if not row:
            return None

        match, candidate, job = row
        breakdown = self.calculate_pre_score(candidate, job, match)

        # Score auf Match speichern
        match.pre_score = breakdown.total
        await self.db.commit()

        return breakdown

    # ──────────────────────────────────────────────────
    # Batch-Scoring: Alle Matches einer Kategorie/Stadt
    # ──────────────────────────────────────────────────

    async def score_matches_for_category(
        self,
        category: str,
        city: str | None = None,
        job_title: str | None = None,
        force: bool = False,
    ) -> PreScoringResult:
        """
        Berechnet Pre-Scores für alle Matches einer Kategorie.

        Args:
            category: FINANCE oder ENGINEERING
            city: Optional: nur Matches in dieser Stadt
            job_title: Optional: nur Matches mit diesem Beruf (Kandidat UND Job)
            force: True = auch bereits gescorte Matches neu bewerten
        """
        from sqlalchemy import or_

        # Query: Matches mit Kandidaten und Jobs der gleichen Kategorie
        query = (
            select(Match, Candidate, Job)
            .join(Candidate, Match.candidate_id == Candidate.id)
            .join(Job, Match.job_id == Job.id)
            .where(
                and_(
                    Candidate.hotlist_category == category,
                    Job.hotlist_category == category,
                    Candidate.deleted_at.is_(None),
                    Job.deleted_at.is_(None),
                )
            )
        )

        if city:
            query = query.where(Candidate.hotlist_city == city)

        if job_title:
            query = query.where(
                or_(
                    Candidate.hotlist_job_titles.any(job_title),
                    Candidate.hotlist_job_title == job_title,
                )
            ).where(
                or_(
                    Job.hotlist_job_titles.any(job_title),
                    Job.hotlist_job_title == job_title,
                )
            )

        if not force:
            query = query.where(Match.pre_score.is_(None))

        result = await self.db.execute(query)
        rows = result.all()

        total = len(rows)
        scored = 0
        skipped = 0
        score_sum = 0.0

        for match, candidate, job in rows:
            try:
                breakdown = self.calculate_pre_score(candidate, job, match)
                match.pre_score = breakdown.total
                score_sum += breakdown.total
                scored += 1
            except Exception as e:
                logger.error(f"Pre-Scoring Fehler für Match {match.id}: {e}")
                skipped += 1

        await self.db.commit()

        avg = score_sum / scored if scored > 0 else 0.0

        logger.info(
            f"Pre-Scoring für {category}"
            f"{f' / {city}' if city else ''}: "
            f"{scored}/{total} gescort, Ø {avg:.1f}"
        )

        return PreScoringResult(
            total_matches=total,
            scored=scored,
            skipped=skipped,
            avg_score=round(avg, 1),
        )

    async def score_all_matches(self, force: bool = False) -> dict:
        """
        Berechnet Pre-Scores für ALLE Matches (FINANCE + ENGINEERING).

        Returns:
            Dict mit Ergebnissen pro Kategorie
        """
        finance_result = await self.score_matches_for_category(
            HotlistCategory.FINANCE, force=force
        )
        engineering_result = await self.score_matches_for_category(
            HotlistCategory.ENGINEERING, force=force
        )

        return {
            "finance": {
                "total": finance_result.total_matches,
                "scored": finance_result.scored,
                "avg_score": finance_result.avg_score,
            },
            "engineering": {
                "total": engineering_result.total_matches,
                "scored": engineering_result.scored,
                "avg_score": engineering_result.avg_score,
            },
        }
