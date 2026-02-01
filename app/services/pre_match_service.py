"""Pre-Match Service v2 — Intelligente Filterung mit Kalibrierung.

Unterschied zu v1:
- v1: Blinder Array-Overlap (gemeinsamer Jobtitel?) + Top 15 nach Entfernung
- v2: Kalibrierte Rollen-Similarity + Keyword-Qualitaet + Score-basiertes Cutoff

Filterung v2:
1. Innerhalb 30km (PostGIS, BLEIBT — harte Grenze)
2. Gleiche Kategorie (z.B. FINANCE)
3. Rollen-Similarity >= MIN_ROLE_SIMILARITY (kalibriert oder Matrix)
4. Pre-Score >= MIN_PRE_SCORE (nach Berechnung)
5. Kein hartes Kandidaten-Limit pro Job mehr — Score entscheidet

Kalibrierungsdaten werden automatisch geladen und angewendet:
- Rollen-Matrix-Overrides (AI-gelernt)
- Power-Keywords (zaehlen doppelt)
- Penalty-Keywords (zaehlen halb)
- Ausschluss-Paare (Match wird gar nicht erst erstellt)
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

# Maximale Distanz fuer Pre-Matches (EINZIGE harte Grenze — User-Anforderung)
PRE_MATCH_MAX_DISTANCE_KM = 30
METERS_PER_KM = 1000

# Mindest-Rollen-Similarity damit ein Match erstellt wird (0.0-1.0)
# 0.25 = z.B. Fibu→Kredi (0.60) ja, Lohn→Fibu (0.05) nein
MIN_ROLE_SIMILARITY = 0.25

# Mindest-Pre-Score damit ein Match gespeichert wird (0-100)
# 35 = filtert mittelmäßige Paarungen raus (z.B. Kredi→Bilu weit weg ohne Keywords)
MIN_PRE_SCORE = 35.0

# Max. Kandidaten pro Job — sortiert nach Pre-Score (beste zuerst!)
# 15 = fokussiert auf die wirklich passenden Kandidaten
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
    matches_filtered_out: int = 0  # v2: Durch intelligente Filterung aussortiert
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
        self._scorer = None  # Wird in generate_all() initialisiert

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

        v2 — Intelligente Filterung:
        1. Laedt Kalibrierungsdaten (AI-gelernte Rollen-Overrides + Keywords)
        2. Fuer jeden Job: Finde Kandidaten innerhalb 30km
        3. Pruefe Rollen-Similarity (kalibriert!) >= Schwelle
        4. Berechne Pre-Score sofort bei Erstellung
        5. Nur Matches mit Pre-Score >= MIN_PRE_SCORE werden gespeichert
        6. Sortierung nach Pre-Score (nicht Entfernung!)

        Args:
            category: Kategorie (z.B. "FINANCE")
            progress_callback: Optional callback(step, detail)

        Returns:
            PreMatchGenerateResult mit Statistiken
        """
        result = PreMatchGenerateResult()

        # ── Schritt 1: Kalibrierungsdaten laden ──
        if progress_callback:
            progress_callback("pre_match", "Lade Kalibrierungsdaten...")

        from app.services.pre_scoring_service import PreScoringService
        self._scorer = PreScoringService(self.db)
        await self._scorer.load_calibration()

        cal_info = "ohne Kalibrierung"
        if self._scorer._calibration is not None:
            n_overrides = len(self._scorer._role_overrides)
            n_keywords = len(self._scorer._keyword_weights)
            n_exclusions = len(self._scorer._exclusion_set)
            cal_info = (
                f"mit Kalibrierung: {n_overrides} Rollen-Overrides, "
                f"{n_keywords} Keyword-Gewichte, {n_exclusions} Ausschluss-Paare"
            )
        logger.info(f"Pre-Match v2: Starte {cal_info}")

        # ── Schritt 2: Jobs laden ──
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

        logger.info(f"Pre-Match v2: {len(jobs)} Jobs in Kategorie {category}")

        if progress_callback:
            progress_callback("pre_match", f"{len(jobs)} Jobs gefunden ({cal_info})")

        # ── Schritt 3: Fuer jeden Job intelligente Matches generieren ──
        batch_count = 0
        total_filtered_out = 0
        for job in jobs:
            try:
                created, filtered = await self._generate_matches_for_job_v2(job, category)
                result.matches_created += created
                total_filtered_out += filtered
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
                            f"{result.matches_created} Matches erstellt, "
                            f"{total_filtered_out} ausgefiltert",
                        )

            except Exception as e:
                logger.error(f"Pre-Match Fehler bei Job {job.id}: {e}")
                result.errors.append(f"Job {job.id}: {str(e)[:100]}")

        # Finaler Commit
        await self.db.commit()

        result.matches_filtered_out = total_filtered_out

        logger.info(
            f"Pre-Match v2 Generierung abgeschlossen: "
            f"{result.combos_processed} Jobs, {result.matches_created} Matches erstellt, "
            f"{total_filtered_out} durch intelligente Filterung ausgefiltert"
        )

        return result

    async def _generate_matches_for_job_v2(
        self, job: Job, category: str,
    ) -> tuple[int, int]:
        """
        Generiert Matches fuer einen Job — v2 mit intelligenter Filterung.

        Ablauf:
        1. Finde alle Kandidaten innerhalb 30km (gleiche Kategorie, aktiv)
           → KEIN Titel-Filter auf DB-Ebene! Wir laden ALLE nahen Kandidaten.
        2. Fuer jeden Kandidaten: Berechne kalibrierte Rollen-Similarity
           → Pruefe Ausschluss-Paare (Score 0)
           → Pruefe Mindest-Similarity (>= 0.25)
        3. Berechne vollstaendigen Pre-Score
           → Matches mit Pre-Score < 20 werden verworfen
        4. Sortiere nach Pre-Score (beste zuerst)
        5. Max MAX_CANDIDATES_PER_JOB als Sicherheitslimit

        Returns:
            (erstellt, ausgefiltert) — Anzahl erstellter und ausgefilterter Matches
        """
        if not job.location_coords or not job.hotlist_job_titles:
            return 0, 0

        # Distanz-Expression
        distance_expr = func.ST_Distance(
            Candidate.address_coords,
            job.location_coords,
            True,  # use_spheroid
        )

        # ── DB-Query: Alle Kandidaten innerhalb 30km (ohne Titel-Filter!) ──
        candidates_query = (
            select(
                Candidate,
                (distance_expr / METERS_PER_KM).label("distance_km"),
            )
            .where(
                and_(
                    # Basis-Filter
                    Candidate.hotlist_category == category,
                    Candidate.address_coords.is_not(None),
                    Candidate.hidden == False,  # noqa: E712
                    Candidate.deleted_at.is_(None),
                    # Distanz max 30km — EINZIGE harte Grenze
                    func.ST_DWithin(
                        Candidate.address_coords,
                        job.location_coords,
                        PRE_MATCH_MAX_DISTANCE_KM * METERS_PER_KM,
                        True,
                    ),
                    # Keine Fuehrungskraefte
                    or_(
                        Candidate.classification_data.is_(None),
                        Candidate.classification_data["is_leadership"].astext != "true",
                    ),
                )
            )
            .order_by((distance_expr / METERS_PER_KM).asc())
        )

        result = await self.db.execute(candidates_query)
        nearby_candidates = result.all()

        if not nearby_candidates:
            return 0, 0

        # Bestehende Matches fuer diesen Job laden
        existing_query = select(Match.candidate_id).where(Match.job_id == job.id)
        existing_result = await self.db.execute(existing_query)
        existing_candidate_ids = {row[0] for row in existing_result}

        # ── Intelligente Filterung: Rollen-Similarity + Pre-Score ──
        scored_candidates = []
        filtered_out = 0

        for candidate, distance_km in nearby_candidates:
            if candidate.id in existing_candidate_ids:
                continue  # Match existiert bereits

            # Schritt 2a: Ausschluss-Paare pruefen
            if self._scorer and self._scorer._exclusion_set:
                job_role = (job.hotlist_job_title or "").strip()
                cand_role = (candidate.hotlist_job_title or "").strip()
                if job_role and cand_role and (job_role, cand_role) in self._scorer._exclusion_set:
                    filtered_out += 1
                    continue

            # Schritt 2b: Rollen-Similarity berechnen (kalibriert)
            if self._scorer:
                role_sim, _, _ = self._scorer._calculate_role_similarity(candidate, job)
            else:
                # Fallback ohne Scorer: einfacher Titel-Vergleich
                role_sim = self._simple_role_check(candidate, job)

            if role_sim < MIN_ROLE_SIMILARITY:
                filtered_out += 1
                continue

            # Schritt 3: Temporaeres Match-Objekt fuer Pre-Score-Berechnung
            temp_match = Match(
                job_id=job.id,
                candidate_id=candidate.id,
                distance_km=round(distance_km, 2),
                status=MatchStatus.NEW,
            )

            # Pre-Score sofort berechnen
            if self._scorer:
                breakdown = self._scorer.calculate_pre_score(candidate, job, temp_match)
                pre_score = breakdown.total
            else:
                pre_score = role_sim * 50  # Grober Fallback

            # Schritt 4: Mindest-Pre-Score pruefen
            if pre_score < MIN_PRE_SCORE:
                filtered_out += 1
                continue

            # Match-Objekt mit Score versehen
            temp_match.pre_score = pre_score
            temp_match.keyword_score = temp_match.keyword_score  # wurde ggf. inline gesetzt
            temp_match.matched_keywords = temp_match.matched_keywords  # wurde ggf. inline gesetzt

            scored_candidates.append((temp_match, pre_score))

        # Schritt 5: Sortiere nach Pre-Score (beste zuerst) + Sicherheitslimit
        scored_candidates.sort(key=lambda x: x[1], reverse=True)

        created = 0
        for match_obj, _score in scored_candidates[:MAX_CANDIDATES_PER_JOB]:
            self.db.add(match_obj)
            created += 1

        # Was ueber dem Sicherheitslimit liegt, zaehlt auch als gefiltert
        if len(scored_candidates) > MAX_CANDIDATES_PER_JOB:
            filtered_out += len(scored_candidates) - MAX_CANDIDATES_PER_JOB

        return created, filtered_out

    @staticmethod
    def _simple_role_check(candidate: Candidate, job: Job) -> float:
        """Fallback Rollen-Check ohne Kalibrierung (einfacher Titel-Vergleich)."""
        job_titles = set(t.lower().strip() for t in (job.hotlist_job_titles or []))
        cand_titles = set(t.lower().strip() for t in (candidate.hotlist_job_titles or []))

        if not job_titles or not cand_titles:
            return 0.0

        # Gemeinsame Titel
        common = job_titles & cand_titles
        if common:
            return 1.0

        # Kein gemeinsamer Titel
        return 0.0

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
