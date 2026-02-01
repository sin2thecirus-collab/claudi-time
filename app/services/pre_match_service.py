"""Pre-Match Service - Automatische Generierung von Pre-Match-Listen.

Unterschied zu Quick-Match:
- Quick-Match: Stadt-basiert (Kandidat.hotlist_city == Job.hotlist_city), manuell
- Pre-Match: Distanz-basiert (Kandidat <30km zum Job, egal welche Stadt), automatisch

Generiert Matches fuer alle Beruf+Stadt Kombinationen einer Kategorie (z.B. FINANCE).
Verwendet PostGIS fuer praezise Distanzberechnung.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from geoalchemy2.functions import ST_Distance, ST_DWithin
from sqlalchemy import ARRAY, String, and_, cast, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.candidate import Candidate
from app.models.job import Job
from app.models.match import Match, MatchStatus
from app.services.keyword_matcher import keyword_matcher

logger = logging.getLogger(__name__)

# Maximale Distanz fuer Pre-Matches
PRE_MATCH_MAX_DISTANCE_KM = 30
METERS_PER_KM = 1000

# Max. Kandidaten pro Job (die naechsten nach Entfernung)
MAX_CANDIDATES_PER_JOB = 15


# ═══════════════════════════════════════════════════════════════
# DATENKLASSEN
# ═══════════════════════════════════════════════════════════════


@dataclass
class PreMatchGroup:
    """Eine Beruf+Stadt Gruppe fuer die Uebersicht."""

    job_title: str
    city: str
    match_count: int
    avg_distance_km: float
    avg_pre_score: float
    close_matches: int  # Anzahl mit <5km


@dataclass
class PreMatchDetail:
    """Ein einzelner Match in der Detail-Ansicht."""

    match_id: UUID
    candidate_id: UUID
    candidate_name: str
    candidate_title: str  # hotlist_job_title
    candidate_titles: list[str]  # hotlist_job_titles
    candidate_phone: str | None
    candidate_email: str | None
    candidate_city: str | None
    job_id: UUID
    job_company: str
    job_position: str
    job_url: str | None
    distance_km: float | None
    pre_score: float | None
    ai_score: float | None
    ai_explanation: str | None
    ai_strengths: list[str]
    ai_weaknesses: list[str]
    user_feedback: str | None
    feedback_note: str | None
    has_cv: bool
    cv_stored_path: str | None
    # Quick-AI Score (Phase C)
    quick_score: int | None = None
    quick_reason: str | None = None
    # Split-View Daten
    job_text: str | None = None
    candidate_work_history: list[dict] | None = None
    candidate_education: list[dict] | None = None
    candidate_further_education: list[dict] | None = None
    candidate_it_skills: list[str] | None = None
    candidate_languages: list[dict] | None = None
    candidate_current_position: str | None = None
    candidate_skills: list[str] | None = None


@dataclass
class PreMatchGenerateResult:
    """Ergebnis einer Pre-Match-Generierung."""

    combos_processed: int = 0
    matches_created: int = 0
    matches_updated: int = 0
    matches_skipped: int = 0
    errors: list[str] = field(default_factory=list)


# ═══════════════════════════════════════════════════════════════
# SERVICE
# ═══════════════════════════════════════════════════════════════


class PreMatchService:
    """
    Service fuer automatische Pre-Match-Generierung.

    Findet alle Kandidat-Job-Paare innerhalb von 30km
    und erstellt/aktualisiert Match-Eintraege.
    """

    def __init__(self, db: AsyncSession):
        self.db = db

    # ──────────────────────────────────────────────────
    # Generierung
    # ──────────────────────────────────────────────────

    async def purge_and_regenerate(
        self,
        category: str = "FINANCE",
        progress_callback: Any = None,
    ) -> PreMatchGenerateResult:
        """
        Loescht ALLE alten Matches einer Kategorie und generiert komplett neu.

        Wird verwendet wenn sich die Filter-Logik geaendert hat und die
        alten Matches nicht mehr gueltig sind.
        """
        from sqlalchemy import delete

        if progress_callback:
            progress_callback("purge", "Loesche alte Matches...")

        # Alle Matches dieser Kategorie loeschen (ueber Job-Kategorie)
        job_ids_query = select(Job.id).where(
            and_(
                Job.hotlist_category == category,
                Job.deleted_at.is_(None),
            )
        )
        delete_query = delete(Match).where(
            Match.job_id.in_(job_ids_query)
        )
        delete_result = await self.db.execute(delete_query)
        deleted = delete_result.rowcount
        await self.db.commit()

        logger.info(f"Pre-Match Purge: {deleted} alte Matches geloescht fuer {category}")

        if progress_callback:
            progress_callback("purge", f"{deleted} alte Matches geloescht, generiere neu...")

        # Neu generieren mit den neuen Filtern
        return await self.generate_all(
            category=category,
            progress_callback=progress_callback,
        )

    async def generate_all(
        self,
        category: str = "FINANCE",
        progress_callback: Any = None,
    ) -> PreMatchGenerateResult:
        """
        Generiert Pre-Matches fuer ALLE Beruf+Stadt Kombos einer Kategorie.

        1. Findet alle aktiven Jobs mit Koordinaten + hotlist_job_titles
        2. Fuer jeden Job: Finde passende Kandidaten (gefiltert!)
        3. Erstelle Match-Eintraege (falls nicht vorhanden)
        4. Berechne Pre-Score

        Filter pro Job:
        - Gleiche Kategorie + gleicher Berufstitel
        - Innerhalb 30km Luftlinie
        - Keine Fuehrungskraefte
        - Max 15 naechste Kandidaten pro Job

        Args:
            category: Kategorie (z.B. "FINANCE")
            progress_callback: Optional callback(step, detail)

        Returns:
            PreMatchGenerateResult mit Statistiken
        """
        result = PreMatchGenerateResult()

        # Alle aktiven Jobs der Kategorie mit Koordinaten laden
        jobs_query = select(Job).where(
            and_(
                Job.hotlist_category == category,
                Job.location_coords.is_not(None),
                Job.deleted_at.is_(None),
                Job.hotlist_job_titles.is_not(None),
            )
        )
        jobs_result = await self.db.execute(jobs_query)
        jobs = jobs_result.scalars().all()

        logger.info(f"Pre-Match: {len(jobs)} Jobs in Kategorie {category}")

        if progress_callback:
            progress_callback("pre_match", f"{len(jobs)} Jobs gefunden")

        # Alle Kandidaten der Kategorie mit Koordinaten vorladen
        candidates_query = select(Candidate).where(
            and_(
                Candidate.hotlist_category == category,
                Candidate.address_coords.is_not(None),
                Candidate.hidden == False,  # noqa: E712
                Candidate.deleted_at.is_(None),
                Candidate.hotlist_job_titles.is_not(None),
            )
        )
        candidates_result = await self.db.execute(candidates_query)
        candidates = candidates_result.scalars().all()

        logger.info(f"Pre-Match: {len(candidates)} Kandidaten in Kategorie {category}")

        # Fuer jeden Job: Distanz-basierte Matches finden
        batch_count = 0
        for job in jobs:
            try:
                created = await self._generate_matches_for_job(job, category)
                result.matches_created += created
                result.combos_processed += 1
                batch_count += 1

                # Batch commit alle 50 Jobs
                if batch_count >= 50:
                    await self.db.commit()
                    batch_count = 0
                    if progress_callback:
                        progress_callback(
                            "pre_match",
                            f"{result.combos_processed}/{len(jobs)} Jobs, "
                            f"{result.matches_created} neue Matches",
                        )

            except Exception as e:
                logger.error(f"Pre-Match Fehler bei Job {job.id}: {e}")
                result.errors.append(f"Job {job.id}: {str(e)[:100]}")

        # Finaler Commit
        await self.db.commit()

        # Pre-Scores berechnen fuer neue Matches ohne Score
        scored = await self._score_unscored_matches(category)

        logger.info(
            f"Pre-Match Generierung abgeschlossen: "
            f"{result.combos_processed} Jobs, {result.matches_created} neue Matches, "
            f"{scored} Scores berechnet"
        )

        return result

    async def _generate_matches_for_job(self, job: Job, category: str) -> int:
        """
        Generiert Matches fuer einen einzelnen Job.

        Filter-Kette (nur passende Kandidaten):
        1. Gleiche Kategorie + aktiv + hat Koordinaten + hat Jobtitel
        2. Innerhalb von 30km (PostGIS)
        3. Mindestens 1 gemeinsamer Berufstitel
        4. KEIN Fuehrungskraft (is_leadership = false)
        5. Nur die Top 15 naechsten nach Entfernung

        Returns:
            Anzahl neu erstellter Matches
        """
        if not job.location_coords or not job.hotlist_job_titles:
            return 0

        job_titles = job.hotlist_job_titles

        # Distanz-Expression
        distance_expr = func.ST_Distance(
            Candidate.address_coords,
            job.location_coords,
            True,  # use_spheroid
        )

        # ── Filter 1-4: Passende Kandidaten finden ──
        candidates_query = select(
            Candidate.id,
            (distance_expr / METERS_PER_KM).label("distance_km"),
        ).where(
            and_(
                # Basis-Filter
                Candidate.hotlist_category == category,
                Candidate.address_coords.is_not(None),
                Candidate.hidden == False,  # noqa: E712
                Candidate.deleted_at.is_(None),
                Candidate.hotlist_job_titles.is_not(None),
                # Filter 2: Distanz max 30km
                func.ST_DWithin(
                    Candidate.address_coords,
                    job.location_coords,
                    PRE_MATCH_MAX_DISTANCE_KM * METERS_PER_KM,
                    True,
                ),
                # Filter 3: Mindestens 1 gemeinsamer Berufstitel
                Candidate.hotlist_job_titles.op("&&")(cast(job_titles, ARRAY(String))),
                # Filter 4: Keine Fuehrungskraefte
                or_(
                    Candidate.classification_data.is_(None),
                    Candidate.classification_data["is_leadership"].astext != "true",
                ),
            )
        ).order_by(
            # Filter 5: Sortiere nach Naehe — die naechsten zuerst
            (distance_expr / METERS_PER_KM).asc()
        ).limit(
            # Filter 5: Nur die Top N naechsten Kandidaten pro Job
            MAX_CANDIDATES_PER_JOB
        )

        result = await self.db.execute(candidates_query)
        nearby_candidates = result.all()

        if not nearby_candidates:
            return 0

        # Bestehende Matches fuer diesen Job laden
        existing_query = select(Match.candidate_id).where(Match.job_id == job.id)
        existing_result = await self.db.execute(existing_query)
        existing_candidate_ids = {row[0] for row in existing_result}

        created = 0
        for candidate_id, distance_km in nearby_candidates:
            if candidate_id in existing_candidate_ids:
                continue  # Match existiert bereits

            # Neuen Match erstellen
            new_match = Match(
                job_id=job.id,
                candidate_id=candidate_id,
                distance_km=round(distance_km, 2),
                status=MatchStatus.NEW,
            )
            self.db.add(new_match)
            created += 1

        return created

    async def _score_unscored_matches(self, category: str) -> int:
        """Berechnet Pre-Scores fuer alle Matches ohne Score in der Kategorie."""
        from app.services.pre_scoring_service import PreScoringService

        scorer = PreScoringService(self.db)
        result = await scorer.score_matches_for_category(category, force=False)
        return result.scored

    # ──────────────────────────────────────────────────
    # Abfragen: Uebersicht
    # ──────────────────────────────────────────────────

    async def get_overview(self, category: str = "FINANCE") -> list[PreMatchGroup]:
        """
        Gibt die Uebersicht zurueck: Gruppiert nach Beruf + Stadt.

        Berechnet fuer jede Kombo:
        - Match-Count
        - Durchschnittliche Entfernung
        - Durchschnittlicher Pre-Score
        - Anzahl <5km Matches

        Args:
            category: Kategorie (z.B. "FINANCE")

        Returns:
            Liste von PreMatchGroup, sortiert nach Beruf, dann Match-Count DESC
        """
        # Wir gruppieren nach Job.hotlist_job_title (primary) + Job.hotlist_city
        query = (
            select(
                Job.hotlist_job_title,
                Job.hotlist_city,
                func.count(Match.id).label("match_count"),
                func.coalesce(func.avg(Match.distance_km), 0).label("avg_distance"),
                func.coalesce(func.avg(Match.pre_score), 0).label("avg_pre_score"),
                func.count(
                    func.nullif(
                        Match.distance_km > 5,
                        True,
                    )
                ).label("close_count"),
            )
            .join(Match, Match.job_id == Job.id)
            .join(Candidate, Match.candidate_id == Candidate.id)
            .where(
                and_(
                    Job.hotlist_category == category,
                    Job.deleted_at.is_(None),
                    Job.hotlist_job_title.is_not(None),
                    Job.hotlist_city.is_not(None),
                    Candidate.hidden == False,  # noqa: E712
                    Candidate.deleted_at.is_(None),
                    Candidate.hotlist_category == category,
                )
            )
            .group_by(Job.hotlist_job_title, Job.hotlist_city)
            .having(func.count(Match.id) > 0)
            .order_by(Job.hotlist_job_title, func.count(Match.id).desc())
        )

        result = await self.db.execute(query)
        rows = result.all()

        groups = []
        for row in rows:
            # close_count berechnen: Matches mit distance_km <= 5
            close_query = (
                select(func.count(Match.id))
                .join(Job, Match.job_id == Job.id)
                .join(Candidate, Match.candidate_id == Candidate.id)
                .where(
                    and_(
                        Job.hotlist_job_title == row[0],
                        Job.hotlist_city == row[1],
                        Job.hotlist_category == category,
                        Job.deleted_at.is_(None),
                        Candidate.hidden == False,  # noqa: E712
                        Candidate.deleted_at.is_(None),
                        Match.distance_km.is_not(None),
                        Match.distance_km <= 5,
                    )
                )
            )
            close_result = await self.db.execute(close_query)
            close_count = close_result.scalar() or 0

            groups.append(
                PreMatchGroup(
                    job_title=row[0],
                    city=row[1],
                    match_count=row[2],
                    avg_distance_km=round(float(row[3]), 1),
                    avg_pre_score=round(float(row[4]), 1),
                    close_matches=close_count,
                )
            )

        return groups

    # ──────────────────────────────────────────────────
    # Abfragen: Detail
    # ──────────────────────────────────────────────────

    async def get_detail(
        self,
        job_title: str,
        city: str,
        category: str = "FINANCE",
        sort_by: str = "distance",  # "distance" oder "score"
    ) -> list[PreMatchDetail]:
        """
        Gibt alle Matches fuer eine Beruf+Stadt Kombination zurueck.

        Args:
            job_title: Berufstitel (z.B. "Bilanzbuchhalter/in")
            city: Stadt (z.B. "Muenchen")
            category: Kategorie
            sort_by: Sortierung ("distance" = ASC, "score" = DESC)

        Returns:
            Liste von PreMatchDetail, sortiert nach Entfernung oder Score
        """
        query = (
            select(Match, Candidate, Job)
            .join(Candidate, Match.candidate_id == Candidate.id)
            .join(Job, Match.job_id == Job.id)
            .where(
                and_(
                    Job.hotlist_job_title == job_title,
                    Job.hotlist_city == city,
                    Job.hotlist_category == category,
                    Job.deleted_at.is_(None),
                    Candidate.hidden == False,  # noqa: E712
                    Candidate.deleted_at.is_(None),
                    Candidate.hotlist_category == category,
                )
            )
        )

        if sort_by == "score":
            query = query.order_by(
                Match.ai_score.desc().nullslast(),
                Match.pre_score.desc().nullslast(),
                Match.distance_km.asc().nullslast(),
            )
        else:
            query = query.order_by(
                Match.distance_km.asc().nullslast(),
                Match.pre_score.desc().nullslast(),
            )

        # Limit um Seite nicht zu ueberlasten
        query = query.limit(500)

        result = await self.db.execute(query)
        rows = result.all()

        details = []
        for match, candidate, job in rows:
            # work_history normalisieren (kann list oder None sein)
            wh = candidate.work_history
            if wh and isinstance(wh, list):
                work_history = wh
            else:
                work_history = []

            # education normalisieren
            edu = candidate.education
            if edu and isinstance(edu, list):
                education = edu
            else:
                education = []

            # further_education normalisieren
            fe = candidate.further_education
            if fe and isinstance(fe, list):
                further_education = fe
            else:
                further_education = []

            # languages normalisieren
            langs = candidate.languages
            if langs and isinstance(langs, list):
                languages = langs
            else:
                languages = []

            details.append(
                PreMatchDetail(
                    match_id=match.id,
                    candidate_id=candidate.id,
                    candidate_name=candidate.full_name,
                    candidate_title=candidate.hotlist_job_title or "",
                    candidate_titles=candidate.hotlist_job_titles or [],
                    candidate_phone=candidate.phone,
                    candidate_email=candidate.email,
                    candidate_city=candidate.city or candidate.hotlist_city,
                    job_id=job.id,
                    job_company=job.company_name,
                    job_position=job.position,
                    job_url=job.job_url,
                    distance_km=match.distance_km,
                    pre_score=match.pre_score,
                    ai_score=match.ai_score,
                    ai_explanation=match.ai_explanation,
                    ai_strengths=match.ai_strengths or [],
                    ai_weaknesses=match.ai_weaknesses or [],
                    user_feedback=match.user_feedback,
                    feedback_note=match.feedback_note,
                    has_cv=bool(candidate.cv_stored_path or candidate.cv_text),
                    cv_stored_path=candidate.cv_stored_path,
                    # Quick-AI Score
                    quick_score=int(match.quick_score) if match.quick_score is not None else None,
                    quick_reason=match.quick_reason,
                    # Split-View Daten
                    job_text=job.job_text,
                    candidate_work_history=work_history,
                    candidate_education=education,
                    candidate_further_education=further_education,
                    candidate_it_skills=candidate.it_skills or [],
                    candidate_languages=languages,
                    candidate_current_position=candidate.current_position,
                    candidate_skills=candidate.skills or [],
                )
            )

        return details

    # ──────────────────────────────────────────────────
    # Statistiken
    # ──────────────────────────────────────────────────

    async def get_stats(self, category: str = "FINANCE") -> dict:
        """Gibt Statistiken fuer die Uebersichtsseite zurueck."""
        # Gesamt-Matches in Kategorie
        total_query = (
            select(func.count(Match.id))
            .join(Job, Match.job_id == Job.id)
            .join(Candidate, Match.candidate_id == Candidate.id)
            .where(
                and_(
                    Job.hotlist_category == category,
                    Job.deleted_at.is_(None),
                    Candidate.hidden == False,  # noqa: E712
                    Candidate.deleted_at.is_(None),
                )
            )
        )
        total_result = await self.db.execute(total_query)
        total = total_result.scalar() or 0

        # Anzahl verschiedene Berufe
        berufe_query = (
            select(func.count(func.distinct(Job.hotlist_job_title)))
            .join(Match, Match.job_id == Job.id)
            .where(
                and_(
                    Job.hotlist_category == category,
                    Job.deleted_at.is_(None),
                    Job.hotlist_job_title.is_not(None),
                )
            )
        )
        berufe_result = await self.db.execute(berufe_query)
        berufe = berufe_result.scalar() or 0

        # Matches < 5km
        close_query = (
            select(func.count(Match.id))
            .join(Job, Match.job_id == Job.id)
            .join(Candidate, Match.candidate_id == Candidate.id)
            .where(
                and_(
                    Job.hotlist_category == category,
                    Job.deleted_at.is_(None),
                    Candidate.hidden == False,  # noqa: E712
                    Candidate.deleted_at.is_(None),
                    Match.distance_km.is_not(None),
                    Match.distance_km <= 5,
                )
            )
        )
        close_result = await self.db.execute(close_query)
        close = close_result.scalar() or 0

        # Durchschnittliche Distanz
        avg_dist_query = (
            select(func.avg(Match.distance_km))
            .join(Job, Match.job_id == Job.id)
            .join(Candidate, Match.candidate_id == Candidate.id)
            .where(
                and_(
                    Job.hotlist_category == category,
                    Job.deleted_at.is_(None),
                    Candidate.hidden == False,  # noqa: E712
                    Candidate.deleted_at.is_(None),
                    Match.distance_km.is_not(None),
                )
            )
        )
        avg_dist_result = await self.db.execute(avg_dist_query)
        avg_dist = avg_dist_result.scalar()

        return {
            "total_matches": total,
            "berufe_count": berufe,
            "close_matches": close,
            "avg_distance_km": round(float(avg_dist), 1) if avg_dist else 0,
        }
