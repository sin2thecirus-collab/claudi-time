"""Match Center Service - Job-zentrische Match-Verwaltung.

Sidebar + Tabellen-Layout mit Pagination, Bulk-Aktionen, sortierbaren Spalten.
Ersetzt das alte Karten-Grid mit einer effizienten Vertriebsansicht.
"""

import logging
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import and_, case, desc, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.match import Match, MatchStatus

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# STADT-NORMALISIERUNG — Duplikat-Konsolidierung (Fix 11)
# ═══════════════════════════════════════════════════════════════

# Bekannte Stadtteil-Suffixe die entfernt werden sollen
_STADTTEILE_PATTERN = re.compile(
    r'^(.+?)[\s]*[-–]\s*('
    r'Wilmersdorf|Charlottenburg|Mitte|Kreuzberg|Neuk[oö]lln|'
    r'Schöneberg|Tempelhof|Steglitz|Zehlendorf|Spandau|Reinickendorf|'
    r'Lichtenberg|Pankow|Treptow|Köpenick|Marzahn|Hellersdorf|'
    r'Friedrichshain|Prenzlauer Berg|Wedding|Moabit|Tiergarten|'
    r'Altona|Eimsbüttel|Wandsbek|Harburg|Bergedorf|'
    r'Sendling|Schwabing|Bogenhausen|Haidhausen|Laim|Pasing|'
    r'Ehrenfeld|Nippes|Lindenthal|Porz|Kalk|Chorweiler|'
    r'Sachsenhausen|Bornheim|Bockenheim|Nordend|Westend|Gallus|'
    r'Innenstadt|Zentrum|Nord|Süd|Ost|West'
    r')\s*$',
    re.IGNORECASE
)

# PLZ-Prefix: "10117 Berlin" → "Berlin"
_PLZ_PREFIX_PATTERN = re.compile(r'^\d{4,5}\s+(.+)$')

# Bundesland-Suffixe
_BUNDESLAENDER = {
    "Baden-Württemberg", "Bayern", "Berlin", "Brandenburg", "Bremen",
    "Hamburg", "Hessen", "Mecklenburg-Vorpommern", "Niedersachsen",
    "Nordrhein-Westfalen", "Rheinland-Pfalz", "Saarland", "Sachsen",
    "Sachsen-Anhalt", "Schleswig-Holstein", "Thüringen",
}

# Pattern fuer Bundesland-Suffix am Ende: "Chemnitz Sachsen" → "Chemnitz"
_BUNDESLAND_SUFFIX_PATTERN = re.compile(
    r'^(.+?)\s+(' + '|'.join(re.escape(bl) for bl in _BUNDESLAENDER) + r')\s*$',
    re.IGNORECASE
)

# "Frankfurt am Main" → "Frankfurt" etc.
_CITY_ALIASES = {
    "Frankfurt am Main": "Frankfurt",
    "Frankfurt a.M.": "Frankfurt",
    "Frankfurt a. M.": "Frankfurt",
    "Frankfurt am Main Hessen": "Frankfurt",
    "Muenchen": "München",
    "Munich": "München",
    "Koeln": "Köln",
    "Duesseldorf": "Düsseldorf",
    "Nuernberg": "Nürnberg",
    "Wuerzburg": "Würzburg",
    "Goettingen": "Göttingen",
    "Luebeck": "Lübeck",
    "Lueneburg": "Lüneburg",
    "Saarbruecken": "Saarbrücken",
    "Offenbach am Main": "Offenbach",
    "Offenbach a.M.": "Offenbach",
    "Fürth Bayern": "Fürth",
    "Alzenau Bayern": "Alzenau",
    "Eisenach Thüringen": "Eisenach",
}

# Suffixe die am Ende entfernt werden
_SUFFIX_PATTERNS = [
    re.compile(r',?\s*Deutschland$', re.IGNORECASE),
    re.compile(r',?\s*DE$'),
    re.compile(r',?\s*Germany$', re.IGNORECASE),
]


def _normalize_city(city: str) -> str:
    """Normalisiert einen Stadtnamen fuer die Konsolidierung.

    Beispiele:
        "Berlin-Wilmersdorf"         → "Berlin"
        "10117 Berlin Berlin"        → "Berlin"
        "Frankfurt am Main"          → "Frankfurt"
        "Muenchen"                   → "München"
        "Munich"                     → "München"
        "09125 Chemnitz Sachsen"     → "Chemnitz"
        "Berlin, Deutschland"        → "Berlin"
        "Berlin, DE"                 → "Berlin"
        "Augsburg-Innenstadt"        → "Augsburg"
    """
    if not city or city == "Unbekannt":
        return city or "Unbekannt"

    city = city.strip()

    # 0) Zuerst: ", Deutschland" / ", DE" / ", Germany" am Ende entfernen (immer)
    for pattern in _SUFFIX_PATTERNS:
        city = pattern.sub('', city).strip()

    # 0b) Mehrstaedte-Strings (Komma mit mehreren Staedten) skippen
    # z.B. "Ansbach, Berlin, Fürth" oder "Bremen, Blankenfelde, Neumünster"
    # ABER: "Berlin, Deutschland" oder "München, Bayern" sind einzelne Staedte
    comma_parts = [p.strip() for p in city.split(',')]
    if len(comma_parts) > 2:
        return city  # Nicht normalisieren bei 3+ Teilen
    if len(comma_parts) == 2:
        second = comma_parts[1].strip()
        # Nur normalisieren wenn zweiter Teil ein Land/Bundesland ist
        if second.lower() in ('deutschland', 'de', 'germany') or second in _BUNDESLAENDER:
            city = comma_parts[0].strip()
        else:
            return city  # "Düsseldorf, Neuss" → nicht normalisieren

    # 1) PLZ-Prefix entfernen
    plz_match = _PLZ_PREFIX_PATTERN.match(city)
    if plz_match:
        city = plz_match.group(1).strip()

    # 2) Suffixe entfernen (, Deutschland / , DE / , Germany)
    for pattern in _SUFFIX_PATTERNS:
        city = pattern.sub('', city).strip()

    # 3) Bundesland-Suffix entfernen ("Chemnitz Sachsen" → "Chemnitz")
    bl_match = _BUNDESLAND_SUFFIX_PATTERN.match(city)
    if bl_match:
        city = bl_match.group(1).strip()

    # 4) Doppelten Stadtnamen entfernen ("Berlin Berlin" → "Berlin")
    words = city.split()
    if len(words) == 2 and words[0] == words[1]:
        city = words[0]

    # 5) Bekannte Aliases
    if city in _CITY_ALIASES:
        return _CITY_ALIASES[city]

    # 6) Stadtteil-Suffix entfernen ("Berlin-Wilmersdorf" → "Berlin")
    stadtteil_match = _STADTTEILE_PATTERN.match(city)
    if stadtteil_match:
        city = stadtteil_match.group(1).strip()

    # 7) Nochmal Aliases prüfen (falls nach Stadtteil-Entfernung ein Alias matcht)
    if city in _CITY_ALIASES:
        return _CITY_ALIASES[city]

    return city


