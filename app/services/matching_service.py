"""Matching Service - Kern-Matching-Logik für Jobs und Kandidaten."""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from geoalchemy2.functions import ST_Distance, ST_DWithin, ST_SetSRID, ST_MakePoint
from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.candidate import Candidate
from app.models.job import Job
from app.models.match import Match, MatchStatus
from app.services.keyword_matcher import keyword_matcher

logger = logging.getLogger(__name__)

# Konstanten
MAX_DISTANCE_KM = 25  # Maximale Distanz für Matches
METERS_PER_KM = 1000


@dataclass
class MatchingResult:
    """Ergebnis einer Matching-Operation."""

    job_id: uuid.UUID
    total_candidates_checked: int
    matches_created: int
    matches_updated: int
    matches_deleted: int


@dataclass
class BatchMatchingResult:
    """Ergebnis einer Batch-Matching-Operation."""

    jobs_processed: int
    total_matches_created: int
    total_matches_updated: int
    total_matches_deleted: int
    errors: list[str]


class MatchingService:
    """Service für Matching-Operationen zwischen Jobs und Kandidaten."""

    def __init__(self, db: AsyncSession):
        """
        Initialisiert den MatchingService.

        Args:
            db: Async-Datenbank-Session
        """
        self.db = db

    async def calculate_matches_for_job(
        self,
        job_id: uuid.UUID,
        delete_old_matches: bool = False,
    ) -> MatchingResult:
        """
        Berechnet Matches für einen einzelnen Job.

        Findet alle Kandidaten im Umkreis von 25km und berechnet
        Distanz und Keyword-Score für jeden.

        Args:
            job_id: ID des Jobs
            delete_old_matches: Wenn True, werden bestehende Matches ohne KI-Check gelöscht

        Returns:
            MatchingResult mit Statistiken
        """
        # Job laden
        job = await self.db.get(Job, job_id)
        if not job:
            raise ValueError(f"Job mit ID {job_id} nicht gefunden")

        if job.is_deleted:
            raise ValueError(f"Job mit ID {job_id} ist gelöscht")

        # Job muss Koordinaten haben
        if not job.location_coords:
            logger.warning(f"Job {job_id} hat keine Koordinaten - übersprungen")
            return MatchingResult(
                job_id=job_id,
                total_candidates_checked=0,
                matches_created=0,
                matches_updated=0,
                matches_deleted=0,
            )

        # Optional: Alte Matches ohne KI-Check löschen
        deleted_count = 0
        if delete_old_matches:
            result = await self.db.execute(
                delete(Match).where(
                    and_(
                        Match.job_id == job_id,
                        Match.ai_checked_at.is_(None),
                        Match.status == MatchStatus.NEW,
                    )
                )
            )
            deleted_count = result.rowcount

        # Kandidaten im Umkreis von 25km finden
        # Verwende PostGIS ST_DWithin für Radius-Filter
        distance_expr = func.ST_Distance(
            Candidate.address_coords,
            job.location_coords,
            True,  # use_spheroid für genauere Berechnung
        )

        candidates_query = select(
            Candidate,
            (distance_expr / METERS_PER_KM).label("distance_km"),
        ).where(
            and_(
                Candidate.address_coords.is_not(None),
                Candidate.hidden == False,  # noqa: E712
                func.ST_DWithin(
                    Candidate.address_coords,
                    job.location_coords,
                    MAX_DISTANCE_KM * METERS_PER_KM,
                    True,  # use_spheroid
                ),
            )
        )

        result = await self.db.execute(candidates_query)
        candidates_with_distance = result.all()

        created_count = 0
        updated_count = 0

        for candidate, distance_km in candidates_with_distance:
            # Keyword-Matching durchführen
            match_result = keyword_matcher.match(
                candidate_skills=candidate.skills,
                job_text=job.job_text,
            )

            # Bestehenden Match prüfen
            existing_match_query = select(Match).where(
                and_(
                    Match.job_id == job_id,
                    Match.candidate_id == candidate.id,
                )
            )
            existing_result = await self.db.execute(existing_match_query)
            existing_match = existing_result.scalar_one_or_none()

            if existing_match:
                # Match aktualisieren (nur wenn noch nicht KI-geprüft)
                if not existing_match.ai_checked_at:
                    existing_match.distance_km = round(distance_km, 2)
                    existing_match.keyword_score = round(match_result.keyword_score, 3)
                    existing_match.matched_keywords = match_result.matched_keywords
                    updated_count += 1
            else:
                # Neuen Match erstellen
                new_match = Match(
                    job_id=job_id,
                    candidate_id=candidate.id,
                    distance_km=round(distance_km, 2),
                    keyword_score=round(match_result.keyword_score, 3),
                    matched_keywords=match_result.matched_keywords,
                    status=MatchStatus.NEW,
                )
                self.db.add(new_match)
                created_count += 1

        await self.db.commit()

        logger.info(
            f"Matching für Job {job_id}: "
            f"{len(candidates_with_distance)} Kandidaten geprüft, "
            f"{created_count} neu, {updated_count} aktualisiert, "
            f"{deleted_count} gelöscht"
        )

        return MatchingResult(
            job_id=job_id,
            total_candidates_checked=len(candidates_with_distance),
            matches_created=created_count,
            matches_updated=updated_count,
            matches_deleted=deleted_count,
        )

    async def calculate_matches_for_candidate(
        self,
        candidate_id: uuid.UUID,
    ) -> int:
        """
        Berechnet Matches für einen einzelnen Kandidaten gegen alle Jobs.

        Nützlich nach CRM-Sync wenn ein neuer Kandidat hinzugefügt wurde.

        Args:
            candidate_id: ID des Kandidaten

        Returns:
            Anzahl der erstellten/aktualisierten Matches
        """
        candidate = await self.db.get(Candidate, candidate_id)
        if not candidate:
            raise ValueError(f"Kandidat mit ID {candidate_id} nicht gefunden")

        if candidate.hidden:
            logger.warning(f"Kandidat {candidate_id} ist versteckt - übersprungen")
            return 0

        if not candidate.address_coords:
            logger.warning(f"Kandidat {candidate_id} hat keine Koordinaten - übersprungen")
            return 0

        # Alle aktiven Jobs im Umkreis finden
        distance_expr = func.ST_Distance(
            Job.location_coords,
            candidate.address_coords,
            True,
        )

        jobs_query = select(
            Job,
            (distance_expr / METERS_PER_KM).label("distance_km"),
        ).where(
            and_(
                Job.location_coords.is_not(None),
                Job.deleted_at.is_(None),
                func.ST_DWithin(
                    Job.location_coords,
                    candidate.address_coords,
                    MAX_DISTANCE_KM * METERS_PER_KM,
                    True,
                ),
            )
        )

        result = await self.db.execute(jobs_query)
        jobs_with_distance = result.all()

        match_count = 0

        for job, distance_km in jobs_with_distance:
            # Keyword-Matching
            match_result = keyword_matcher.match(
                candidate_skills=candidate.skills,
                job_text=job.job_text,
            )

            # Bestehenden Match prüfen
            existing_query = select(Match).where(
                and_(
                    Match.job_id == job.id,
                    Match.candidate_id == candidate_id,
                )
            )
            existing_result = await self.db.execute(existing_query)
            existing_match = existing_result.scalar_one_or_none()

            if existing_match:
                if not existing_match.ai_checked_at:
                    existing_match.distance_km = round(distance_km, 2)
                    existing_match.keyword_score = round(match_result.keyword_score, 3)
                    existing_match.matched_keywords = match_result.matched_keywords
                    match_count += 1
            else:
                new_match = Match(
                    job_id=job.id,
                    candidate_id=candidate_id,
                    distance_km=round(distance_km, 2),
                    keyword_score=round(match_result.keyword_score, 3),
                    matched_keywords=match_result.matched_keywords,
                    status=MatchStatus.NEW,
                )
                self.db.add(new_match)
                match_count += 1

        await self.db.commit()

        logger.info(
            f"Matching für Kandidat {candidate_id}: "
            f"{len(jobs_with_distance)} Jobs geprüft, "
            f"{match_count} Matches erstellt/aktualisiert"
        )

        return match_count

    async def recalculate_all_matches(self) -> BatchMatchingResult:
        """
        Berechnet Matches für alle aktiven Jobs neu.

        Für nächtlichen Cron-Job.

        Returns:
            BatchMatchingResult mit Gesamtstatistiken
        """
        # Alle aktiven Jobs laden
        jobs_query = select(Job).where(
            and_(
                Job.deleted_at.is_(None),
                Job.location_coords.is_not(None),
            )
        )
        result = await self.db.execute(jobs_query)
        jobs = result.scalars().all()

        total_created = 0
        total_updated = 0
        total_deleted = 0
        errors = []

        for job in jobs:
            try:
                match_result = await self.calculate_matches_for_job(
                    job.id,
                    delete_old_matches=False,
                )
                total_created += match_result.matches_created
                total_updated += match_result.matches_updated
                total_deleted += match_result.matches_deleted
            except Exception as e:
                error_msg = f"Fehler bei Job {job.id}: {str(e)}"
                logger.error(error_msg)
                errors.append(error_msg)

        logger.info(
            f"Batch-Matching abgeschlossen: {len(jobs)} Jobs, "
            f"{total_created} neu, {total_updated} aktualisiert, "
            f"{total_deleted} gelöscht, {len(errors)} Fehler"
        )

        return BatchMatchingResult(
            jobs_processed=len(jobs),
            total_matches_created=total_created,
            total_matches_updated=total_updated,
            total_matches_deleted=total_deleted,
            errors=errors,
        )

    async def get_matches_for_job(
        self,
        job_id: uuid.UUID,
        include_hidden: bool = False,
        only_ai_checked: bool = False,
        min_ai_score: float | None = None,
        status: MatchStatus | None = None,
        sort_by: str = "distance",
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[Match], int]:
        """
        Lädt Matches für einen Job mit Filteroptionen.

        Args:
            job_id: ID des Jobs
            include_hidden: Auch versteckte Kandidaten einbeziehen
            only_ai_checked: Nur KI-geprüfte Matches
            min_ai_score: Minimaler KI-Score
            status: Filter nach Status
            sort_by: Sortierung (distance, ai_score, keyword_score, created_at)
            limit: Maximale Anzahl
            offset: Offset für Pagination

        Returns:
            Tuple aus (Liste der Matches, Gesamtanzahl)
        """
        # Basis-Query
        query = (
            select(Match)
            .options(selectinload(Match.candidate))
            .where(Match.job_id == job_id)
        )

        # Filter anwenden
        if not include_hidden:
            query = query.join(Candidate).where(Candidate.hidden == False)  # noqa: E712

        if only_ai_checked:
            query = query.where(Match.ai_checked_at.is_not(None))

        if min_ai_score is not None:
            query = query.where(Match.ai_score >= min_ai_score)

        if status:
            query = query.where(Match.status == status)

        # Zählen
        count_query = select(func.count()).select_from(query.subquery())
        count_result = await self.db.execute(count_query)
        total_count = count_result.scalar()

        # Sortierung
        if sort_by == "ai_score":
            query = query.order_by(Match.ai_score.desc().nullslast())
        elif sort_by == "keyword_score":
            query = query.order_by(Match.keyword_score.desc().nullslast())
        elif sort_by == "created_at":
            query = query.order_by(Match.created_at.desc())
        else:  # distance (default)
            query = query.order_by(Match.distance_km.asc().nullslast())

        # Pagination
        query = query.limit(limit).offset(offset)

        result = await self.db.execute(query)
        matches = result.scalars().all()

        return list(matches), total_count

    async def update_match_status(
        self,
        match_id: uuid.UUID,
        status: MatchStatus,
    ) -> Match | None:
        """
        Aktualisiert den Status eines Matches.

        Args:
            match_id: ID des Matches
            status: Neuer Status

        Returns:
            Aktualisierter Match oder None
        """
        match = await self.db.get(Match, match_id)
        if not match:
            return None

        match.status = status

        if status == MatchStatus.PLACED:
            match.placed_at = datetime.utcnow()

        await self.db.commit()
        await self.db.refresh(match)

        return match

    async def mark_as_placed(
        self,
        match_id: uuid.UUID,
        notes: str | None = None,
    ) -> Match | None:
        """
        Markiert einen Match als vermittelt.

        Args:
            match_id: ID des Matches
            notes: Optionale Notizen zur Vermittlung

        Returns:
            Aktualisierter Match oder None
        """
        match = await self.db.get(Match, match_id)
        if not match:
            return None

        match.status = MatchStatus.PLACED
        match.placed_at = datetime.utcnow()
        match.placed_notes = notes

        await self.db.commit()
        await self.db.refresh(match)

        return match

    async def delete_match(self, match_id: uuid.UUID) -> bool:
        """
        Löscht einen Match.

        Args:
            match_id: ID des Matches

        Returns:
            True wenn gelöscht, False wenn nicht gefunden
        """
        match = await self.db.get(Match, match_id)
        if not match:
            return False

        await self.db.delete(match)
        await self.db.commit()

        return True

    async def batch_delete_matches(self, match_ids: list[uuid.UUID]) -> int:
        """
        Löscht mehrere Matches.

        Args:
            match_ids: Liste der Match-IDs

        Returns:
            Anzahl der gelöschten Matches
        """
        if not match_ids:
            return 0

        result = await self.db.execute(
            delete(Match).where(Match.id.in_(match_ids))
        )
        await self.db.commit()

        return result.rowcount

    async def get_match(self, match_id: uuid.UUID) -> Match | None:
        """
        Lädt einen einzelnen Match.

        Args:
            match_id: ID des Matches

        Returns:
            Match oder None
        """
        query = (
            select(Match)
            .options(
                selectinload(Match.candidate),
                selectinload(Match.job),
            )
            .where(Match.id == match_id)
        )
        result = await self.db.execute(query)
        return result.scalar_one_or_none()

    async def get_excellent_matches(self) -> list[Match]:
        """
        Findet exzellente Matches (≤5km und ≥3 Keywords).

        Returns:
            Liste der exzellenten Matches
        """
        query = (
            select(Match)
            .options(
                selectinload(Match.candidate),
                selectinload(Match.job),
            )
            .where(
                and_(
                    Match.distance_km <= 5,
                    func.array_length(Match.matched_keywords, 1) >= 3,
                    Match.status == MatchStatus.NEW,
                )
            )
            .order_by(Match.created_at.desc())
        )
        result = await self.db.execute(query)
        return list(result.scalars().all())

    async def cleanup_orphaned_matches(self) -> int:
        """
        Entfernt Matches für gelöschte Jobs oder versteckte Kandidaten.

        Returns:
            Anzahl der gelöschten Matches
        """
        # Matches für gelöschte Jobs
        deleted_jobs_subquery = select(Job.id).where(Job.deleted_at.is_not(None))

        result1 = await self.db.execute(
            delete(Match).where(Match.job_id.in_(deleted_jobs_subquery))
        )

        # Matches für versteckte Kandidaten (nur wenn noch nicht KI-geprüft)
        hidden_candidates_subquery = select(Candidate.id).where(
            Candidate.hidden == True  # noqa: E712
        )

        result2 = await self.db.execute(
            delete(Match).where(
                and_(
                    Match.candidate_id.in_(hidden_candidates_subquery),
                    Match.ai_checked_at.is_(None),
                )
            )
        )

        await self.db.commit()

        total_deleted = result1.rowcount + result2.rowcount
        logger.info(f"Cleanup: {total_deleted} verwaiste Matches gelöscht")

        return total_deleted

    async def get_match_statistics(self, job_id: uuid.UUID) -> dict:
        """
        Gibt Statistiken für die Matches eines Jobs zurück.

        Args:
            job_id: ID des Jobs

        Returns:
            Dict mit Statistiken
        """
        # Gesamtanzahl
        total_query = select(func.count()).where(Match.job_id == job_id)
        total_result = await self.db.execute(total_query)
        total = total_result.scalar()

        # Nach Status
        status_query = (
            select(Match.status, func.count())
            .where(Match.job_id == job_id)
            .group_by(Match.status)
        )
        status_result = await self.db.execute(status_query)
        status_counts = {row[0].value: row[1] for row in status_result}

        # KI-geprüft
        ai_checked_query = (
            select(func.count())
            .where(
                and_(
                    Match.job_id == job_id,
                    Match.ai_checked_at.is_not(None),
                )
            )
        )
        ai_checked_result = await self.db.execute(ai_checked_query)
        ai_checked = ai_checked_result.scalar()

        # Durchschnittlicher AI-Score
        avg_score_query = (
            select(func.avg(Match.ai_score))
            .where(
                and_(
                    Match.job_id == job_id,
                    Match.ai_score.is_not(None),
                )
            )
        )
        avg_score_result = await self.db.execute(avg_score_query)
        avg_ai_score = avg_score_result.scalar()

        # Durchschnittliche Distanz
        avg_distance_query = (
            select(func.avg(Match.distance_km))
            .where(Match.job_id == job_id)
        )
        avg_distance_result = await self.db.execute(avg_distance_query)
        avg_distance = avg_distance_result.scalar()

        return {
            "total_matches": total,
            "by_status": status_counts,
            "ai_checked_count": ai_checked,
            "avg_ai_score": round(avg_ai_score, 2) if avg_ai_score else None,
            "avg_distance_km": round(avg_distance, 2) if avg_distance else None,
        }
