"""Filter Service - Filtert Jobs und Kandidaten, verwaltet Presets."""

import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy import Select, and_, delete, distinct, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import Limits
from app.models.candidate import Candidate
from app.models.job import Job
from app.models.match import Match
from app.models.settings import FilterPreset, PriorityCity
from app.schemas.filters import (
    CandidateFilterParams,
    CandidateSortBy,
    FilterPresetCreate,
    JobFilterParams,
    JobSortBy,
    SortOrder,
)

logger = logging.getLogger(__name__)


class FilterService:
    """Service für Filter-Operationen."""

    def __init__(self, db: AsyncSession):
        """Initialisiert den FilterService."""
        self.db = db

    # ==================== Job-Filter ====================

    def apply_job_filters(
        self,
        query: Select,
        filters: JobFilterParams,
    ) -> Select:
        """
        Wendet Filter auf eine Job-Query an.

        Args:
            query: Basis-Query
            filters: Filter-Parameter

        Returns:
            Gefilterte Query
        """
        # Text-Suche (Position oder Unternehmen)
        if filters.search:
            search_pattern = f"%{filters.search}%"
            query = query.where(
                or_(
                    Job.position.ilike(search_pattern),
                    Job.company_name.ilike(search_pattern),
                )
            )

        # Städte-Filter (city OR work_location_city)
        if filters.cities:
            city_conditions = []
            for city in filters.cities:
                city_conditions.append(Job.city.ilike(f"%{city}%"))
                city_conditions.append(Job.work_location_city.ilike(f"%{city}%"))
            query = query.where(or_(*city_conditions))

        # Branchen-Filter
        if filters.industries:
            query = query.where(Job.industry.in_(filters.industries))

        # Unternehmen-Filter
        if filters.company:
            query = query.where(Job.company_name.ilike(f"%{filters.company}%"))

        # Position-Filter
        if filters.position:
            query = query.where(Job.position.ilike(f"%{filters.position}%"))

        # Gelöschte Jobs
        if not filters.include_deleted:
            query = query.where(Job.deleted_at.is_(None))

        # Abgelaufene Jobs
        if not filters.include_expired:
            query = query.where(
                or_(
                    Job.expires_at.is_(None),
                    Job.expires_at > datetime.utcnow(),
                )
            )

        # Datum-Filter: created_after
        if filters.created_after:
            query = query.where(Job.created_at >= filters.created_after)

        # Datum-Filter: created_before
        if filters.created_before:
            query = query.where(Job.created_at <= filters.created_before)

        # Datum-Filter: expires_after
        if filters.expires_after:
            query = query.where(Job.expires_at >= filters.expires_after)

        # Datum-Filter: expires_before
        if filters.expires_before:
            query = query.where(Job.expires_at <= filters.expires_before)

        return query

    def apply_job_sorting(
        self,
        query: Select,
        sort_by: JobSortBy,
        sort_order: SortOrder,
    ) -> Select:
        """Wendet Sortierung auf eine Job-Query an."""
        order_func = func.asc if sort_order == SortOrder.ASC else func.desc

        if sort_by == JobSortBy.COMPANY_NAME:
            query = query.order_by(order_func(Job.company_name))
        elif sort_by == JobSortBy.POSITION:
            query = query.order_by(order_func(Job.position))
        elif sort_by == JobSortBy.CITY:
            query = query.order_by(order_func(func.coalesce(Job.work_location_city, Job.city)))
        elif sort_by == JobSortBy.EXPIRES_AT:
            query = query.order_by(order_func(Job.expires_at).nullslast())
        else:  # CREATED_AT (default)
            query = query.order_by(order_func(Job.created_at))

        return query

    async def sort_jobs_by_priority_cities(self, query: Select) -> Select:
        """
        Sortiert Jobs so, dass Prio-Städte oben erscheinen.

        Hamburg und München sind standardmäßig Prio-Städte.
        """
        # Lade Prio-Städte
        prio_cities = await self.get_priority_cities()
        prio_city_names = [pc.city_name.lower() for pc in prio_cities]

        if not prio_city_names:
            return query

        # Erstelle CASE-Statement für Sortierung
        # Jobs in Prio-Städten bekommen niedrigere Sortier-Werte
        case_conditions = []
        for idx, city_name in enumerate(prio_city_names):
            case_conditions.append(
                (func.lower(func.coalesce(Job.work_location_city, Job.city)).like(f"%{city_name}%"), idx)
            )

        # Standard-Wert für Nicht-Prio-Städte
        priority_order = func.case(
            *case_conditions,
            else_=len(prio_city_names) + 1,
        )

        query = query.order_by(priority_order)
        return query

    # ==================== Kandidaten-Filter ====================

    def apply_candidate_filters(
        self,
        query: Select,
        filters: CandidateFilterParams,
        job_id: uuid.UUID | None = None,
    ) -> Select:
        """
        Wendet Filter auf eine Kandidaten-Query an.

        Args:
            query: Basis-Query (sollte bereits mit Match gejoined sein wenn job_id)
            filters: Filter-Parameter
            job_id: Optional Job-ID für Match-bezogene Filter

        Returns:
            Gefilterte Query
        """
        # Name-Suche
        if filters.name:
            name_pattern = f"%{filters.name}%"
            query = query.where(
                or_(
                    Candidate.first_name.ilike(name_pattern),
                    Candidate.last_name.ilike(name_pattern),
                    func.concat(Candidate.first_name, ' ', Candidate.last_name).ilike(name_pattern),
                )
            )

        # Städte-Filter
        if filters.cities:
            city_conditions = [Candidate.city.ilike(f"%{c}%") for c in filters.cities]
            query = query.where(or_(*city_conditions))

        # Skills-Filter (AND-Verknüpfung: Kandidat muss ALLE Skills haben)
        if filters.skills:
            for skill in filters.skills:
                query = query.where(
                    Candidate.skills.any(func.lower(skill))
                )

        # Position-Filter
        if filters.position:
            query = query.where(Candidate.current_position.ilike(f"%{filters.position}%"))

        # Nur aktive Kandidaten (≤30 Tage)
        if filters.only_active:
            active_threshold = datetime.utcnow() - timedelta(days=Limits.ACTIVE_CANDIDATE_DAYS)
            query = query.where(Candidate.created_at >= active_threshold)

        # Versteckte Kandidaten
        if not filters.include_hidden:
            query = query.where(Candidate.hidden == False)  # noqa: E712

        # Falls mit Match verknüpft (job_id gegeben)
        if job_id:
            # Distanz-Filter
            if filters.min_distance_km is not None:
                query = query.where(Match.distance_km >= filters.min_distance_km)
            if filters.max_distance_km is not None:
                query = query.where(Match.distance_km <= filters.max_distance_km)

            # Nur mit KI-Check
            if filters.only_ai_checked:
                query = query.where(Match.ai_checked_at.is_not(None))

            # Mindest-KI-Score
            if filters.min_ai_score is not None:
                query = query.where(Match.ai_score >= filters.min_ai_score)

            # Status-Filter
            if filters.status:
                query = query.where(Match.status == filters.status)

        return query

    def apply_candidate_sorting(
        self,
        query: Select,
        sort_by: CandidateSortBy,
        sort_order: SortOrder,
        has_match_join: bool = False,
    ) -> Select:
        """Wendet Sortierung auf eine Kandidaten-Query an."""
        order_func = func.asc if sort_order == SortOrder.ASC else func.desc

        if sort_by == CandidateSortBy.FULL_NAME:
            query = query.order_by(
                order_func(func.concat(Candidate.first_name, ' ', Candidate.last_name))
            )
        elif sort_by == CandidateSortBy.CITY:
            query = query.order_by(order_func(Candidate.city).nullslast())
        elif sort_by == CandidateSortBy.DISTANCE_KM and has_match_join:
            query = query.order_by(order_func(Match.distance_km).nullslast())
        elif sort_by == CandidateSortBy.AI_SCORE and has_match_join:
            query = query.order_by(order_func(Match.ai_score).nullslast())
        elif sort_by == CandidateSortBy.KEYWORD_SCORE and has_match_join:
            query = query.order_by(order_func(Match.keyword_score).nullslast())
        else:  # CREATED_AT (default)
            query = query.order_by(order_func(Candidate.created_at))

        return query

    # ==================== Filter-Optionen ====================

    async def get_available_cities(self) -> list[str]:
        """Gibt alle verfügbaren Städte aus Jobs und Kandidaten zurück."""
        # Städte aus Jobs
        job_cities_query = select(Job.city).where(
            and_(Job.city.is_not(None), Job.deleted_at.is_(None))
        ).distinct()

        job_work_cities_query = select(Job.work_location_city).where(
            and_(Job.work_location_city.is_not(None), Job.deleted_at.is_(None))
        ).distinct()

        # Städte aus Kandidaten
        candidate_cities_query = select(Candidate.city).where(
            and_(Candidate.city.is_not(None), Candidate.hidden == False)  # noqa: E712
        ).distinct()

        # Alle Queries ausführen
        job_cities_result = await self.db.execute(job_cities_query)
        job_work_cities_result = await self.db.execute(job_work_cities_query)
        candidate_cities_result = await self.db.execute(candidate_cities_query)

        # Kombinieren und deduplizieren
        all_cities = set()
        all_cities.update(c for (c,) in job_cities_result if c)
        all_cities.update(c for (c,) in job_work_cities_result if c)
        all_cities.update(c for (c,) in candidate_cities_result if c)

        return sorted(all_cities)

    async def get_available_skills(self) -> list[str]:
        """Gibt alle verfügbaren Skills aus Kandidaten zurück."""
        query = select(func.unnest(Candidate.skills)).where(
            and_(
                Candidate.skills.is_not(None),
                Candidate.hidden == False,  # noqa: E712
            )
        ).distinct()

        result = await self.db.execute(query)
        skills = [row[0] for row in result if row[0]]

        return sorted(set(skills))

    async def get_available_industries(self) -> list[str]:
        """Gibt alle verfügbaren Branchen aus Jobs zurück."""
        query = select(distinct(Job.industry)).where(
            and_(
                Job.industry.is_not(None),
                Job.deleted_at.is_(None),
            )
        )

        result = await self.db.execute(query)
        industries = [row[0] for row in result if row[0]]

        return sorted(industries)

    async def get_available_employment_types(self) -> list[str]:
        """Gibt alle verfügbaren Beschäftigungsarten zurück."""
        query = select(distinct(Job.employment_type)).where(
            and_(
                Job.employment_type.is_not(None),
                Job.deleted_at.is_(None),
            )
        )

        result = await self.db.execute(query)
        types = [row[0] for row in result if row[0]]

        return sorted(types)

    # ==================== Prio-Städte ====================

    async def get_priority_cities(self) -> list[PriorityCity]:
        """Gibt alle Prio-Städte sortiert zurück."""
        query = select(PriorityCity).order_by(PriorityCity.priority_order)
        result = await self.db.execute(query)
        return list(result.scalars().all())

    async def add_priority_city(
        self,
        city_name: str,
        priority_order: int | None = None,
    ) -> PriorityCity:
        """Fügt eine neue Prio-Stadt hinzu."""
        # Wenn keine Priorität angegeben, ans Ende setzen
        if priority_order is None:
            max_order_query = select(func.max(PriorityCity.priority_order))
            result = await self.db.execute(max_order_query)
            max_order = result.scalar() or 0
            priority_order = max_order + 1

        city = PriorityCity(
            city_name=city_name,
            priority_order=priority_order,
        )
        self.db.add(city)
        await self.db.commit()
        await self.db.refresh(city)

        return city

    async def update_priority_cities(
        self,
        cities: list[dict],
    ) -> list[PriorityCity]:
        """
        Aktualisiert die Prio-Städte (ersetzt alle).

        Args:
            cities: Liste von {city_name, priority_order}
        """
        # Alle bestehenden löschen
        await self.db.execute(delete(PriorityCity))

        # Neue erstellen
        new_cities = []
        for idx, city_data in enumerate(cities):
            city = PriorityCity(
                city_name=city_data["city_name"],
                priority_order=city_data.get("priority_order", idx),
            )
            self.db.add(city)
            new_cities.append(city)

        await self.db.commit()

        return new_cities

    async def remove_priority_city(self, city_id: uuid.UUID) -> bool:
        """Entfernt eine Prio-Stadt."""
        city = await self.db.get(PriorityCity, city_id)
        if not city:
            return False

        await self.db.delete(city)
        await self.db.commit()

        return True

    # ==================== Filter-Presets ====================

    async def get_filter_presets(self) -> list[FilterPreset]:
        """Gibt alle Filter-Presets zurück."""
        query = select(FilterPreset).order_by(FilterPreset.is_default.desc(), FilterPreset.name)
        result = await self.db.execute(query)
        return list(result.scalars().all())

    async def get_filter_preset(self, preset_id: uuid.UUID) -> FilterPreset | None:
        """Gibt einen einzelnen Preset zurück."""
        return await self.db.get(FilterPreset, preset_id)

    async def create_filter_preset(self, data: FilterPresetCreate) -> FilterPreset:
        """Erstellt einen neuen Filter-Preset."""
        # Wenn is_default, alle anderen auf False setzen
        if data.is_default:
            await self.db.execute(
                FilterPreset.__table__.update().values(is_default=False)
            )

        preset = FilterPreset(
            name=data.name,
            filter_config=data.filter_config,
            is_default=data.is_default,
        )
        self.db.add(preset)
        await self.db.commit()
        await self.db.refresh(preset)

        return preset

    async def delete_filter_preset(self, preset_id: uuid.UUID) -> bool:
        """Löscht einen Filter-Preset."""
        preset = await self.db.get(FilterPreset, preset_id)
        if not preset:
            return False

        await self.db.delete(preset)
        await self.db.commit()

        return True

    async def set_default_preset(self, preset_id: uuid.UUID) -> FilterPreset | None:
        """Setzt einen Preset als Standard."""
        preset = await self.db.get(FilterPreset, preset_id)
        if not preset:
            return None

        # Alle anderen auf False
        await self.db.execute(
            FilterPreset.__table__.update().values(is_default=False)
        )

        preset.is_default = True
        await self.db.commit()
        await self.db.refresh(preset)

        return preset
