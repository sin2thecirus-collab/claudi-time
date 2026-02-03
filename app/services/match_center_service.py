"""Match Center Service - Job-zentrische Match-Verwaltung.

Ersetzt das alte Pre-Match-System mit einer uebersichtlichen,
job-zentrierten Ansicht aller Smart-Match-Ergebnisse.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, case, cast, desc, func, Numeric, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.match import Match, MatchStatus

logger = logging.getLogger(__name__)


@dataclass
class JobMatchSummary:
    """Zusammenfassung eines Jobs mit seinen Matches."""

    job_id: UUID
    position: str
    company_name: str
    city: str
    match_count: int
    top_ai_score: float | None
    avg_ai_score: float | None
    new_count: int
    presented_count: int
    created_at: datetime | None


@dataclass
class MatchDetail:
    """Detailansicht eines einzelnen Matches."""

    match_id: UUID
    candidate_id: UUID | None
    candidate_name: str
    candidate_title: str
    candidate_city: str
    ai_score: float | None
    ai_explanation: str | None
    ai_strengths: list[str] | None
    ai_weaknesses: list[str] | None
    distance_km: float | None
    status: str
    matching_method: str | None
    user_feedback: str | None
    feedback_note: str | None
    created_at: datetime | None


class MatchCenterService:
    """Service fuer job-zentrische Match-Verwaltung."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_stats(self, category: str = "FINANCE") -> dict:
        """Holt Uebersichts-Statistiken fuer das Match Center.

        Returns:
            dict mit total_jobs, total_matches, avg_score, placed_count,
            new_count, in_progress_count, archive_count
        """
        from app.models.job import Job

        # Basis-Query: Nur Jobs mit Matches in der richtigen Kategorie
        base = (
            select(
                func.count(func.distinct(Match.job_id)).label("total_jobs"),
                func.count(Match.id).label("total_matches"),
                func.avg(Match.ai_score).label("avg_score"),
                func.sum(case((Match.status == MatchStatus.PLACED, 1), else_=0)).label("placed"),
                func.sum(case((Match.status == MatchStatus.NEW, 1), else_=0)).label("new"),
                func.sum(case((Match.status == MatchStatus.AI_CHECKED, 1), else_=0)).label("ai_checked"),
                func.sum(case((Match.status == MatchStatus.PRESENTED, 1), else_=0)).label("presented"),
                func.sum(case((Match.status == MatchStatus.REJECTED, 1), else_=0)).label("rejected"),
            )
            .select_from(Match)
            .join(Job, Match.job_id == Job.id, isouter=True)
            .where(
                and_(
                    Match.job_id.isnot(None),
                    Match.candidate_id.isnot(None),
                    or_(
                        Job.hotlist_category == category,
                        Job.hotlist_category.is_(None),
                    ),
                )
            )
        )

        result = await self.db.execute(base)
        row = result.one()

        avg = round(float(row.avg_score) * 100, 1) if row.avg_score else 0

        return {
            "total_jobs": int(row.total_jobs or 0),
            "total_matches": int(row.total_matches or 0),
            "avg_score": avg,
            "placed_count": int(row.placed or 0),
            "new_count": int(row.new or 0) + int(row.ai_checked or 0),
            "presented_count": int(row.presented or 0),
            "rejected_count": int(row.rejected or 0),
        }

    async def get_jobs_overview(
        self,
        category: str = "FINANCE",
        stage: str = "new",
        search: str | None = None,
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[list[JobMatchSummary], int]:
        """Holt Jobs gruppiert nach Lifecycle-Stufe.

        Args:
            category: Job-Kategorie (z.B. FINANCE)
            stage: Lifecycle-Stufe (new, in_progress, archive)
            search: Optionale Textsuche in Position/Firma
            page: Seitennummer
            per_page: Ergebnisse pro Seite

        Returns:
            (Liste von JobMatchSummary, Gesamtzahl)
        """
        from app.models.job import Job

        # Subquery: Aggregierte Match-Daten pro Job
        match_agg = (
            select(
                Match.job_id,
                func.count(Match.id).label("match_count"),
                func.max(Match.ai_score).label("top_ai_score"),
                func.avg(Match.ai_score).label("avg_ai_score"),
                func.sum(case((Match.status == MatchStatus.NEW, 1), else_=0)).label("new_count"),
                func.sum(case((Match.status == MatchStatus.AI_CHECKED, 1), else_=0)).label("ai_checked_count"),
                func.sum(case((Match.status == MatchStatus.PRESENTED, 1), else_=0)).label("presented_count"),
                func.sum(case((Match.status == MatchStatus.REJECTED, 1), else_=0)).label("rejected_count"),
                func.sum(case((Match.status == MatchStatus.PLACED, 1), else_=0)).label("placed_count"),
            )
            .where(
                and_(
                    Match.job_id.isnot(None),
                    Match.candidate_id.isnot(None),
                )
            )
            .group_by(Match.job_id)
            .subquery()
        )

        # Haupt-Query: Jobs mit Match-Aggregaten
        query = (
            select(
                Job.id,
                Job.position,
                Job.company_name,
                Job.city,
                Job.created_at,
                match_agg.c.match_count,
                match_agg.c.top_ai_score,
                match_agg.c.avg_ai_score,
                match_agg.c.new_count,
                match_agg.c.ai_checked_count,
                match_agg.c.presented_count,
                match_agg.c.rejected_count,
                match_agg.c.placed_count,
            )
            .join(match_agg, Job.id == match_agg.c.job_id)
            .where(
                and_(
                    Job.deleted_at.is_(None),
                    or_(
                        Job.hotlist_category == category,
                        Job.hotlist_category.is_(None),
                    ),
                )
            )
        )

        # Stage-Filter
        if stage == "new":
            # Neu: Keine Matches mit Status PRESENTED oder hoeher
            query = query.where(
                and_(
                    match_agg.c.presented_count == 0,
                    match_agg.c.placed_count == 0,
                )
            )
        elif stage == "in_progress":
            # In Bearbeitung: Mindestens ein Match PRESENTED, aber nicht alle PLACED/REJECTED
            query = query.where(match_agg.c.presented_count > 0)
        elif stage == "archive":
            # Archiv: Alle Matches REJECTED oder PLACED
            query = query.where(
                and_(
                    (match_agg.c.rejected_count + match_agg.c.placed_count)
                    == match_agg.c.match_count,
                    match_agg.c.match_count > 0,
                )
            )

        # Textsuche
        if search:
            search_term = f"%{search}%"
            query = query.where(
                or_(
                    Job.position.ilike(search_term),
                    Job.company_name.ilike(search_term),
                    Job.city.ilike(search_term),
                )
            )

        # Gesamtzahl ermitteln
        count_query = select(func.count()).select_from(query.subquery())
        total = (await self.db.execute(count_query)).scalar() or 0

        # Sortierung: Bester AI-Score zuerst
        query = query.order_by(desc(match_agg.c.top_ai_score))

        # Pagination
        offset = (page - 1) * per_page
        query = query.limit(per_page).offset(offset)

        result = await self.db.execute(query)
        rows = result.all()

        summaries = []
        for row in rows:
            summaries.append(
                JobMatchSummary(
                    job_id=row.id,
                    position=row.position or "Unbekannte Position",
                    company_name=row.company_name or "Unbekanntes Unternehmen",
                    city=row.city or "",
                    match_count=int(row.match_count or 0),
                    top_ai_score=round(float(row.top_ai_score) * 100, 1) if row.top_ai_score else None,
                    avg_ai_score=round(float(row.avg_ai_score) * 100, 1) if row.avg_ai_score else None,
                    new_count=int(row.new_count or 0) + int(row.ai_checked_count or 0),
                    presented_count=int(row.presented_count or 0),
                    created_at=row.created_at,
                )
            )

        return summaries, total

    async def get_job_matches(
        self,
        job_id: UUID,
        sort_by: str = "ai_score",
        limit: int = 10,
    ) -> list[MatchDetail]:
        """Holt die Top-N Matches fuer einen bestimmten Job.

        Args:
            job_id: Job-UUID
            sort_by: Sortierung (ai_score, distance, created_at)
            limit: Maximale Anzahl

        Returns:
            Liste von MatchDetail
        """
        from app.models.candidate import Candidate

        query = (
            select(
                Match.id,
                Match.candidate_id,
                Match.ai_score,
                Match.ai_explanation,
                Match.ai_strengths,
                Match.ai_weaknesses,
                Match.distance_km,
                Match.status,
                Match.matching_method,
                Match.user_feedback,
                Match.feedback_note,
                Match.created_at,
                Candidate.first_name,
                Candidate.last_name,
                Candidate.hotlist_job_title,
                Candidate.hotlist_city,
            )
            .join(Candidate, Match.candidate_id == Candidate.id, isouter=True)
            .where(
                and_(
                    Match.job_id == job_id,
                    Match.candidate_id.isnot(None),
                )
            )
        )

        # Sortierung
        if sort_by == "distance":
            query = query.order_by(Match.distance_km.asc().nullslast())
        elif sort_by == "created_at":
            query = query.order_by(desc(Match.created_at))
        else:
            query = query.order_by(desc(Match.ai_score))

        query = query.limit(limit)

        result = await self.db.execute(query)
        rows = result.all()

        details = []
        for row in rows:
            name_parts = []
            if row.first_name:
                name_parts.append(row.first_name)
            if row.last_name:
                name_parts.append(row.last_name)
            full_name = " ".join(name_parts) if name_parts else "Unbekannt"

            details.append(
                MatchDetail(
                    match_id=row.id,
                    candidate_id=row.candidate_id,
                    candidate_name=full_name,
                    candidate_title=row.hotlist_job_title or "",
                    candidate_city=row.hotlist_city or "",
                    ai_score=round(row.ai_score * 100, 1) if row.ai_score else None,
                    ai_explanation=row.ai_explanation,
                    ai_strengths=row.ai_strengths,
                    ai_weaknesses=row.ai_weaknesses,
                    distance_km=round(row.distance_km, 1) if row.distance_km else None,
                    status=row.status.value if row.status else "new",
                    matching_method=row.matching_method,
                    user_feedback=row.user_feedback,
                    feedback_note=row.feedback_note,
                    created_at=row.created_at,
                )
            )

        return details

    async def update_match_status(
        self,
        match_id: UUID,
        new_status: str,
    ) -> Match | None:
        """Aktualisiert den Status eines Matches.

        Args:
            match_id: Match-UUID
            new_status: Neuer Status (presented, rejected, placed)

        Returns:
            Aktualisiertes Match oder None
        """
        result = await self.db.execute(
            select(Match).where(Match.id == match_id)
        )
        match = result.scalar_one_or_none()

        if not match:
            return None

        # Status-Mapping
        status_map = {
            "new": MatchStatus.NEW,
            "ai_checked": MatchStatus.AI_CHECKED,
            "presented": MatchStatus.PRESENTED,
            "rejected": MatchStatus.REJECTED,
            "placed": MatchStatus.PLACED,
        }

        if new_status in status_map:
            match.status = status_map[new_status]

        await self.db.flush()
        return match

    async def save_feedback(
        self,
        match_id: UUID,
        feedback: str,
        note: str | None = None,
    ) -> Match | None:
        """Speichert Recruiter-Feedback fuer ein Match.

        Args:
            match_id: Match-UUID
            feedback: Feedback-Typ (good, bad, maybe)
            note: Optionale Notiz

        Returns:
            Aktualisiertes Match oder None
        """
        from datetime import datetime, timezone

        result = await self.db.execute(
            select(Match).where(Match.id == match_id)
        )
        match = result.scalar_one_or_none()

        if not match:
            return None

        match.user_feedback = feedback
        match.feedback_note = note
        match.feedback_at = datetime.now(timezone.utc)

        await self.db.flush()
        return match

    async def get_job_detail(self, job_id: UUID) -> dict | None:
        """Holt detaillierte Job-Informationen fuer die Detail-Ansicht.

        Args:
            job_id: Job-UUID

        Returns:
            dict mit Job-Details oder None
        """
        from app.models.job import Job

        result = await self.db.execute(
            select(Job).where(Job.id == job_id)
        )
        job = result.scalar_one_or_none()

        if not job:
            return None

        return {
            "id": job.id,
            "position": job.position or "Unbekannte Position",
            "company_name": job.company_name or "",
            "city": job.city or "",
            "postal_code": getattr(job, "postal_code", ""),
            "description": getattr(job, "description", ""),
            "requirements": getattr(job, "requirements", ""),
            "salary_from": getattr(job, "salary_from", None),
            "salary_to": getattr(job, "salary_to", None),
            "remote_option": getattr(job, "remote_option", None),
            "created_at": job.created_at,
            "hotlist_category": getattr(job, "hotlist_category", ""),
            "hotlist_job_title": getattr(job, "hotlist_job_title", ""),
        }

    async def get_stage_counts(self, category: str = "FINANCE") -> dict:
        """Holt die Anzahl Jobs pro Lifecycle-Stufe.

        Returns:
            dict mit new, in_progress, archive Counts
        """
        from app.models.job import Job

        # Subquery: Match-Aggregate pro Job
        match_agg = (
            select(
                Match.job_id,
                func.count(Match.id).label("match_count"),
                func.sum(case((Match.status == MatchStatus.PRESENTED, 1), else_=0)).label("presented_count"),
                func.sum(case((Match.status == MatchStatus.REJECTED, 1), else_=0)).label("rejected_count"),
                func.sum(case((Match.status == MatchStatus.PLACED, 1), else_=0)).label("placed_count"),
            )
            .where(
                and_(
                    Match.job_id.isnot(None),
                    Match.candidate_id.isnot(None),
                )
            )
            .group_by(Match.job_id)
            .subquery()
        )

        base = (
            select(
                match_agg.c.job_id,
                match_agg.c.match_count,
                match_agg.c.presented_count,
                match_agg.c.rejected_count,
                match_agg.c.placed_count,
            )
            .join(Job, Job.id == match_agg.c.job_id)
            .where(
                and_(
                    Job.deleted_at.is_(None),
                    or_(
                        Job.hotlist_category == category,
                        Job.hotlist_category.is_(None),
                    ),
                )
            )
            .subquery()
        )

        # Neu: Kein PRESENTED/PLACED
        new_q = select(func.count()).select_from(base).where(
            and_(
                base.c.presented_count == 0,
                base.c.placed_count == 0,
            )
        )
        new_count = (await self.db.execute(new_q)).scalar() or 0

        # In Bearbeitung: Mindestens 1 PRESENTED
        ip_q = select(func.count()).select_from(base).where(
            base.c.presented_count > 0
        )
        ip_count = (await self.db.execute(ip_q)).scalar() or 0

        # Archiv: Alle REJECTED oder PLACED
        arch_q = select(func.count()).select_from(base).where(
            and_(
                (base.c.rejected_count + base.c.placed_count) == base.c.match_count,
                base.c.match_count > 0,
            )
        )
        arch_count = (await self.db.execute(arch_q)).scalar() or 0

        return {
            "new": new_count,
            "in_progress": ip_count,
            "archive": arch_count,
        }
