"""New Match Center Service — Claude Code Matching.

Job-zentrische und Kandidat-zentrische Ansichten fuer Claude-Code-Matches.
Ersetzt den alten Match Center Service + Action Board.
"""

import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import and_, case, desc, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.candidate import Candidate
from app.models.job import Job
from app.models.match import Match, MatchStatus

logger = logging.getLogger(__name__)


class NewMatchCenterService:
    """Service fuer das neue Match Center (Claude Code Matches)."""

    def __init__(self, db: AsyncSession):
        self.db = db

    # ── STATISTIKEN ──────────────────────────────────────────

    async def get_dashboard_stats(self) -> dict:
        """Holt alle Dashboard-Statistiken auf einen Blick."""
        db = self.db

        # Matches nach Empfehlung
        empfehlung_result = await db.execute(
            select(
                Match.empfehlung,
                func.count(Match.id).label("cnt"),
            )
            .where(Match.matching_method == "claude_code")
            .where(Match.empfehlung.is_not(None))
            .group_by(Match.empfehlung)
        )
        empfehlung_counts = {row.empfehlung: row.cnt for row in empfehlung_result}

        # WOW Matches
        wow_count = await db.scalar(
            select(func.count(Match.id))
            .where(Match.matching_method == "claude_code")
            .where(Match.wow_faktor == True)
        )

        # Total Claude-Code Matches
        total = await db.scalar(
            select(func.count(Match.id))
            .where(Match.matching_method == "claude_code")
        )

        # Status-Verteilung
        status_result = await db.execute(
            select(
                Match.status,
                func.count(Match.id).label("cnt"),
            )
            .where(Match.matching_method == "claude_code")
            .group_by(Match.status)
        )
        status_counts = {row.status.value if hasattr(row.status, 'value') else row.status: row.cnt for row in status_result}

        # Schnitt-Score
        avg_score = await db.scalar(
            select(func.avg(Match.v2_score))
            .where(Match.matching_method == "claude_code")
            .where(Match.v2_score.is_not(None))
        )

        # Stale Matches
        stale_count = await db.scalar(
            select(func.count(Match.id))
            .where(Match.matching_method == "claude_code")
            .where(Match.stale == True)
        )

        # Aktive Jobs und Kandidaten
        active_jobs = await db.scalar(
            select(func.count(Job.id))
            .where(Job.deleted_at.is_(None))
            .where(or_(Job.expires_at.is_(None), Job.expires_at > func.now()))
        )

        active_candidates = await db.scalar(
            select(func.count(Candidate.id))
            .where(Candidate.hidden == False)
            .where(Candidate.deleted_at.is_(None))
        )

        # Letzter Match
        last_match_at = await db.scalar(
            select(func.max(Match.created_at))
            .where(Match.matching_method == "claude_code")
        )

        return {
            "total_matches": total or 0,
            "vorstellen": empfehlung_counts.get("vorstellen", 0),
            "beobachten": empfehlung_counts.get("beobachten", 0),
            "nicht_passend": empfehlung_counts.get("nicht_passend", 0),
            "wow_count": wow_count or 0,
            "avg_score": round(avg_score, 1) if avg_score else 0,
            "stale_count": stale_count or 0,
            "status_counts": status_counts,
            "active_jobs": active_jobs or 0,
            "active_candidates": active_candidates or 0,
            "last_match_at": last_match_at,
        }

    # ── MATCH-LISTE ──────────────────────────────────────────

    async def get_matches(
        self,
        empfehlung: str | None = None,
        city: str | None = None,
        role: str | None = None,
        wow_only: bool = False,
        status_filter: str | None = None,
        score_min: int | None = None,
        score_max: int | None = None,
        sort_by: str = "score",
        sort_dir: str = "desc",
        page: int = 1,
        per_page: int = 20,
    ) -> dict:
        """Paginierte Match-Liste mit Filtern."""
        db = self.db

        # Basis-Query mit Joins
        base_q = (
            select(
                Match,
                Job.position.label("job_position"),
                Job.company_name.label("job_company"),
                Job.city.label("job_city"),
                Job.work_location_city.label("job_work_city"),
                Job.work_arrangement.label("job_arrangement"),
                Job.classification_data.label("job_classification"),
                Candidate.city.label("cand_city"),
                Candidate.current_position.label("cand_position"),
                Candidate.classification_data.label("cand_classification"),
                Candidate.skills.label("cand_skills"),
                Candidate.it_skills.label("cand_it_skills"),
            )
            .outerjoin(Job, Job.id == Match.job_id)
            .outerjoin(Candidate, Candidate.id == Match.candidate_id)
            .where(Match.matching_method == "claude_code")
        )

        # Filter anwenden
        if empfehlung:
            base_q = base_q.where(Match.empfehlung == empfehlung)
        if wow_only:
            base_q = base_q.where(Match.wow_faktor == True)
        if status_filter:
            base_q = base_q.where(Match.status == status_filter)
        if score_min is not None:
            base_q = base_q.where(Match.v2_score >= score_min)
        if score_max is not None:
            base_q = base_q.where(Match.v2_score <= score_max)
        if city:
            city_lower = f"%{city.lower()}%"
            base_q = base_q.where(
                or_(
                    func.lower(Job.city).like(city_lower),
                    func.lower(Job.work_location_city).like(city_lower),
                    func.lower(Candidate.city).like(city_lower),
                )
            )
        if role:
            base_q = base_q.where(
                or_(
                    Job.classification_data["primary_role"].astext == role,
                    Candidate.classification_data["primary_role"].astext == role,
                )
            )

        # Total zaehlen
        count_q = select(func.count()).select_from(base_q.subquery())
        total = await db.scalar(count_q) or 0

        # Sortierung
        sort_map = {
            "score": Match.v2_score,
            "created": Match.created_at,
            "drive_car": Match.drive_time_car_min,
            "drive_transit": Match.drive_time_transit_min,
            "company": Job.company_name,
        }
        sort_col = sort_map.get(sort_by, Match.v2_score)
        if sort_dir == "asc":
            base_q = base_q.order_by(sort_col.asc().nulls_last())
        else:
            base_q = base_q.order_by(sort_col.desc().nulls_last())

        # Pagination
        offset = (page - 1) * per_page
        base_q = base_q.offset(offset).limit(per_page)

        result = await db.execute(base_q)
        rows = result.all()

        matches = []
        for row in rows:
            m = row[0]  # Match object
            matches.append({
                "id": str(m.id),
                "job_id": str(m.job_id) if m.job_id else None,
                "candidate_id": str(m.candidate_id) if m.candidate_id else None,
                "score": m.v2_score,
                "ai_score": m.ai_score,
                "empfehlung": m.empfehlung,
                "wow_faktor": m.wow_faktor,
                "wow_grund": m.wow_grund,
                "ai_explanation": m.ai_explanation,
                "ai_strengths": m.ai_strengths or [],
                "ai_weaknesses": m.ai_weaknesses or [],
                "status": m.status.value if hasattr(m.status, 'value') else m.status,
                "drive_time_car_min": m.drive_time_car_min,
                "drive_time_transit_min": m.drive_time_transit_min,
                "distance_km": m.distance_km,
                "stale": m.stale,
                "user_feedback": m.user_feedback,
                "created_at": m.created_at.isoformat() if m.created_at else None,
                # Job-Daten
                "job_position": row.job_position,
                "job_company": row.job_company,
                "job_city": row.job_work_city or row.job_city,
                "job_arrangement": row.job_arrangement,
                "job_role": (row.job_classification or {}).get("primary_role", ""),
                # Kandidat-Daten
                "cand_city": row.cand_city,
                "cand_position": row.cand_position,
                "cand_role": (row.cand_classification or {}).get("primary_role", ""),
                "cand_skills": row.cand_skills or [],
                "cand_it_skills": row.cand_it_skills or [],
            })

        pages = max(1, -(-total // per_page))  # ceil division

        return {
            "matches": matches,
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": pages,
        }

    # ── EINZEL-MATCH ─────────────────────────────────────────

    async def get_match_detail(self, match_id: UUID) -> dict | None:
        """Detailansicht eines Matches mit allen Infos."""
        db = self.db

        result = await db.execute(
            select(Match, Job, Candidate)
            .outerjoin(Job, Job.id == Match.job_id)
            .outerjoin(Candidate, Candidate.id == Match.candidate_id)
            .where(Match.id == match_id)
        )
        row = result.first()
        if not row:
            return None

        m, j, c = row

        return {
            "match": {
                "id": str(m.id),
                "score": m.v2_score,
                "empfehlung": m.empfehlung,
                "wow_faktor": m.wow_faktor,
                "wow_grund": m.wow_grund,
                "ai_explanation": m.ai_explanation,
                "ai_strengths": m.ai_strengths or [],
                "ai_weaknesses": m.ai_weaknesses or [],
                "status": m.status.value if hasattr(m.status, 'value') else m.status,
                "drive_time_car_min": m.drive_time_car_min,
                "drive_time_transit_min": m.drive_time_transit_min,
                "distance_km": m.distance_km,
                "stale": m.stale,
                "user_feedback": m.user_feedback,
                "feedback_note": m.feedback_note,
                "created_at": m.created_at.isoformat() if m.created_at else None,
                "matching_method": m.matching_method,
            },
            "job": {
                "id": str(j.id) if j else None,
                "position": j.position if j else None,
                "company_name": j.company_name if j else None,
                "city": (j.work_location_city or j.city) if j else None,
                "street_address": j.street_address if j else None,
                "postal_code": j.postal_code if j else None,
                "work_arrangement": j.work_arrangement if j else None,
                "job_text": j.job_text if j else None,
                "job_tasks": j.job_tasks if j else None,
                "classification_data": j.classification_data if j else None,
                "quality_score": j.quality_score if j else None,
                "employment_type": j.employment_type if j else None,
                "industry": j.industry if j else None,
                "company_size": j.company_size if j else None,
                "company_id": str(j.company_id) if j and j.company_id else None,
            } if j else None,
            "candidate": {
                "id": str(c.id) if c else None,
                "first_name": c.first_name if c else None,
                "last_name": c.last_name if c else None,
                "city": c.city if c else None,
                "street_address": c.street_address if c else None,
                "postal_code": c.postal_code if c else None,
                "current_position": c.current_position if c else None,
                "current_company": c.current_company if c else None,
                "skills": c.skills if c else [],
                "it_skills": c.it_skills if c else [],
                "erp": c.erp if c else [],
                "erp_main": c.erp_main if c else None,
                "work_history": c.work_history if c else None,
                "education": c.education if c else None,
                "further_education": c.further_education if c else None,
                "classification_data": c.classification_data if c else None,
                "salary": c.salary if c else None,
                "notice_period": c.notice_period if c else None,
                "willingness_to_change": c.willingness_to_change if c else None,
                "desired_positions": c.desired_positions if c else None,
                "key_activities": c.key_activities if c else None,
                "home_office_days": c.home_office_days if c else None,
                "commute_max": c.commute_max if c else None,
                "languages": c.languages if c else None,
                "candidate_notes": c.candidate_notes if c else None,
            } if c else None,
        }

    # ── STATUS UPDATE ────────────────────────────────────────

    async def update_status(self, match_id: UUID, new_status: str) -> bool:
        """Match-Status aendern."""
        match = await self.db.get(Match, match_id)
        if not match:
            return False
        match.status = new_status
        await self.db.commit()
        return True

    async def save_feedback(
        self,
        match_id: UUID,
        feedback: str,
        note: str | None = None,
    ) -> bool:
        """Feedback fuer ein Match speichern."""
        match = await self.db.get(Match, match_id)
        if not match:
            return False

        match.user_feedback = feedback
        match.feedback_note = note
        match.feedback_at = datetime.now(timezone.utc)

        if feedback.startswith("bad_"):
            match.status = MatchStatus.REJECTED
            match.rejection_reason = feedback

        await self.db.commit()
        return True

    # ── FILTER-OPTIONEN ──────────────────────────────────────

    async def get_filter_options(self) -> dict:
        """Liefert verfuegbare Filter-Werte (Staedte, Rollen)."""
        db = self.db

        # Staedte aus Jobs
        cities_result = await db.execute(
            select(func.coalesce(Job.work_location_city, Job.city).label("city"))
            .join(Match, Match.job_id == Job.id)
            .where(Match.matching_method == "claude_code")
            .where(func.coalesce(Job.work_location_city, Job.city).is_not(None))
            .group_by(text("1"))
            .order_by(func.count(Match.id).desc())
            .limit(30)
        )
        cities = [row.city for row in cities_result if row.city]

        # Rollen aus Jobs
        roles_result = await db.execute(
            select(Job.classification_data["primary_role"].astext.label("role"))
            .join(Match, Match.job_id == Job.id)
            .where(Match.matching_method == "claude_code")
            .where(Job.classification_data["primary_role"].astext.is_not(None))
            .group_by(text("1"))
            .order_by(func.count(Match.id).desc())
            .limit(15)
        )
        roles = [row.role for row in roles_result if row.role]

        return {
            "cities": cities,
            "roles": roles,
        }