# ═══════════════════════════════════════════════════════════════
# EFFECTIVE SCORE HELPER — v2_score (0-100) mit Fallback auf ai_score * 100
# ═══════════════════════════════════════════════════════════════

def _effective_score():
    """SQL-Expression: Effektiver Score auf 0-100 Skala.

    Pipeline V3 Matches: ai_score * 100 (immer vorhanden)
    Legacy Matches: COALESCE(v2_score, ai_score * 100)
    """
    from sqlalchemy import case, literal_column

    return case(
        (Match.matching_method == "pipeline_v3", Match.ai_score * 100),
        else_=func.coalesce(Match.v2_score, Match.ai_score * 100),
    )


# ═══════════════════════════════════════════════════════════════
# FESTE SORTIERREIHENFOLGEN
# ═══════════════════════════════════════════════════════════════

TITLE_ORDER = {
    "Leiter Buchhaltung": 0,
    "Bilanzbuchhalter/in": 1,
    "Finanzbuchhalter/in": 2,
    "Kreditorenbuchhalter/in": 3,
    "Debitorenbuchhalter/in": 4,
    "Lohnbuchhalter/in": 5,
    "Steuerfachangestellte/r": 6,
}

CITY_ORDER = {
    "München": 0,
    "Muenchen": 0,
    "Hamburg": 1,
    "Frankfurt": 2,
    "Frankfurt am Main": 2,
    "Berlin": 3,
    "Stuttgart": 4,
    "Köln": 5,
    "Koeln": 5,
    "Düsseldorf": 6,
    "Duesseldorf": 6,
    "Hannover": 7,
}


def _title_sort_key(title: str) -> tuple:
    """Sortier-Schluessel fuer Jobtitel (benutzerdefinierte Reihenfolge)."""
    return (TITLE_ORDER.get(title, 99), title)


def _city_sort_key(city: str) -> tuple:
    """Sortier-Schluessel fuer Stadt (benutzerdefinierte Reihenfolge)."""
    return (CITY_ORDER.get(city, 99), city)


# ═══════════════════════════════════════════════════════════════
# DATACLASSES
# ═══════════════════════════════════════════════════════════════


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
class JobTitleCityGroup:
    """Ein Kaestchen im Grid: Jobtitel + Stadt mit aggregierten Match-Daten."""

    hotlist_job_title: str
    city: str
    job_count: int
    total_match_count: int
    top_ai_score: float | None
    avg_ai_score: float | None
    new_count: int
    presented_count: int
    job_ids: list[str] = field(default_factory=list)


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
    drive_time_car_min: int | None = None
    drive_time_transit_min: int | None = None
    status: str = "new"
    matching_method: str | None = None
    user_feedback: str | None = None
    feedback_note: str | None = None
    created_at: datetime | None = None


@dataclass
class MatchComparisonData:
    """Alle Daten fuer den Vergleich-Dialog eines Matches."""

    # Match
    match_id: UUID
    ai_score: float | None
    ai_explanation: str | None
    ai_strengths: list[str] | None
    ai_weaknesses: list[str] | None
    distance_km: float | None
    drive_time_car_min: int | None = None
    drive_time_transit_min: int | None = None
    status: str = "new"
    user_feedback: str | None = None

    # Job
    job_id: UUID | None = None
    job_position: str = ""
    job_company_name: str = ""
    job_city: str = ""
    job_postal_code: str = ""
    job_street_address: str = ""
    job_text: str = ""

    # Score Breakdown (v2.5)
    v2_score_breakdown: dict | None = None

    # Kandidat
    candidate_id: UUID | None = None
    candidate_name: str = ""
    candidate_city: str = ""
    candidate_postal_code: str = ""
    candidate_street_address: str = ""
    candidate_current_position: str = ""
    candidate_current_company: str = ""
    work_history: list[dict] | None = None
    education: list[dict] | None = None
    further_education: list[dict] | None = None
    languages: list[dict] | None = None
    it_skills: list[str] | None = None
    skills: list[str] | None = None


# ═══════════════════════════════════════════════════════════════
# SERVICE
# ═══════════════════════════════════════════════════════════════


