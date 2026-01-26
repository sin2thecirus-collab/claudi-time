"""Candidate Service - CRUD-Operationen für Kandidaten.

Dieser Service bietet:
- CRUD für Kandidaten
- Filterung und Suche
- Hide/Unhide-Funktionalität
- Batch-Operationen
"""

import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from geoalchemy2 import functions as geo_func
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import limits
from app.models.candidate import Candidate
from app.models.match import Match
from app.schemas.candidate import (
    CandidateCreate,
    CandidateResponse,
    CandidateUpdate,
    CandidateWithMatch,
)
from app.schemas.filters import CandidateFilterParams
from app.schemas.pagination import PaginatedResponse, PaginationParams

logger = logging.getLogger(__name__)


class CandidateService:
    """Service für Kandidaten-Operationen."""

    def __init__(self, db: AsyncSession):
        """Initialisiert den Service.

        Args:
            db: Datenbank-Session
        """
        self.db = db

    async def create_candidate(self, data: CandidateCreate) -> Candidate:
        """Erstellt einen neuen Kandidaten.

        Args:
            data: Kandidaten-Daten

        Returns:
            Erstellter Kandidat
        """
        candidate = Candidate(
            crm_id=data.crm_id,
            first_name=data.first_name,
            last_name=data.last_name,
            email=data.email,
            phone=data.phone,
            birth_date=data.birth_date,
            current_position=data.current_position,
            current_company=data.current_company,
            skills=data.skills,
            street_address=data.street_address,
            postal_code=data.postal_code,
            city=data.city,
            cv_url=data.cv_url,
        )

        self.db.add(candidate)
        await self.db.flush()

        logger.info(f"Kandidat erstellt: {candidate.id} ({candidate.full_name})")
        return candidate

    async def get_candidate(self, candidate_id: UUID) -> Candidate | None:
        """Ruft einen Kandidaten ab.

        Args:
            candidate_id: ID des Kandidaten

        Returns:
            Kandidat oder None
        """
        result = await self.db.execute(
            select(Candidate).where(Candidate.id == candidate_id)
        )
        return result.scalar_one_or_none()

    async def get_candidate_by_crm_id(self, crm_id: str) -> Candidate | None:
        """Ruft einen Kandidaten über die CRM-ID ab.

        Args:
            crm_id: CRM-ID des Kandidaten

        Returns:
            Kandidat oder None
        """
        result = await self.db.execute(
            select(Candidate).where(Candidate.crm_id == crm_id)
        )
        return result.scalar_one_or_none()

    async def update_candidate(
        self,
        candidate_id: UUID,
        data: CandidateUpdate,
    ) -> Candidate | None:
        """Aktualisiert einen Kandidaten.

        Args:
            candidate_id: ID des Kandidaten
            data: Update-Daten

        Returns:
            Aktualisierter Kandidat oder None
        """
        candidate = await self.get_candidate(candidate_id)
        if not candidate:
            return None

        update_data = data.model_dump(exclude_unset=True)

        # Work History und Education als JSONB speichern
        if "work_history" in update_data and update_data["work_history"]:
            update_data["work_history"] = [
                entry.model_dump() if hasattr(entry, "model_dump") else entry
                for entry in update_data["work_history"]
            ]
        if "education" in update_data and update_data["education"]:
            update_data["education"] = [
                entry.model_dump() if hasattr(entry, "model_dump") else entry
                for entry in update_data["education"]
            ]

        for key, value in update_data.items():
            setattr(candidate, key, value)

        candidate.updated_at = datetime.now(timezone.utc)

        logger.info(f"Kandidat aktualisiert: {candidate_id}")
        return candidate

    async def list_candidates(
        self,
        filters: CandidateFilterParams | None = None,
        pagination: PaginationParams | None = None,
    ) -> PaginatedResponse:
        """Listet Kandidaten mit Filterung und Pagination.

        Args:
            filters: Filter-Parameter
            pagination: Pagination-Parameter

        Returns:
            Paginierte Liste von Kandidaten
        """
        filters = filters or CandidateFilterParams()
        pagination = pagination or PaginationParams()

        # Basis-Query
        query = select(Candidate)

        # Filter anwenden
        query = self._apply_filters(query, filters)

        # Sortierung
        sort_column = getattr(Candidate, filters.sort_by, Candidate.created_at)
        if filters.sort_order == "asc":
            query = query.order_by(sort_column.asc())
        else:
            query = query.order_by(sort_column.desc())

        # Total zählen
        count_query = select(func.count()).select_from(query.subquery())
        total_result = await self.db.execute(count_query)
        total = total_result.scalar_one()

        # Pagination
        offset = (pagination.page - 1) * pagination.per_page
        query = query.offset(offset).limit(pagination.per_page)

        # Ausführen
        result = await self.db.execute(query)
        candidates = result.scalars().all()

        # Response erstellen
        items = [self._to_response(c) for c in candidates]
        pages = (total + pagination.per_page - 1) // pagination.per_page

        return PaginatedResponse(
            items=items,
            total=total,
            page=pagination.page,
            per_page=pagination.per_page,
            pages=pages,
        )

    async def get_candidates_for_job(
        self,
        job_id: UUID,
        filters: CandidateFilterParams | None = None,
        pagination: PaginationParams | None = None,
    ) -> PaginatedResponse:
        """Ruft Kandidaten für einen Job ab (mit Match-Daten).

        Args:
            job_id: ID des Jobs
            filters: Filter-Parameter
            pagination: Pagination-Parameter

        Returns:
            Paginierte Liste mit CandidateWithMatch
        """
        filters = filters or CandidateFilterParams()
        pagination = pagination or PaginationParams()

        # Query mit Match-Join
        query = (
            select(Candidate, Match)
            .join(Match, Match.candidate_id == Candidate.id)
            .where(Match.job_id == job_id)
        )

        # Filter anwenden
        query = self._apply_filters_with_match(query, filters)

        # Sortierung (Standard: nach Distanz)
        if filters.sort_by == "distance_km":
            query = query.order_by(Match.distance_km.asc())
        elif filters.sort_by == "ai_score":
            query = query.order_by(Match.ai_score.desc().nullslast())
        elif filters.sort_by == "keyword_score":
            query = query.order_by(Match.keyword_score.desc())
        else:
            query = query.order_by(Match.distance_km.asc())

        # Total zählen
        count_query = select(func.count()).select_from(query.subquery())
        total_result = await self.db.execute(count_query)
        total = total_result.scalar_one()

        # Pagination
        offset = (pagination.page - 1) * pagination.per_page
        query = query.offset(offset).limit(pagination.per_page)

        # Ausführen
        result = await self.db.execute(query)
        rows = result.all()

        # Response erstellen
        items = [self._to_match_response(candidate, match) for candidate, match in rows]
        pages = (total + pagination.per_page - 1) // pagination.per_page

        return PaginatedResponse(
            items=items,
            total=total,
            page=pagination.page,
            per_page=pagination.per_page,
            pages=pages,
        )

    async def hide_candidate(self, candidate_id: UUID) -> bool:
        """Blendet einen Kandidaten aus.

        Args:
            candidate_id: ID des Kandidaten

        Returns:
            True bei Erfolg
        """
        candidate = await self.get_candidate(candidate_id)
        if not candidate:
            return False

        candidate.hidden = True
        candidate.updated_at = datetime.now(timezone.utc)

        logger.info(f"Kandidat ausgeblendet: {candidate_id}")
        return True

    async def unhide_candidate(self, candidate_id: UUID) -> bool:
        """Blendet einen Kandidaten wieder ein.

        Args:
            candidate_id: ID des Kandidaten

        Returns:
            True bei Erfolg
        """
        candidate = await self.get_candidate(candidate_id)
        if not candidate:
            return False

        candidate.hidden = False
        candidate.updated_at = datetime.now(timezone.utc)

        logger.info(f"Kandidat eingeblendet: {candidate_id}")
        return True

    async def batch_hide(self, candidate_ids: list[UUID]) -> int:
        """Blendet mehrere Kandidaten aus.

        Args:
            candidate_ids: Liste von Kandidaten-IDs (max. 100)

        Returns:
            Anzahl der ausgeblendeten Kandidaten
        """
        if len(candidate_ids) > limits.BATCH_HIDE_MAX:
            raise ValueError(f"Maximal {limits.BATCH_HIDE_MAX} Kandidaten pro Batch")

        count = 0
        for candidate_id in candidate_ids:
            if await self.hide_candidate(candidate_id):
                count += 1

        logger.info(f"{count} Kandidaten ausgeblendet")
        return count

    async def batch_unhide(self, candidate_ids: list[UUID]) -> int:
        """Blendet mehrere Kandidaten wieder ein.

        Args:
            candidate_ids: Liste von Kandidaten-IDs (max. 100)

        Returns:
            Anzahl der eingeblendeten Kandidaten
        """
        if len(candidate_ids) > limits.BATCH_HIDE_MAX:
            raise ValueError(f"Maximal {limits.BATCH_HIDE_MAX} Kandidaten pro Batch")

        count = 0
        for candidate_id in candidate_ids:
            if await self.unhide_candidate(candidate_id):
                count += 1

        logger.info(f"{count} Kandidaten eingeblendet")
        return count

    async def get_candidates_without_coordinates(self) -> list[Candidate]:
        """Ruft Kandidaten ohne Koordinaten ab (für Geocoding)."""
        result = await self.db.execute(
            select(Candidate)
            .where(Candidate.address_coords.is_(None))
            .where(
                or_(
                    Candidate.city.isnot(None),
                    Candidate.postal_code.isnot(None),
                )
            )
            .limit(500)  # Batch-Limit
        )
        return list(result.scalars().all())

    async def get_candidates_without_cv_parse(self) -> list[Candidate]:
        """Ruft Kandidaten mit CV aber ohne Parsing ab."""
        result = await self.db.execute(
            select(Candidate)
            .where(Candidate.cv_url.isnot(None))
            .where(Candidate.cv_parsed_at.is_(None))
            .limit(100)  # Batch-Limit
        )
        return list(result.scalars().all())

    async def count_active_candidates(self) -> int:
        """Zählt aktive Kandidaten (≤30 Tage, nicht hidden)."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=limits.ACTIVE_CANDIDATE_DAYS)
        result = await self.db.execute(
            select(func.count(Candidate.id))
            .where(Candidate.hidden == False)
            .where(Candidate.created_at >= cutoff)
        )
        return result.scalar_one()

    async def count_total_candidates(self) -> int:
        """Zählt alle Kandidaten (nicht hidden)."""
        result = await self.db.execute(
            select(func.count(Candidate.id))
            .where(Candidate.hidden == False)
        )
        return result.scalar_one()

    def _apply_filters(self, query, filters: CandidateFilterParams):
        """Wendet Filter auf die Query an."""
        # Name-Suche
        if filters.name:
            search_term = f"%{filters.name}%"
            query = query.where(
                or_(
                    Candidate.first_name.ilike(search_term),
                    Candidate.last_name.ilike(search_term),
                )
            )

        # Stadt-Filter
        if filters.cities:
            query = query.where(Candidate.city.in_(filters.cities))

        # Skills-Filter (AND-Verknüpfung)
        if filters.skills:
            for skill in filters.skills:
                query = query.where(Candidate.skills.contains([skill]))

        # Position-Filter
        if filters.position:
            query = query.where(Candidate.current_position.ilike(f"%{filters.position}%"))

        # Nur aktive Kandidaten
        if filters.only_active:
            cutoff = datetime.now(timezone.utc) - timedelta(days=limits.ACTIVE_CANDIDATE_DAYS)
            query = query.where(Candidate.created_at >= cutoff)

        # Hidden-Filter
        if not filters.include_hidden:
            query = query.where(Candidate.hidden == False)

        return query

    def _apply_filters_with_match(self, query, filters: CandidateFilterParams):
        """Wendet Filter auf Query mit Match-Join an."""
        # Basis-Filter
        query = self._apply_filters(query, filters)

        # Distanz-Filter
        if filters.min_distance_km is not None:
            query = query.where(Match.distance_km >= filters.min_distance_km)
        if filters.max_distance_km is not None:
            query = query.where(Match.distance_km <= filters.max_distance_km)

        # Nur KI-geprüfte
        if filters.only_ai_checked:
            query = query.where(Match.ai_checked_at.isnot(None))

        # Min. KI-Score
        if filters.min_ai_score is not None:
            query = query.where(Match.ai_score >= filters.min_ai_score)

        # Status-Filter
        if filters.status:
            query = query.where(Match.status.in_(filters.status))

        return query

    def _to_response(self, candidate: Candidate) -> CandidateResponse:
        """Konvertiert Candidate zu CandidateResponse."""
        return CandidateResponse(
            id=candidate.id,
            crm_id=candidate.crm_id,
            first_name=candidate.first_name,
            last_name=candidate.last_name,
            full_name=candidate.full_name,
            email=candidate.email,
            phone=candidate.phone,
            birth_date=candidate.birth_date,
            age=candidate.age,
            current_position=candidate.current_position,
            current_company=candidate.current_company,
            skills=candidate.skills,
            work_history=candidate.work_history,
            education=candidate.education,
            street_address=candidate.street_address,
            postal_code=candidate.postal_code,
            city=candidate.city,
            has_coordinates=candidate.address_coords is not None,
            cv_url=candidate.cv_url,
            cv_parsed_at=candidate.cv_parsed_at,
            hidden=candidate.hidden,
            is_active=candidate.is_active,
            crm_synced_at=candidate.crm_synced_at,
            created_at=candidate.created_at,
            updated_at=candidate.updated_at,
        )

    def _to_match_response(self, candidate: Candidate, match: Match) -> CandidateWithMatch:
        """Konvertiert Candidate + Match zu CandidateWithMatch."""
        base = self._to_response(candidate)
        return CandidateWithMatch(
            **base.model_dump(),
            distance_km=match.distance_km,
            keyword_score=match.keyword_score,
            matched_keywords=match.matched_keywords,
            ai_score=match.ai_score,
            ai_explanation=match.ai_explanation,
            ai_strengths=match.ai_strengths,
            ai_weaknesses=match.ai_weaknesses,
            match_status=match.status,
            match_id=match.id,
            is_ai_checked=match.ai_checked_at is not None,
        )
