"""Statistics Service - Aggregiert Statistiken für das Dashboard.

Dieser Service bietet:
- Dashboard-Statistiken (Jobs, Kandidaten, KI-Checks, etc.)
- Filter-Nutzungs-Tracking
- Problem-Erkennung (Jobs ohne Matches, Kandidaten ohne Adresse)
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.candidate import Candidate
from app.models.job import Job
from app.models.match import Match, MatchStatus
from app.models.statistics import DailyStatistics, FilterUsage

logger = logging.getLogger(__name__)


@dataclass
class TopFilter:
    """Meistgenutzter Filter."""

    filter_type: str
    filter_value: str
    usage_count: int


@dataclass
class DashboardStats:
    """Statistiken für das Dashboard."""

    # Aktuelle Zählungen
    jobs_active: int
    candidates_active: int
    candidates_total: int
    matches_total: int

    # KI-Nutzung (Zeitraum)
    ai_checks_count: int
    ai_checks_cost_usd: float

    # Vermittlungen (Zeitraum)
    matches_presented: int
    matches_placed: int

    # Durchschnittswerte
    avg_ai_score: float | None
    avg_distance_km: float | None

    # Top-Filter
    top_filters: list[TopFilter]

    # Probleme
    jobs_without_matches: int
    candidates_without_address: int


class StatisticsService:
    """Service für Statistiken und Auswertungen."""

    def __init__(self, db: AsyncSession):
        """Initialisiert den Service.

        Args:
            db: Async Database Session
        """
        self.db = db

    async def get_dashboard_stats(self, days: int = 30) -> DashboardStats:
        """Aggregiert Statistiken für das Dashboard.

        Args:
            days: Zeitraum in Tagen für zeitraumbezogene Statistiken

        Returns:
            DashboardStats mit allen relevanten Werten
        """
        now = datetime.now(timezone.utc)
        period_start = now - timedelta(days=days)

        # Parallele Abfragen für Performance
        (
            jobs_active,
            candidates_active,
            candidates_total,
            matches_total,
            ai_checks_count,
            ai_checks_cost_usd,
            matches_presented,
            matches_placed,
            avg_ai_score,
            avg_distance_km,
            top_filters,
            jobs_without_matches,
            candidates_without_address,
        ) = await self._gather_all_stats(period_start)

        return DashboardStats(
            jobs_active=jobs_active,
            candidates_active=candidates_active,
            candidates_total=candidates_total,
            matches_total=matches_total,
            ai_checks_count=ai_checks_count,
            ai_checks_cost_usd=ai_checks_cost_usd,
            matches_presented=matches_presented,
            matches_placed=matches_placed,
            avg_ai_score=avg_ai_score,
            avg_distance_km=avg_distance_km,
            top_filters=top_filters,
            jobs_without_matches=jobs_without_matches,
            candidates_without_address=candidates_without_address,
        )

    async def _gather_all_stats(
        self, period_start: datetime
    ) -> tuple[int, int, int, int, int, float, int, int, float | None, float | None, list[TopFilter], int, int]:
        """Sammelt alle Statistiken."""
        # Jobs aktiv (nicht gelöscht, nicht abgelaufen)
        jobs_active = await self._count_active_jobs()

        # Kandidaten
        candidates_active = await self._count_active_candidates()
        candidates_total = await self._count_total_candidates()

        # Matches gesamt
        matches_total = await self._count_total_matches()

        # KI-Checks im Zeitraum
        ai_checks_count, ai_checks_cost_usd = await self._get_ai_stats(period_start)

        # Vermittlungen im Zeitraum
        matches_presented = await self._count_matches_by_status(
            [MatchStatus.PRESENTED, MatchStatus.PLACED], period_start
        )
        matches_placed = await self._count_matches_by_status(
            [MatchStatus.PLACED], period_start
        )

        # Durchschnittswerte
        avg_ai_score = await self._get_avg_ai_score()
        avg_distance_km = await self._get_avg_distance()

        # Top-Filter
        top_filters = await self._get_top_filters(limit=5)

        # Probleme
        jobs_without_matches = await self._count_jobs_without_matches()
        candidates_without_address = await self._count_candidates_without_address()

        return (
            jobs_active,
            candidates_active,
            candidates_total,
            matches_total,
            ai_checks_count,
            ai_checks_cost_usd,
            matches_presented,
            matches_placed,
            avg_ai_score,
            avg_distance_km,
            top_filters,
            jobs_without_matches,
            candidates_without_address,
        )

    async def _count_active_jobs(self) -> int:
        """Zählt aktive Jobs (nicht gelöscht, nicht abgelaufen)."""
        now = datetime.now(timezone.utc)
        query = select(func.count(Job.id)).where(
            and_(
                Job.deleted_at.is_(None),
                or_(Job.expires_at.is_(None), Job.expires_at > now),
            )
        )
        result = await self.db.execute(query)
        return result.scalar() or 0

    async def _count_active_candidates(self) -> int:
        """Zählt aktive Kandidaten (nicht hidden, in den letzten 30 Tagen aktualisiert)."""
        threshold = datetime.now(timezone.utc) - timedelta(days=30)
        query = select(func.count(Candidate.id)).where(
            and_(
                Candidate.hidden.is_(False),
                Candidate.updated_at >= threshold,
            )
        )
        result = await self.db.execute(query)
        return result.scalar() or 0

    async def _count_total_candidates(self) -> int:
        """Zählt alle Kandidaten (nicht hidden)."""
        query = select(func.count(Candidate.id)).where(
            Candidate.hidden.is_(False)
        )
        result = await self.db.execute(query)
        return result.scalar() or 0

    async def _count_total_matches(self) -> int:
        """Zählt alle Matches."""
        query = select(func.count(Match.id))
        result = await self.db.execute(query)
        return result.scalar() or 0

    async def _get_ai_stats(self, period_start: datetime) -> tuple[int, float]:
        """Holt KI-Check-Statistiken für den Zeitraum."""
        # Zähle KI-Checks aus Matches
        query = select(func.count(Match.id)).where(
            and_(
                Match.ai_checked_at.is_not(None),
                Match.ai_checked_at >= period_start,
            )
        )
        result = await self.db.execute(query)
        count = result.scalar() or 0

        # Kosten aus daily_statistics summieren
        query = select(func.coalesce(func.sum(DailyStatistics.ai_checks_cost_usd), 0)).where(
            DailyStatistics.date >= period_start.date()
        )
        result = await self.db.execute(query)
        cost = result.scalar() or 0.0

        return count, float(cost)

    async def _count_matches_by_status(
        self, statuses: list[MatchStatus], period_start: datetime
    ) -> int:
        """Zählt Matches nach Status im Zeitraum."""
        query = select(func.count(Match.id)).where(
            and_(
                Match.status.in_(statuses),
                Match.updated_at >= period_start,
            )
        )
        result = await self.db.execute(query)
        return result.scalar() or 0

    async def _get_avg_ai_score(self) -> float | None:
        """Berechnet den durchschnittlichen KI-Score."""
        query = select(func.avg(Match.ai_score)).where(
            Match.ai_score.is_not(None)
        )
        result = await self.db.execute(query)
        avg = result.scalar()
        return float(avg) if avg is not None else None

    async def _get_avg_distance(self) -> float | None:
        """Berechnet die durchschnittliche Distanz."""
        query = select(func.avg(Match.distance_km)).where(
            Match.distance_km.is_not(None)
        )
        result = await self.db.execute(query)
        avg = result.scalar()
        return float(avg) if avg is not None else None

    async def _get_top_filters(self, limit: int = 5) -> list[TopFilter]:
        """Holt die meistgenutzten Filter."""
        query = (
            select(
                FilterUsage.filter_type,
                FilterUsage.filter_value,
                func.count(FilterUsage.id).label("usage_count"),
            )
            .group_by(FilterUsage.filter_type, FilterUsage.filter_value)
            .order_by(func.count(FilterUsage.id).desc())
            .limit(limit)
        )
        result = await self.db.execute(query)
        rows = result.all()

        return [
            TopFilter(
                filter_type=row.filter_type,
                filter_value=row.filter_value,
                usage_count=row.usage_count,
            )
            for row in rows
        ]

    async def _count_jobs_without_matches(self) -> int:
        """Zählt Jobs ohne Matches."""
        # Subquery für Jobs mit Matches
        has_matches = (
            select(Match.job_id)
            .distinct()
            .scalar_subquery()
        )

        now = datetime.now(timezone.utc)
        query = select(func.count(Job.id)).where(
            and_(
                Job.deleted_at.is_(None),
                or_(Job.expires_at.is_(None), Job.expires_at > now),
                Job.id.not_in(select(Match.job_id).distinct()),
            )
        )
        result = await self.db.execute(query)
        return result.scalar() or 0

    async def _count_candidates_without_address(self) -> int:
        """Zählt Kandidaten ohne gültige Adresse (Koordinaten)."""
        query = select(func.count(Candidate.id)).where(
            and_(
                Candidate.hidden.is_(False),
                Candidate.address_coords.is_(None),
            )
        )
        result = await self.db.execute(query)
        return result.scalar() or 0

    # ==================== Filter-Tracking ====================

    async def record_filter_usage(
        self, filter_type: str, filter_value: str
    ) -> None:
        """Speichert eine Filter-Nutzung.

        Args:
            filter_type: Art des Filters (z.B. "city", "skill", "industry")
            filter_value: Wert des Filters (z.B. "Hamburg", "SAP")
        """
        usage = FilterUsage(
            filter_type=filter_type,
            filter_value=filter_value,
        )
        self.db.add(usage)
        await self.db.commit()

    # ==================== KI-Kosten-Tracking ====================

    async def record_ai_check(self, count: int, cost_usd: float) -> None:
        """Speichert KI-Check-Statistiken.

        Args:
            count: Anzahl der durchgeführten Checks
            cost_usd: Kosten in USD
        """
        today = date.today()

        # Existierenden Eintrag suchen oder erstellen
        query = select(DailyStatistics).where(DailyStatistics.date == today)
        result = await self.db.execute(query)
        stats = result.scalar_one_or_none()

        if stats:
            stats.ai_checks_count += count
            stats.ai_checks_cost_usd += cost_usd
        else:
            stats = DailyStatistics(
                date=today,
                ai_checks_count=count,
                ai_checks_cost_usd=cost_usd,
            )
            self.db.add(stats)

        await self.db.commit()

    # ==================== Tägliche Aggregation ====================

    async def aggregate_daily_stats(self) -> None:
        """Aggregiert und speichert die täglichen Statistiken.

        Wird vom nächtlichen Cron-Job aufgerufen.
        """
        today = date.today()

        # Aktuelle Zählungen
        jobs_active = await self._count_active_jobs()
        jobs_total = await self._count_total_jobs()
        candidates_active = await self._count_active_candidates()
        candidates_total = await self._count_total_candidates()
        matches_total = await self._count_total_matches()

        # Durchschnittswerte
        avg_ai_score = await self._get_avg_ai_score()
        avg_distance_km = await self._get_avg_distance()

        # Vermittlungen heute
        today_start = datetime.combine(today, datetime.min.time()).replace(
            tzinfo=timezone.utc
        )
        matches_presented = await self._count_matches_by_status(
            [MatchStatus.PRESENTED], today_start
        )
        matches_placed = await self._count_matches_by_status(
            [MatchStatus.PLACED], today_start
        )

        # Existierenden Eintrag suchen oder erstellen
        query = select(DailyStatistics).where(DailyStatistics.date == today)
        result = await self.db.execute(query)
        stats = result.scalar_one_or_none()

        if stats:
            stats.jobs_total = jobs_total
            stats.jobs_active = jobs_active
            stats.candidates_total = candidates_total
            stats.candidates_active = candidates_active
            stats.matches_total = matches_total
            stats.avg_ai_score = avg_ai_score
            stats.avg_distance_km = avg_distance_km
            stats.matches_presented = matches_presented
            stats.matches_placed = matches_placed
        else:
            stats = DailyStatistics(
                date=today,
                jobs_total=jobs_total,
                jobs_active=jobs_active,
                candidates_total=candidates_total,
                candidates_active=candidates_active,
                matches_total=matches_total,
                avg_ai_score=avg_ai_score,
                avg_distance_km=avg_distance_km,
                matches_presented=matches_presented,
                matches_placed=matches_placed,
            )
            self.db.add(stats)

        await self.db.commit()
        logger.info(f"Tägliche Statistiken für {today} aggregiert")

    async def _count_total_jobs(self) -> int:
        """Zählt alle Jobs (nicht gelöscht)."""
        query = select(func.count(Job.id)).where(Job.deleted_at.is_(None))
        result = await self.db.execute(query)
        return result.scalar() or 0

    # ==================== Problem-Listen ====================

    async def get_jobs_without_matches(self, limit: int = 20) -> list[Any]:
        """Gibt Jobs ohne Matches zurück.

        Args:
            limit: Maximale Anzahl

        Returns:
            Liste von Jobs ohne Matches
        """
        now = datetime.now(timezone.utc)

        # Subquery für Jobs mit Matches
        jobs_with_matches = select(Match.job_id).distinct()

        query = (
            select(Job)
            .where(
                and_(
                    Job.deleted_at.is_(None),
                    or_(Job.expires_at.is_(None), Job.expires_at > now),
                    Job.id.not_in(jobs_with_matches),
                )
            )
            .order_by(Job.created_at.desc())
            .limit(limit)
        )

        result = await self.db.execute(query)
        return list(result.scalars().all())

    async def get_candidates_without_address(self, limit: int = 20) -> list[Any]:
        """Gibt Kandidaten ohne gültige Adresse zurück.

        Args:
            limit: Maximale Anzahl

        Returns:
            Liste von Kandidaten ohne Koordinaten
        """
        query = (
            select(Candidate)
            .where(
                and_(
                    Candidate.hidden.is_(False),
                    Candidate.address_coords.is_(None),
                )
            )
            .order_by(Candidate.created_at.desc())
            .limit(limit)
        )

        result = await self.db.execute(query)
        return list(result.scalars().all())