class MatchCenterService:
    """Service fuer job-zentrische Match-Verwaltung."""

    def __init__(self, db: AsyncSession):
        self.db = db

    # ───────────────────────────────────────────────────────────
    # STATISTIKEN
    # ───────────────────────────────────────────────────────────

    async def get_stats(self, category: str = "FINANCE") -> dict:
        """Holt Uebersichts-Statistiken fuer das Match Center."""
        from app.models.job import Job

        eff = _effective_score()

        base = (
            select(
                func.count(func.distinct(Match.job_id)).label("total_jobs"),
                func.count(Match.id).label("total_matches"),
                func.avg(eff).label("avg_score"),
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

        avg = round(float(row.avg_score), 1) if row.avg_score else 0

        return {
            "total_jobs": int(row.total_jobs or 0),
            "total_matches": int(row.total_matches or 0),
            "avg_score": avg,
            "placed_count": int(row.placed or 0),
            "new_count": int(row.new or 0) + int(row.ai_checked or 0),
            "presented_count": int(row.presented or 0),
            "rejected_count": int(row.rejected or 0),
        }

    async def get_stage_counts(self, category: str = "FINANCE") -> dict:
        """Holt die Anzahl Jobs pro Lifecycle-Stufe."""
        from app.models.job import Job

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

        new_q = select(func.count()).select_from(base).where(
            and_(base.c.presented_count == 0, base.c.placed_count == 0)
        )
        new_count = (await self.db.execute(new_q)).scalar() or 0

        ip_q = select(func.count()).select_from(base).where(base.c.presented_count > 0)
        ip_count = (await self.db.execute(ip_q)).scalar() or 0

        arch_q = select(func.count()).select_from(base).where(
            and_(
                (base.c.rejected_count + base.c.placed_count) == base.c.match_count,
                base.c.match_count > 0,
            )
        )
        arch_count = (await self.db.execute(arch_q)).scalar() or 0

        return {"new": new_count, "in_progress": ip_count, "archive": arch_count}

    # ───────────────────────────────────────────────────────────
    # GRID-UEBERSICHT (Jobtitel × Stadt)
    # ───────────────────────────────────────────────────────────

    async def get_grid_overview(
        self,
        category: str = "FINANCE",
        stage: str = "new",
        search: str | None = None,
    ) -> OrderedDict[str, list[JobTitleCityGroup]]:
        """Holt das Grid: Jobtitel-Reihen × Stadt-Kaestchen.

        Returns:
            OrderedDict[jobtitel → [JobTitleCityGroup, ...]]
            Sortiert nach benutzerdefinierter Jobtitel- und Stadt-Reihenfolge.
        """
        from app.models.job import Job

        # Subquery: Match-Aggregate pro Job (fuer Stage-Filter)
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

        # Display-City: work_location_city oder city
        display_city = func.coalesce(Job.work_location_city, Job.city)

        # Haupt-Query: Jobs mit Match-Aggregaten
        query = (
            select(
                Job.id,
                func.coalesce(Job.hotlist_job_title, text("'Sonstige'")).label("job_title"),
                func.coalesce(display_city, text("'Unbekannt'")).label("display_city"),
                Job.position,
                Job.company_name,
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
            query = query.where(
                and_(
                    match_agg.c.presented_count == 0,
                    match_agg.c.placed_count == 0,
                )
            )
        elif stage == "in_progress":
            query = query.where(match_agg.c.presented_count > 0)
        elif stage == "archive":
            query = query.where(
                and_(
                    (match_agg.c.rejected_count + match_agg.c.placed_count) == match_agg.c.match_count,
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
                    Job.work_location_city.ilike(search_term),
                    Job.hotlist_job_title.ilike(search_term),
                )
            )

        result = await self.db.execute(query)
        rows = result.all()

        # In Python gruppieren und sortieren (wegen benutzerdefinierter Reihenfolge)
        # Erst: Rohdaten pro (title, city) aggregieren
        grid_map: dict[tuple[str, str], dict] = {}

        for row in rows:
            title = row.job_title or "Sonstige"
            city = row.display_city or "Unbekannt"
            key = (title, city)

            if key not in grid_map:
                grid_map[key] = {
                    "hotlist_job_title": title,
                    "city": city,
                    "job_count": 0,
                    "total_match_count": 0,
                    "top_ai_score": None,
                    "sum_ai_score": 0.0,
                    "score_count": 0,
                    "new_count": 0,
                    "presented_count": 0,
                    "job_ids": [],
                }

            g = grid_map[key]
            g["job_count"] += 1
            g["total_match_count"] += int(row.match_count or 0)
            g["new_count"] += int(row.new_count or 0) + int(row.ai_checked_count or 0)
            g["presented_count"] += int(row.presented_count or 0)
            g["job_ids"].append(str(row.id))

            if row.top_ai_score is not None:
                score_pct = round(float(row.top_ai_score) * 100, 1)
                if g["top_ai_score"] is None or score_pct > g["top_ai_score"]:
                    g["top_ai_score"] = score_pct

            if row.avg_ai_score is not None:
                g["sum_ai_score"] += float(row.avg_ai_score)
                g["score_count"] += 1

        # Zu Dataclasses konvertieren
        groups_flat: list[JobTitleCityGroup] = []
        for g in grid_map.values():
            avg_score = None
            if g["score_count"] > 0:
                avg_score = round(g["sum_ai_score"] / g["score_count"] * 100, 1)

            groups_flat.append(
                JobTitleCityGroup(
                    hotlist_job_title=g["hotlist_job_title"],
                    city=g["city"],
                    job_count=g["job_count"],
                    total_match_count=g["total_match_count"],
                    top_ai_score=g["top_ai_score"],
                    avg_ai_score=avg_score,
                    new_count=g["new_count"],
                    presented_count=g["presented_count"],
                    job_ids=g["job_ids"],
                )
            )

        # Sortieren: Jobtitel (benutzerdefiniert), dann Stadt (benutzerdefiniert)
        groups_flat.sort(key=lambda g: (_title_sort_key(g.hotlist_job_title), _city_sort_key(g.city)))

        # In OrderedDict gruppieren: title → [cards]
        result_dict: OrderedDict[str, list[JobTitleCityGroup]] = OrderedDict()
        for group in groups_flat:
            if group.hotlist_job_title not in result_dict:
                result_dict[group.hotlist_job_title] = []
            result_dict[group.hotlist_job_title].append(group)

        return result_dict

    # ───────────────────────────────────────────────────────────
    # GRUPPEN-DETAIL (Klick auf ein Kaestchen)
    # ───────────────────────────────────────────────────────────

    async def get_group_jobs(
        self,
        job_title: str,
        city: str,
        stage: str = "new",
        category: str = "FINANCE",
    ) -> list[dict]:
        """Holt alle Jobs + Matches fuer eine Jobtitel+Stadt Kombination.

        Returns:
            Liste von dicts mit job-Info + matches-Liste
        """
        from app.models.candidate import Candidate
        from app.models.job import Job

        display_city = func.coalesce(Job.work_location_city, Job.city)

        # Subquery: Match-Aggregate pro Job (fuer Stage-Filter)
        match_agg = (
            select(
                Match.job_id,
                func.count(Match.id).label("match_count"),
                func.sum(case((Match.status == MatchStatus.PRESENTED, 1), else_=0)).label("presented_count"),
                func.sum(case((Match.status == MatchStatus.REJECTED, 1), else_=0)).label("rejected_count"),
                func.sum(case((Match.status == MatchStatus.PLACED, 1), else_=0)).label("placed_count"),
            )
            .where(and_(Match.job_id.isnot(None), Match.candidate_id.isnot(None)))
            .group_by(Match.job_id)
            .subquery()
        )

        # Jobs mit dem richtigen Titel und Stadt finden
        job_query = (
            select(Job.id, Job.position, Job.company_name, display_city.label("display_city"))
            .join(match_agg, Job.id == match_agg.c.job_id)
            .where(
                and_(
                    Job.deleted_at.is_(None),
                    or_(Job.hotlist_category == category, Job.hotlist_category.is_(None)),
                    func.coalesce(Job.hotlist_job_title, text("'Sonstige'")) == job_title,
                    func.coalesce(display_city, text("'Unbekannt'")) == city,
                )
            )
        )

        # Stage-Filter
        if stage == "new":
            job_query = job_query.where(
                and_(match_agg.c.presented_count == 0, match_agg.c.placed_count == 0)
            )
        elif stage == "in_progress":
            job_query = job_query.where(match_agg.c.presented_count > 0)
        elif stage == "archive":
            job_query = job_query.where(
                and_(
                    (match_agg.c.rejected_count + match_agg.c.placed_count) == match_agg.c.match_count,
                    match_agg.c.match_count > 0,
                )
            )

        job_result = await self.db.execute(job_query)
        job_rows = job_result.all()

        if not job_rows:
            return []

        # Fuer jeden Job die Matches laden
        jobs_with_matches = []
        for job_row in job_rows:
            match_query = (
                select(
                    Match.id,
                    Match.candidate_id,
                    Match.ai_score,
                    Match.ai_explanation,
                    Match.distance_km,
                    Match.status,
                    Match.matching_method,
                    Match.user_feedback,
                    Match.v2_score_breakdown,
                    Candidate.first_name,
                    Candidate.last_name,
                    Candidate.hotlist_job_title.label("cand_title"),
                    Candidate.hotlist_city.label("cand_city"),
                )
                .join(Candidate, Match.candidate_id == Candidate.id, isouter=True)
                .where(
                    and_(
                        Match.job_id == job_row.id,
                        Match.candidate_id.isnot(None),
                    )
                )
                .order_by(desc(Match.ai_score))
                .limit(20)
            )

            match_result = await self.db.execute(match_query)
            match_rows = match_result.all()

            matches = []
            for m in match_rows:
                name_parts = []
                if m.first_name:
                    name_parts.append(m.first_name)
                if m.last_name:
                    name_parts.append(m.last_name)

                breakdown = m.v2_score_breakdown or {}
                matches.append({
                    "match_id": m.id,
                    "candidate_id": m.candidate_id,
                    "candidate_name": " ".join(name_parts) if name_parts else "Unbekannt",
                    "candidate_title": m.cand_title or "",
                    "candidate_city": m.cand_city or "",
                    "ai_score": round(m.ai_score * 100, 1) if m.ai_score else None,
                    "ai_explanation": m.ai_explanation,
                    "distance_km": round(m.distance_km, 1) if m.distance_km else None,
                    "status": m.status.value if m.status else "new",
                    "user_feedback": m.user_feedback,
                    "qualification_tag": breakdown.get("qualification_tag"),
                    "role_multiplier": breakdown.get("role_multiplier"),
                    "job_role": breakdown.get("job_role"),
                })

            jobs_with_matches.append({
                "job_id": job_row.id,
                "position": job_row.position or "Unbekannte Position",
                "company_name": job_row.company_name or "",
                "city": job_row.display_city or "",
                "matches": matches,
                "match_count": len(matches),
            })

        return jobs_with_matches

    # ───────────────────────────────────────────────────────────
    # VERGLEICHS-MODAL
    # ───────────────────────────────────────────────────────────

    async def get_match_comparison(self, match_id: UUID) -> MatchComparisonData | None:
        """Holt alle Daten fuer den Vergleich-Dialog eines Matches.

        Laedt Job-Beschreibung, Kandidaten-CV, Adressen, Entfernung.
        """
        from app.models.candidate import Candidate
        from app.models.job import Job

        query = (
            select(
                Match.id,
                Match.ai_score,
                Match.ai_explanation,
                Match.ai_strengths,
                Match.ai_weaknesses,
                Match.distance_km,
                Match.drive_time_car_min,
                Match.drive_time_transit_min,
                Match.status,
                Match.user_feedback,
                Match.v2_score_breakdown,
                # Job
                Job.id.label("job_id"),
                Job.position,
                Job.company_name,
                func.coalesce(Job.work_location_city, Job.city).label("job_city"),
                Job.postal_code.label("job_postal_code"),
                Job.street_address.label("job_street_address"),
                Job.job_text,
                # Kandidat
                Candidate.id.label("candidate_id"),
                Candidate.first_name,
                Candidate.last_name,
                Candidate.city.label("candidate_city"),
                Candidate.postal_code.label("candidate_postal_code"),
                Candidate.street_address.label("candidate_street_address"),
                Candidate.current_position,
                Candidate.current_company,
                Candidate.work_history,
                Candidate.education,
                Candidate.further_education,
                Candidate.languages,
                Candidate.it_skills,
                Candidate.skills,
            )
            .join(Job, Match.job_id == Job.id, isouter=True)
            .join(Candidate, Match.candidate_id == Candidate.id, isouter=True)
            .where(Match.id == match_id)
        )

        result = await self.db.execute(query)
        row = result.one_or_none()

        if not row:
            return None

        name_parts = []
        if row.first_name:
            name_parts.append(row.first_name)
        if row.last_name:
            name_parts.append(row.last_name)
        full_name = " ".join(name_parts) if name_parts else "Unbekannt"

        return MatchComparisonData(
            match_id=row.id,
            ai_score=round(row.ai_score * 100, 1) if row.ai_score else None,
            ai_explanation=row.ai_explanation,
            ai_strengths=row.ai_strengths,
            ai_weaknesses=row.ai_weaknesses,
            distance_km=round(row.distance_km, 1) if row.distance_km else None,
            drive_time_car_min=row.drive_time_car_min,
            drive_time_transit_min=row.drive_time_transit_min,
            status=row.status.value if row.status else "new",
            user_feedback=row.user_feedback,
            v2_score_breakdown=row.v2_score_breakdown,
            job_id=row.job_id,
            job_position=row.position or "Unbekannte Position",
            job_company_name=row.company_name or "",
            job_city=row.job_city or "",
            job_postal_code=row.job_postal_code or "",
            job_street_address=row.job_street_address or "",
            job_text=row.job_text or "",
            candidate_id=row.candidate_id,
            candidate_name=full_name,
            candidate_city=row.candidate_city or "",
            candidate_postal_code=row.candidate_postal_code or "",
            candidate_street_address=row.candidate_street_address or "",
            candidate_current_position=row.current_position or "",
            candidate_current_company=row.current_company or "",
            work_history=row.work_history,
            education=row.education,
            further_education=row.further_education,
            languages=row.languages,
            it_skills=row.it_skills,
            skills=row.skills,
        )

    # ───────────────────────────────────────────────────────────
    # BESTEHENDE METHODEN (unveraendert)
    # ───────────────────────────────────────────────────────────

    async def get_jobs_overview(
        self,
        category: str = "FINANCE",
        stage: str = "new",
        search: str | None = None,
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[list[JobMatchSummary], int]:
        """Holt Jobs gruppiert nach Lifecycle-Stufe (Legacy/Detail-Ansicht)."""
        from app.models.job import Job

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
            .where(and_(Match.job_id.isnot(None), Match.candidate_id.isnot(None)))
            .group_by(Match.job_id)
            .subquery()
        )

        query = (
            select(
                Job.id, Job.position, Job.company_name, Job.city, Job.created_at,
                match_agg.c.match_count, match_agg.c.top_ai_score, match_agg.c.avg_ai_score,
                match_agg.c.new_count, match_agg.c.ai_checked_count,
                match_agg.c.presented_count, match_agg.c.rejected_count, match_agg.c.placed_count,
            )
            .join(match_agg, Job.id == match_agg.c.job_id)
            .where(
                and_(
                    Job.deleted_at.is_(None),
                    or_(Job.hotlist_category == category, Job.hotlist_category.is_(None)),
                )
            )
        )

        if stage == "new":
            query = query.where(and_(match_agg.c.presented_count == 0, match_agg.c.placed_count == 0))
        elif stage == "in_progress":
            query = query.where(match_agg.c.presented_count > 0)
        elif stage == "archive":
            query = query.where(
                and_(
                    (match_agg.c.rejected_count + match_agg.c.placed_count) == match_agg.c.match_count,
                    match_agg.c.match_count > 0,
                )
            )

        if search:
            search_term = f"%{search}%"
            query = query.where(
                or_(Job.position.ilike(search_term), Job.company_name.ilike(search_term), Job.city.ilike(search_term))
            )

        count_query = select(func.count()).select_from(query.subquery())
        total = (await self.db.execute(count_query)).scalar() or 0

        query = query.order_by(desc(match_agg.c.top_ai_score))
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
        self, job_id: UUID, sort_by: str = "ai_score", limit: int = 10,
    ) -> list[MatchDetail]:
        """Holt die Top-N Matches fuer einen bestimmten Job."""
        from app.models.candidate import Candidate

        query = (
            select(
                Match.id, Match.candidate_id, Match.ai_score, Match.ai_explanation,
                Match.ai_strengths, Match.ai_weaknesses, Match.distance_km, Match.status,
                Match.matching_method, Match.user_feedback, Match.feedback_note, Match.created_at,
                Match.drive_time_car_min, Match.drive_time_transit_min,
                Candidate.first_name, Candidate.last_name, Candidate.hotlist_job_title, Candidate.hotlist_city,
            )
            .join(Candidate, Match.candidate_id == Candidate.id, isouter=True)
            .where(and_(Match.job_id == job_id, Match.candidate_id.isnot(None)))
        )

        if sort_by == "distance":
            query = query.order_by(Match.distance_km.asc().nullslast())
        elif sort_by == "drive_time":
            query = query.order_by(Match.drive_time_car_min.asc().nullslast())
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
                    drive_time_car_min=row.drive_time_car_min,
                    drive_time_transit_min=row.drive_time_transit_min,
                    status=row.status.value if row.status else "new",
                    matching_method=row.matching_method,
                    user_feedback=row.user_feedback,
                    feedback_note=row.feedback_note,
                    created_at=row.created_at,
                )
            )

        return details

    async def update_match_status(self, match_id: UUID, new_status: str) -> Match | None:
        """Aktualisiert den Status eines Matches."""
        result = await self.db.execute(select(Match).where(Match.id == match_id))
        match = result.scalar_one_or_none()

        if not match:
            return None

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

    async def save_feedback(self, match_id: UUID, feedback: str, note: str | None = None) -> Match | None:
        """Speichert Recruiter-Feedback fuer ein Match."""
        from datetime import datetime, timezone

        result = await self.db.execute(select(Match).where(Match.id == match_id))
        match = result.scalar_one_or_none()

        if not match:
            return None

        match.user_feedback = feedback
        match.feedback_note = note
        match.feedback_at = datetime.now(timezone.utc)

        await self.db.commit()
        return match

    async def get_memory_warnings(self, candidate_id: UUID) -> list[dict]:
        """Holt Memory-Warnungen fuer einen Kandidaten.

        Returns:
            Liste von Warnungen wie:
            [{"company_name": "Firma X", "action": "rejected", "created_at": "..."}]
        """
        try:
            from app.models.mt_match_memory import MTMatchMemory
            from app.models.company import Company

            result = await self.db.execute(
                select(
                    MTMatchMemory.action,
                    MTMatchMemory.rejection_reason,
                    MTMatchMemory.never_again_company,
                    MTMatchMemory.created_at,
                    Company.name.label("company_name"),
                )
                .join(Company, MTMatchMemory.company_id == Company.id, isouter=True)
                .where(MTMatchMemory.candidate_id == candidate_id)
                .order_by(MTMatchMemory.created_at.desc())
                .limit(10)
            )
            rows = result.all()

            return [
                {
                    "action": row.action,
                    "reason": row.rejection_reason,
                    "never_again": row.never_again_company,
                    "created_at": row.created_at.strftime("%d.%m.%Y") if row.created_at else "",
                    "company_name": row.company_name or "Unbekannt",
                }
                for row in rows
            ]
        except Exception as e:
            logger.warning(f"Memory-Abfrage fehlgeschlagen: {e}")
            return []

    async def get_job_detail(self, job_id: UUID) -> dict | None:
        """Holt detaillierte Job-Informationen fuer die Detail-Ansicht."""
        from app.models.job import Job

        result = await self.db.execute(select(Job).where(Job.id == job_id))
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

    # ───────────────────────────────────────────────────────────
    # SIDEBAR-DATEN (Jobtitel → Staedte)
    # ───────────────────────────────────────────────────────────

    async def get_sidebar_data(
        self,
        category: str = "FINANCE",
        stage: str = "new",
    ) -> list[dict]:
        """Liefert Jobtitel-Liste mit verschachtelten Stadt-Listen fuer die Sidebar.

        Returns:
            [{
                "title": "Bilanzbuchhalter/in",
                "total_matches": 47,
                "cities": [
                    {"city": "München", "match_count": 15, "avg_score": 78.3,
                     "new_count": 5, "top_score": 92.1}
                ]
            }, ...]
        """
        from app.models.job import Job

        eff = _effective_score()
        display_city = func.coalesce(Job.work_location_city, Job.city)

        # Subquery: Match-Aggregate pro Job (fuer Stage-Filter)
        match_agg = (
            select(
                Match.job_id,
                func.count(Match.id).label("match_count"),
                func.sum(case((Match.status == MatchStatus.PRESENTED, 1), else_=0)).label("presented_count"),
                func.sum(case((Match.status == MatchStatus.REJECTED, 1), else_=0)).label("rejected_count"),
                func.sum(case((Match.status == MatchStatus.PLACED, 1), else_=0)).label("placed_count"),
            )
            .where(and_(Match.job_id.isnot(None), Match.candidate_id.isnot(None)))
            .group_by(Match.job_id)
            .subquery()
        )

        # Haupt-Query: Matches gruppiert nach Jobtitel + Stadt
        query = (
            select(
                func.coalesce(Job.hotlist_job_title, text("'Sonstige'")).label("job_title"),
                func.coalesce(display_city, text("'Unbekannt'")).label("display_city"),
                func.count(Match.id).label("match_count"),
                func.avg(eff).label("avg_score"),
                func.max(eff).label("top_score"),
                func.sum(case(
                    (Match.status.in_([MatchStatus.NEW, MatchStatus.AI_CHECKED]), 1),
                    else_=0
                )).label("new_count"),
            )
            .select_from(Match)
            .join(Job, Match.job_id == Job.id)
            .join(match_agg, Job.id == match_agg.c.job_id)
            .where(
                and_(
                    Job.deleted_at.is_(None),
                    Match.candidate_id.isnot(None),
                    or_(Job.hotlist_category == category, Job.hotlist_category.is_(None)),
                )
            )
        )

        # Stage-Filter (selbe Logik wie get_grid_overview)
        if stage == "new":
            query = query.where(and_(match_agg.c.presented_count == 0, match_agg.c.placed_count == 0))
        elif stage == "in_progress":
            query = query.where(match_agg.c.presented_count > 0)
        elif stage == "archive":
            query = query.where(
                and_(
                    (match_agg.c.rejected_count + match_agg.c.placed_count) == match_agg.c.match_count,
                    match_agg.c.match_count > 0,
                )
            )

        query = query.group_by(
            func.coalesce(Job.hotlist_job_title, text("'Sonstige'")),
            func.coalesce(display_city, text("'Unbekannt'")),
        )

        result = await self.db.execute(query)
        rows = result.all()

        # In Python gruppieren: title → cities (mit Stadt-Normalisierung)
        title_map: dict[str, dict] = {}
        for row in rows:
            title = row.job_title or "Sonstige"
            raw_city = row.display_city or "Unbekannt"
            city = _normalize_city(raw_city)

            if title not in title_map:
                title_map[title] = {"title": title, "total_matches": 0, "cities_map": {}}

            cities_map = title_map[title]["cities_map"]
            if city not in cities_map:
                cities_map[city] = {
                    "city": city,
                    "match_count": 0,
                    "sum_score": 0.0,
                    "score_count": 0,
                    "top_score": 0.0,
                    "new_count": 0,
                    "raw_cities": [],  # Alle Original-Stadtnamen fuer den Filter
                }

            c = cities_map[city]
            mc = int(row.match_count or 0)
            c["match_count"] += mc
            c["new_count"] += int(row.new_count or 0)
            title_map[title]["total_matches"] += mc

            if raw_city not in c["raw_cities"]:
                c["raw_cities"].append(raw_city)

            if row.top_score:
                ts = round(float(row.top_score), 1)
                if ts > c["top_score"]:
                    c["top_score"] = ts

            if row.avg_score:
                c["sum_score"] += float(row.avg_score) * mc
                c["score_count"] += mc

        # Zu finaler Struktur konvertieren
        result_list = []
        for title_data in title_map.values():
            cities = []
            for c in title_data["cities_map"].values():
                avg = round(c["sum_score"] / c["score_count"], 1) if c["score_count"] > 0 else 0
                cities.append({
                    "city": c["city"],
                    "match_count": c["match_count"],
                    "avg_score": avg,
                    "top_score": c["top_score"],
                    "new_count": c["new_count"],
                    "raw_cities": c["raw_cities"],
                })
            result_list.append({
                "title": title_data["title"],
                "total_matches": title_data["total_matches"],
                "cities": cities,
            })

        # Sortieren: TITLE_ORDER + Staedte nach avg_score DESC
        result_list.sort(key=lambda t: _title_sort_key(t["title"]))

        for title_data in result_list:
            title_data["cities"].sort(key=lambda c: (-c["avg_score"], _city_sort_key(c["city"])))

        return result_list

    # ───────────────────────────────────────────────────────────
    # PAGINIERTE MATCHES (Tabelle)
    # ───────────────────────────────────────────────────────────

    async def _get_raw_cities_for_normalized(
        self,
        title: str,
        normalized_city: str,
        stage: str = "new",
        category: str = "FINANCE",
    ) -> list[str]:
        """Findet alle Original-Stadtnamen die zum normalisierten Namen gehoeren.

        Beispiel: normalized_city="Berlin" → ["Berlin", "Berlin-Wilmersdorf", "10117 Berlin Berlin"]
        """
        from app.models.job import Job

        display_city = func.coalesce(Job.work_location_city, Job.city)

        # Subquery: Match-Aggregate pro Job (fuer Stage-Filter)
        match_agg = (
            select(
                Match.job_id,
                func.count(Match.id).label("match_count"),
                func.sum(case((Match.status == MatchStatus.PRESENTED, 1), else_=0)).label("presented_count"),
                func.sum(case((Match.status == MatchStatus.REJECTED, 1), else_=0)).label("rejected_count"),
                func.sum(case((Match.status == MatchStatus.PLACED, 1), else_=0)).label("placed_count"),
            )
            .where(and_(Match.job_id.isnot(None), Match.candidate_id.isnot(None)))
            .group_by(Match.job_id)
            .subquery()
        )

        query = (
            select(func.distinct(func.coalesce(display_city, text("'Unbekannt'"))).label("raw_city"))
            .select_from(Match)
            .join(Job, Match.job_id == Job.id)
            .join(match_agg, Job.id == match_agg.c.job_id)
            .where(
                and_(
                    Job.deleted_at.is_(None),
                    Match.candidate_id.isnot(None),
                    or_(Job.hotlist_category == category, Job.hotlist_category.is_(None)),
                    func.coalesce(Job.hotlist_job_title, text("'Sonstige'")) == title,
                )
            )
        )

        # Stage-Filter
        if stage == "new":
            query = query.where(and_(match_agg.c.presented_count == 0, match_agg.c.placed_count == 0))
        elif stage == "in_progress":
            query = query.where(match_agg.c.presented_count > 0)
        elif stage == "archive":
            query = query.where(and_(
                (match_agg.c.rejected_count + match_agg.c.placed_count) == match_agg.c.match_count,
                match_agg.c.match_count > 0,
            ))

        result = await self.db.execute(query)
        all_raw = [row.raw_city for row in result.all()]

        # Nur die zurueckgeben, deren Normalisierung == normalized_city
        return [rc for rc in all_raw if _normalize_city(rc) == normalized_city]

    async def get_paginated_matches(
        self,
        title: str,
        city: str,
        stage: str = "new",
        status_filter: str | None = None,
        score_min: int | None = None,
        score_max: int | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        page: int = 1,
        per_page: int = 15,
        sort_by: str = "score",
        sort_dir: str = "desc",
        category: str = "FINANCE",
    ) -> dict:
        """Paginierte Matches fuer Jobtitel + Stadt.

        Returns:
            {
                "matches": [...], "total": int, "page": int, "per_page": int,
                "pages": int, "status_counts": {"all": N, "new": N, "presented": N, "rejected": N}
            }
        """
        from app.models.candidate import Candidate
        from app.models.job import Job

        eff = _effective_score()
        display_city = func.coalesce(Job.work_location_city, Job.city)
        now = func.now()

        # Fix 11: Alle Original-Stadtnamen fuer den normalisierten Namen finden
        raw_cities = await self._get_raw_cities_for_normalized(title, city, stage, category)
        if not raw_cities:
            # Fallback: Exakter Match (falls keine raw cities gefunden)
            raw_cities = [city]

        # Subquery: Match-Aggregate pro Job (fuer Stage-Filter)
        match_agg = (
            select(
                Match.job_id,
                func.count(Match.id).label("match_count"),
                func.sum(case((Match.status == MatchStatus.PRESENTED, 1), else_=0)).label("presented_count"),
                func.sum(case((Match.status == MatchStatus.REJECTED, 1), else_=0)).label("rejected_count"),
                func.sum(case((Match.status == MatchStatus.PLACED, 1), else_=0)).label("placed_count"),
            )
            .where(and_(Match.job_id.isnot(None), Match.candidate_id.isnot(None)))
            .group_by(Match.job_id)
            .subquery()
        )

        # Base Query: Alle Matches fuer diesen Jobtitel + Stadt (IN statt ==)
        base_where = and_(
            Job.deleted_at.is_(None),
            Match.candidate_id.isnot(None),
            or_(Job.hotlist_category == category, Job.hotlist_category.is_(None)),
            func.coalesce(Job.hotlist_job_title, text("'Sonstige'")) == title,
            func.coalesce(display_city, text("'Unbekannt'")).in_(raw_cities),
        )

        # Stage-Filter
        stage_where = []
        if stage == "new":
            stage_where.append(and_(match_agg.c.presented_count == 0, match_agg.c.placed_count == 0))
        elif stage == "in_progress":
            stage_where.append(match_agg.c.presented_count > 0)
        elif stage == "archive":
            stage_where.append(and_(
                (match_agg.c.rejected_count + match_agg.c.placed_count) == match_agg.c.match_count,
                match_agg.c.match_count > 0,
            ))

        # Date-Range-Filter (v2_matched_at bevorzugt, Fallback auf created_at)
        date_clauses = []
        if date_from or date_to:
            from datetime import date as date_type
            match_date = func.coalesce(Match.v2_matched_at, Match.created_at)
            if date_from:
                try:
                    d = date_type.fromisoformat(date_from)
                    date_clauses.append(func.date(match_date) >= d)
                except ValueError:
                    pass
            if date_to:
                try:
                    d = date_type.fromisoformat(date_to)
                    date_clauses.append(func.date(match_date) <= d)
                except ValueError:
                    pass

        # 1) Status-Counts berechnen (ohne Status-Filter)
        count_query = (
            select(
                func.count(Match.id).label("total"),
                func.sum(case(
                    (Match.status.in_([MatchStatus.NEW, MatchStatus.AI_CHECKED]), 1),
                    else_=0
                )).label("new_count"),
                func.sum(case((Match.status == MatchStatus.PRESENTED, 1), else_=0)).label("presented_count"),
                func.sum(case((Match.status == MatchStatus.REJECTED, 1), else_=0)).label("rejected_count"),
            )
            .select_from(Match)
            .join(Job, Match.job_id == Job.id)
            .join(match_agg, Job.id == match_agg.c.job_id)
            .where(base_where)
        )
        if stage_where:
            count_query = count_query.where(*stage_where)
        if date_clauses:
            count_query = count_query.where(*date_clauses)

        count_result = await self.db.execute(count_query)
        cr = count_result.one()
        status_counts = {
            "all": int(cr.total or 0),
            "new": int(cr.new_count or 0),
            "presented": int(cr.presented_count or 0),
            "rejected": int(cr.rejected_count or 0),
        }

        # 2) Haupt-Query mit Status-Filter
        query = (
            select(
                Match.id.label("match_id"),
                Match.candidate_id,
                Match.job_id,
                Match.status,
                Match.distance_km,
                Match.drive_time_car_min,
                Match.drive_time_transit_min,
                Match.user_feedback,
                Match.v2_score_breakdown,
                Match.created_at.label("match_created_at"),
                eff.label("score"),
                Candidate.first_name,
                Candidate.last_name,
                Candidate.current_position,
                Candidate.current_company,
                Candidate.city.label("cand_city"),
                Candidate.salary,
                Candidate.email,
                Candidate.updated_at.label("cand_updated_at"),
                Candidate.v2_seniority_level,
                Job.position.label("job_position"),
                Job.company_name.label("job_company"),
            )
            .select_from(Match)
            .join(Job, Match.job_id == Job.id)
            .join(Candidate, Match.candidate_id == Candidate.id)
            .join(match_agg, Job.id == match_agg.c.job_id)
            .where(base_where)
        )
        if stage_where:
            query = query.where(*stage_where)
        if date_clauses:
            query = query.where(*date_clauses)

        # Status-Filter
        if status_filter == "new":
            query = query.where(Match.status.in_([MatchStatus.NEW, MatchStatus.AI_CHECKED]))
        elif status_filter == "presented":
            query = query.where(Match.status == MatchStatus.PRESENTED)
        elif status_filter == "rejected":
            query = query.where(Match.status == MatchStatus.REJECTED)
        else:
            # Standard: REJECTED Matches ausblenden (nur ueber expliziten Filter sichtbar)
            query = query.where(Match.status != MatchStatus.REJECTED)

        # Score-Range-Filter (0-100 Skala, auf Integer gerundet damit 79.5 → 79 korrekt in 65-79 fällt)
        if score_min is not None:
            query = query.where(func.floor(eff) >= score_min)
        if score_max is not None:
            query = query.where(func.floor(eff) <= score_max)

        # Total fuer Pagination (mit Status-Filter)
        total_query = select(func.count()).select_from(query.subquery())
        total = (await self.db.execute(total_query)).scalar() or 0

        # Sortierung
        if sort_by == "name":
            order_col = Candidate.last_name
        elif sort_by == "distance":
            order_col = Match.distance_km
        elif sort_by == "drive_time":
            order_col = Match.drive_time_car_min
        elif sort_by == "activity":
            order_col = Candidate.updated_at
        else:  # score (default)
            order_col = eff

        if sort_dir == "asc":
            query = query.order_by(order_col.asc().nullslast())
        else:
            query = query.order_by(order_col.desc().nullsfirst())

        # Pagination
        offset = (page - 1) * per_page
        query = query.offset(offset).limit(per_page)

        result = await self.db.execute(query)
        rows = result.all()

        # Zu dicts konvertieren
        matches = []
        for i, row in enumerate(rows):
            name_parts = []
            if row.first_name:
                name_parts.append(row.first_name)
            if row.last_name:
                name_parts.append(row.last_name)

            # Aktivitaet berechnen (Tage seit letztem Update)
            activity_days = None
            if row.cand_updated_at:
                try:
                    now_dt = datetime.now(timezone.utc)
                    updated = row.cand_updated_at
                    if updated.tzinfo is None:
                        from datetime import timezone as tz
                        updated = updated.replace(tzinfo=tz.utc)
                    activity_days = (now_dt - updated).days
                except Exception:
                    activity_days = None

            # Status-Label und Farbe
            status_val = row.status.value if row.status else "new"

            matches.append({
                "rank": offset + i + 1,
                "match_id": str(row.match_id),
                "candidate_id": str(row.candidate_id) if row.candidate_id else None,
                "name": " ".join(name_parts) if name_parts else "Unbekannt",
                "current_position": row.current_position or "",
                "current_company": row.current_company or "",
                "score": round(float(row.score), 1) if row.score else 0,
                "status": status_val,
                "distance_km": round(float(row.distance_km), 1) if row.distance_km else None,
                "drive_time_car_min": row.drive_time_car_min,
                "drive_time_transit_min": row.drive_time_transit_min,
                "activity_days": activity_days,
                "salary": row.salary or "",
                "email": row.email or "",
                "seniority_level": row.v2_seniority_level,
                "user_feedback": row.user_feedback,
                "job_position": row.job_position or "",
                "job_company": row.job_company or "",
            })

        pages = (total + per_page - 1) // per_page if per_page > 0 else 0

        return {
            "matches": matches,
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": pages,
            "status_counts": status_counts,
        }

    # ───────────────────────────────────────────────────────────
    # BULK STATUS UPDATE
    # ───────────────────────────────────────────────────────────

    async def bulk_update_status(
        self,
        match_ids: list[UUID],
        new_status: str,
    ) -> int:
        """Aktualisiert den Status mehrerer Matches in einer Query.

        Returns:
            Anzahl aktualisierter Matches.
        """
        status_map = {
            "new": MatchStatus.NEW,
            "ai_checked": MatchStatus.AI_CHECKED,
            "presented": MatchStatus.PRESENTED,
            "rejected": MatchStatus.REJECTED,
            "placed": MatchStatus.PLACED,
        }

        if new_status not in status_map:
            return 0

        if not match_ids:
            return 0

        stmt = (
            update(Match)
            .where(Match.id.in_(match_ids))
            .values(status=status_map[new_status])
        )

        result = await self.db.execute(stmt)
        await self.db.flush()

        count = result.rowcount or 0
        logger.info(f"Bulk Status Update: {count} Matches → {new_status}")
        return count

    # ───────────────────────────────────────────────────────────
    # CSV-EXPORT
    # ───────────────────────────────────────────────────────────

    async def get_export_data(self, match_ids: list[UUID]) -> list[dict]:
        """Holt Match + Candidate + Job Daten fuer CSV-Export.

        Returns:
            Liste von dicts mit allen exportierbaren Feldern.
        """
        from app.models.candidate import Candidate
        from app.models.job import Job

        if not match_ids:
            return []

        eff = _effective_score()

        query = (
            select(
                Match.id.label("match_id"),
                eff.label("score"),
                Match.distance_km,
                Match.status,
                Candidate.first_name,
                Candidate.last_name,
                Candidate.email,
                Candidate.phone,
                Candidate.current_position,
                Candidate.current_company,
                Candidate.city.label("cand_city"),
                Candidate.salary,
                Job.position.label("job_position"),
                Job.company_name.label("job_company"),
                func.coalesce(Job.work_location_city, Job.city).label("job_city"),
            )
            .select_from(Match)
            .join(Candidate, Match.candidate_id == Candidate.id, isouter=True)
            .join(Job, Match.job_id == Job.id, isouter=True)
            .where(Match.id.in_(match_ids))
            .order_by(eff.desc().nullsfirst())
        )

        result = await self.db.execute(query)
        rows = result.all()

        export = []
        for row in rows:
            name_parts = []
            if row.first_name:
                name_parts.append(row.first_name)
            if row.last_name:
                name_parts.append(row.last_name)

            status_labels = {
                "new": "Neu",
                "ai_checked": "Bewertet",
                "presented": "Vorgestellt",
                "rejected": "Abgelehnt",
                "placed": "Vermittelt",
            }
            status_val = row.status.value if row.status else "new"

            export.append({
                "Name": " ".join(name_parts) if name_parts else "Unbekannt",
                "E-Mail": row.email or "",
                "Telefon": row.phone or "",
                "Position": row.current_position or "",
                "Unternehmen": row.current_company or "",
                "Stadt": row.cand_city or "",
                "Gehalt": row.salary or "",
                "Score": round(float(row.score), 1) if row.score else 0,
                "Entfernung (km)": round(float(row.distance_km), 1) if row.distance_km else "",
                "Status": status_labels.get(status_val, status_val),
                "Stelle": row.job_position or "",
                "Firma": row.job_company or "",
                "Standort": row.job_city or "",
            })

        return export
