"""Matching Engine v2 — 3-Schichten Enterprise-Matching.

Schicht 1: Hard Filters (SQL, <5ms) — eliminiert 85-90% der Kandidaten
Schicht 2: Structured Scoring (Python, <50ms/200 Kandidaten) — gewichtete Score-Berechnung
Schicht 3: Pattern Boost/Penalty — gelernte Regeln anwenden

Kosten pro Match: $0.00 (alles lokal/vorberechnet)
"""

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Sequence
from uuid import UUID

from sqlalchemy import select, func, and_, or_, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.candidate import Candidate
from app.models.job import Job
from app.models.match import Match, MatchStatus
from app.models.match_v2_models import (
    MatchV2LearnedRule,
    MatchV2ScoringWeight,
)
from app.services.local_embedding_service import EmbeddingService

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════
# DATENKLASSEN
# ══════════════════════════════════════════════════════════════════


@dataclass
class MatchCandidate:
    """Kandidat nach Hard-Filter, bereit fuer Scoring."""
    id: UUID
    seniority_level: int
    career_trajectory: str
    years_experience: int
    structured_skills: list[dict]
    current_role_summary: str
    embedding_current: list[float] | None
    embedding_full: list[float] | None
    city: str | None
    hotlist_category: str | None
    distance_km: float | None = None  # Entfernung zum Job in km (NULL = keine Geodaten)


@dataclass
class ScoredMatch:
    """Ergebnis des Scorings fuer einen Kandidaten."""
    candidate_id: UUID
    total_score: float  # 0-100
    breakdown: dict  # {skill_overlap, seniority_fit, ...}
    rank: int = 0


@dataclass
class MatchResult:
    """Gesamtergebnis fuer einen Job-Match-Lauf."""
    job_id: UUID
    matches: list[ScoredMatch]
    total_candidates_checked: int
    candidates_after_filter: int
    duration_ms: float
    scoring_weights: dict


@dataclass
class BatchMatchResult:
    """Ergebnis fuer einen Batch-Match-Lauf."""
    jobs_matched: int = 0
    total_matches_created: int = 0
    total_duration_ms: float = 0.0
    errors: list[str] = field(default_factory=list)


# ══════════════════════════════════════════════════════════════════
# DEFAULT SCORING WEIGHTS
# ══════════════════════════════════════════════════════════════════

DEFAULT_WEIGHTS = {
    "skill_overlap": 35.0,
    "seniority_fit": 20.0,
    "embedding_sim": 20.0,
    "career_fit": 10.0,
    "software_match": 10.0,
    "location_bonus": 5.0,  # Bonus fuer gleiche Stadt/Metro (kein Penalty — Entfernung ist Hard Filter)
}

# Entfernung ist jetzt ein HARD FILTER, kein Soft-Score!
# Max. 60km Luftlinie (~1h Fahrweg) — alles darueber wird rausgeworfen.
MAX_DISTANCE_KM = 60


# ══════════════════════════════════════════════════════════════════
# MATCHING ENGINE
# ══════════════════════════════════════════════════════════════════

class MatchingEngineV2:
    """3-Schichten Enterprise-Matching Engine.

    Schicht 1: Hard Filters → SQL-basiert, eliminiert unpassende Kandidaten
    Schicht 2: Structured Scoring → Gewichtete Score-Berechnung
    Schicht 3: Pattern Boost → Gelernte Regeln anwenden

    Alle Gewichte sind lernbar (aus match_v2_scoring_weights).
    """

    TOP_N = 50  # Max. Matches pro Job

    def __init__(self, db: AsyncSession):
        self.db = db
        self._weights: dict[str, float] | None = None
        self._rules: list[dict] | None = None
        self._embedding_service = EmbeddingService()

    async def _load_weights(self) -> dict[str, float]:
        """Laedt aktuelle Scoring-Gewichte aus der DB."""
        if self._weights is not None:
            return self._weights

        result = await self.db.execute(
            select(MatchV2ScoringWeight.component, MatchV2ScoringWeight.weight)
        )
        rows = result.all()

        if rows:
            self._weights = {r[0]: r[1] for r in rows}
        else:
            self._weights = DEFAULT_WEIGHTS.copy()

        return self._weights

    async def _load_rules(self) -> list[dict]:
        """Laedt aktive gelernte Regeln aus der DB."""
        if self._rules is not None:
            return self._rules

        result = await self.db.execute(
            select(MatchV2LearnedRule)
            .where(MatchV2LearnedRule.active == True)
            .order_by(MatchV2LearnedRule.confidence.desc())
        )
        rules = result.scalars().all()
        self._rules = [
            {
                "rule_type": r.rule_type,
                "rule_json": r.rule_json,
                "confidence": r.confidence,
            }
            for r in rules
        ]
        return self._rules

    # ── Schicht 1: Hard Filters ─────────────────────────────

    async def _hard_filter_candidates(
        self,
        job: Job,
        job_level: int,
    ) -> list[MatchCandidate]:
        """Schicht 1: SQL-basierte Hard Filters.

        Eliminiert Kandidaten die definitiv nicht passen:
        - Kein v2-Profil vorhanden
        - Seniority-Level zu weit entfernt
        - Geloescht/Hidden
        - Falsche Hotlist-Kategorie (wenn vorhanden)
        - ZU WEIT WEG: >60km Luftlinie (~1h Fahrweg) → HARD FILTER!

        Returns:
            Liste von MatchCandidate-Objekten (vorgeflitert)
        """
        # Seniority-Range: Job Level ±2, aber max 1-6
        min_level = max(1, job_level - 2)
        max_level = min(6, job_level + 2)

        # Prüfe ob Job Geodaten hat (fuer Distanz-Filter)
        job_has_coords = job.location_coords is not None

        # Basis-Filter
        conditions = [
            Candidate.v2_profile_created_at.isnot(None),
            Candidate.v2_seniority_level.isnot(None),
            Candidate.v2_seniority_level >= min_level,
            Candidate.v2_seniority_level <= max_level,
            Candidate.deleted_at.is_(None),
            Candidate.hidden == False,
        ]

        # HARD FILTER: Entfernung max. 60km (wenn Job Koordinaten hat)
        # Kandidaten OHNE Koordinaten werden trotzdem durchgelassen,
        # aber mit distance_km=None markiert (koennen spaeter manuell geprueft werden)
        if job_has_coords:
            conditions.append(
                or_(
                    # Kandidat innerhalb MAX_DISTANCE_KM km
                    func.ST_DWithin(
                        Candidate.address_coords,
                        job.location_coords,
                        MAX_DISTANCE_KM * 1000,  # ST_DWithin nutzt Meter bei Geography
                    ),
                    # ODER Kandidat hat keine Koordinaten (nicht ausschliessen)
                    Candidate.address_coords.is_(None),
                )
            )

        # Hotlist-Kategorie (wenn Job eine hat)
        if job.hotlist_category:
            conditions.append(
                or_(
                    Candidate.hotlist_category == job.hotlist_category,
                    Candidate.hotlist_category.is_(None),
                )
            )

        # Distanz-Berechnung als Spalte (wenn Job Geodaten hat)
        if job_has_coords:
            distance_expr = func.ST_Distance(
                Candidate.address_coords,
                job.location_coords,
            ).label("distance_m")  # Meter (Geography-Type)
        else:
            distance_expr = text("NULL::float").label("distance_m")

        query = (
            select(
                Candidate.id,
                Candidate.v2_seniority_level,
                Candidate.v2_career_trajectory,
                Candidate.v2_years_experience,
                Candidate.v2_structured_skills,
                Candidate.v2_current_role_summary,
                Candidate.v2_embedding_current,
                Candidate.v2_embedding_full,
                Candidate.city,
                Candidate.hotlist_category,
                distance_expr,
            )
            .where(and_(*conditions))
            .order_by(
                # Priorisiere Kandidaten mit Embeddings
                Candidate.v2_embedding_current.isnot(None).desc(),
                Candidate.v2_profile_created_at.desc(),
            )
            .limit(2000)  # Safety-Limit
        )

        result = await self.db.execute(query)
        rows = result.all()

        candidates = []
        for row in rows:
            distance_m = row[10]  # Meter oder None
            distance_km = round(distance_m / 1000, 1) if distance_m is not None else None

            candidates.append(MatchCandidate(
                id=row[0],
                seniority_level=row[1] or 2,
                career_trajectory=row[2] or "lateral",
                years_experience=row[3] or 0,
                structured_skills=row[4] or [],
                current_role_summary=row[5] or "",
                embedding_current=row[6],
                embedding_full=row[7],
                city=row[8],
                hotlist_category=row[9],
                distance_km=distance_km,
            ))

        logger.info(
            f"Hard Filter: {len(candidates)} Kandidaten fuer Job Level {job_level} "
            f"(Range {min_level}-{max_level}, Category: {job.hotlist_category}, "
            f"Distance Hard Filter: {'<=' + str(MAX_DISTANCE_KM) + 'km' if job_has_coords else 'keine Geodaten'})"
        )
        return candidates

    # ── Schicht 2: Structured Scoring ───────────────────────

    def _score_skill_overlap(
        self,
        candidate_skills: list[dict],
        job_skills: list[dict],
    ) -> float:
        """Berechnet Skill-Overlap Score (0.0 - 1.0).

        Essential Skills wiegen mehr als Preferred.
        Aktuelle Skills wiegen mehr als veraltete.
        """
        if not job_skills or not candidate_skills:
            return 0.0

        # Normalisierte Skill-Namen
        cand_skill_map: dict[str, dict] = {}
        for s in candidate_skills:
            name = s.get("skill", "").lower().strip()
            if name:
                cand_skill_map[name] = s

        essential_total = 0
        essential_matched = 0.0
        preferred_total = 0
        preferred_matched = 0.0

        for js in job_skills:
            skill_name = js.get("skill", "").lower().strip()
            importance = js.get("importance", "preferred")

            if importance == "essential":
                essential_total += 1
            else:
                preferred_total += 1

            # Suche nach Match (exakt oder Teilstring)
            match_score = 0.0
            matched_skill = cand_skill_map.get(skill_name)

            if not matched_skill:
                # Fuzzy: Pruefe ob ein Kandidaten-Skill den Job-Skill enthaelt oder umgekehrt
                for cname, cskill in cand_skill_map.items():
                    if skill_name in cname or cname in skill_name:
                        matched_skill = cskill
                        match_score = 0.8  # Partial match penalty
                        break

            if matched_skill:
                if match_score == 0.0:
                    match_score = 1.0  # Exact match

                # Recency-Abschlag
                recency = matched_skill.get("recency", "aktuell")
                if recency == "aktuell":
                    pass  # Kein Abschlag
                elif recency == "kuerzlich":
                    match_score *= 0.7
                elif recency == "veraltet":
                    match_score *= 0.3

                # Proficiency-Bonus
                prof = matched_skill.get("proficiency", "grundlagen")
                if prof == "experte":
                    match_score = min(1.0, match_score * 1.1)

                if importance == "essential":
                    essential_matched += match_score
                else:
                    preferred_matched += match_score

        # Gewichtete Berechnung: Essential zaehlt 70%, Preferred 30%
        essential_score = (essential_matched / essential_total) if essential_total > 0 else 0.5
        preferred_score = (preferred_matched / preferred_total) if preferred_total > 0 else 0.5

        return essential_score * 0.7 + preferred_score * 0.3

    def _score_seniority_fit(self, candidate_level: int, job_level: int) -> float:
        """Berechnet Seniority-Fit Score (0.0 - 1.0).

        Perfekter Match = 1.0
        ±1 Level = 0.7 (leichte Unter-/Ueberqualifikation)
        ±2 Level = 0.3 (groessere Abweichung)
        >2 Level = 0.0 (wird von Hard Filter schon gefiltert)
        """
        gap = abs(candidate_level - job_level)
        if gap == 0:
            return 1.0
        elif gap == 1:
            # Leicht ueberqualifiziert ist besser als unterqualifiziert
            if candidate_level > job_level:
                return 0.65  # Ueberqualifiziert
            else:
                return 0.75  # Leicht unterqualifiziert (kann reinwachsen)
        elif gap == 2:
            return 0.3
        else:
            return 0.0

    def _score_career_fit(
        self,
        candidate_trajectory: str,
        candidate_level: int,
        job_level: int,
    ) -> float:
        """Berechnet Karriere-Fit Score (0.0 - 1.0).

        Aufsteigender Kandidat + passender naechster Schritt = ideal.
        Lateraler Kandidat = solide.
        Absteigender + Level-Gap = problematisch.
        """
        gap = job_level - candidate_level  # Positiv = Job ist hoeher

        if candidate_trajectory == "aufsteigend":
            if gap == 1:
                return 1.0  # Perfekter naechster Karriereschritt
            elif gap == 0:
                return 0.8  # Lateraler Wechsel fuer aufsteigenden Kandidaten
            elif gap == -1:
                return 0.4  # Rueckschritt fuer aufsteigenden Kandidaten
            elif gap >= 2:
                return 0.3  # Zu grosser Sprung
            else:
                return 0.2  # Deutlicher Rueckschritt
        elif candidate_trajectory == "lateral":
            if gap == 0:
                return 0.9  # Perfekt fuer laterale Karriere
            elif abs(gap) == 1:
                return 0.6
            else:
                return 0.3
        elif candidate_trajectory == "absteigend":
            if gap <= 0:
                return 0.5  # Passt zum Trend
            else:
                return 0.2  # Aufstieg bei absteigender Karriere = unwahrscheinlich
        elif candidate_trajectory == "einstieg":
            if job_level <= 2:
                return 0.8  # Einstieg in Junior/Sachbearbeiter-Rolle
            else:
                return 0.2  # Einsteiger fuer Senior-Rolle = unrealistisch

        return 0.5  # Default

    def _score_software_match(
        self,
        candidate_skills: list[dict],
        job_skills: list[dict],
    ) -> float:
        """Berechnet Software-Match Score (0.0 - 1.0).

        DATEV + DATEV = 1.0
        SAP + SAP = 1.0
        DATEV + SAP = 0.3 (6-12 Monate Umstieg)
        Keine Software-Anforderung = 0.5 (neutral)
        """
        datev_keywords = {"datev", "datev unternehmen online", "datev kanzlei"}
        sap_keywords = {"sap", "sap fi", "sap co", "sap s/4hana", "sap s4hana"}

        def detect_ecosystem(skills: list[dict]) -> set[str]:
            ecosystems = set()
            for s in skills:
                name = s.get("skill", "").lower()
                if any(kw in name for kw in datev_keywords):
                    ecosystems.add("datev")
                if any(kw in name for kw in sap_keywords):
                    ecosystems.add("sap")
            return ecosystems

        job_eco = detect_ecosystem(job_skills)
        cand_eco = detect_ecosystem(candidate_skills)

        if not job_eco:
            return 0.5  # Job hat keine Software-Anforderung

        if not cand_eco:
            return 0.3  # Kandidat hat keine bekannte Software

        # Mindestens ein Ecosystem stimmt ueberein
        overlap = job_eco & cand_eco
        if overlap:
            return 1.0

        # Cross-Ecosystem (z.B. DATEV-Kandidat fuer SAP-Job)
        if job_eco and cand_eco and not overlap:
            return 0.3  # 6-12 Monate Umstieg

        return 0.5

    def _score_city_metro(
        self,
        candidate_city: str | None,
        job_city: str | None,
    ) -> float:
        """Berechnet Stadt/Metro-Match Score (0.0 - 1.0).

        Gleiche Stadt = 1.0
        Metro-Area = 0.5
        Andere = 0.0
        """
        if not candidate_city or not job_city:
            return 0.3  # Keine Daten → neutral

        c_city = candidate_city.lower().strip()
        j_city = job_city.lower().strip()

        if c_city == j_city:
            return 1.0

        # Metro-Areas (haeufige Agglomerationen in Deutschland)
        metro_areas = {
            "muenchen": {"muenchen", "münchen", "munich", "garching", "unterfoeehring",
                         "unterfoehring", "ismaning", "ottobrunn", "haar", "gruenwald",
                         "grünwald", "pullach", "taufkirchen", "unterschleissheim",
                         "unterschleißheim", "oberschleissheim", "oberschleißheim",
                         "neubiberg", "aschheim", "kirchheim", "heimstetten",
                         "dachau", "freising", "erding", "starnberg", "germering",
                         "fuerstenfeldbruck", "fürstenfeldbruck", "pasing"},
            "frankfurt": {"frankfurt", "frankfurt am main", "offenbach", "eschborn",
                          "bad homburg", "oberursel", "kronberg", "friedberg",
                          "bad vilbel", "dreieich", "neu-isenburg", "langen",
                          "darmstadt", "wiesbaden", "mainz", "hanau"},
            "hamburg": {"hamburg", "norderstedt", "ahrensburg", "pinneberg",
                        "wedel", "schenefeld", "quickborn", "elmshorn"},
            "berlin": {"berlin", "potsdam", "berlin-mitte", "charlottenburg",
                       "schoeneberg", "schöneberg"},
            "koeln": {"koeln", "köln", "cologne", "leverkusen", "bonn",
                      "bergisch gladbach", "troisdorf", "bruehl", "brühl"},
            "duesseldorf": {"duesseldorf", "düsseldorf", "neuss", "meerbusch",
                            "ratingen", "erkrath", "hilden", "dormagen"},
            "stuttgart": {"stuttgart", "esslingen", "ludwigsburg", "sindelfingen",
                          "boeblingen", "böblingen", "leonberg", "waiblingen",
                          "fellbach", "filderstadt"},
            "nuernberg": {"nuernberg", "nürnberg", "fuerth", "fürth",
                          "erlangen", "schwabach"},
        }

        # Finde Metro fuer beide Staedte
        c_metro = None
        j_metro = None
        for metro_name, cities in metro_areas.items():
            if c_city in cities or any(c_city.startswith(c) for c in cities):
                c_metro = metro_name
            if j_city in cities or any(j_city.startswith(c) for c in cities):
                j_metro = metro_name

        if c_metro and j_metro and c_metro == j_metro:
            return 0.5  # Gleiche Metro-Area

        return 0.0

    def _score_embedding_similarity(
        self,
        candidate_embedding: list[float] | None,
        job_embedding: list[float] | None,
    ) -> float:
        """Berechnet Embedding-Similarity (0.0 - 1.0).

        Nutzt Cosine-Similarity und normiert auf 0-1 Range.
        Typische gute Matches: 0.6-0.8, schlechte: <0.4
        """
        if not candidate_embedding or not job_embedding:
            return 0.3  # Kein Embedding → neutraler Default

        sim = EmbeddingService.cosine_similarity(candidate_embedding, job_embedding)

        # Normierung: cosine_sim geht von -1 bis 1, wir brauchen 0-1
        # Typische Werte liegen bei 0.3-0.9 fuer sinnvolle Texte
        # Lineares Mapping: 0.3→0.0, 0.9→1.0
        normalized = max(0.0, min(1.0, (sim - 0.3) / 0.6))
        return normalized

    def _score_location_bonus(
        self,
        candidate_city: str | None,
        job_city: str | None,
        distance_km: float | None,
    ) -> float:
        """Berechnet Location-Bonus (0.0 - 1.0).

        Entfernung ist bereits ein HARD FILTER (>60km = raus).
        Dieser Score gibt NUR einen Bonus fuer besonders nahe Kandidaten:

        - Gleiche Stadt oder ≤15km = 1.0 (ideal)
        - Gleiche Metro-Area oder ≤30km = 0.7 (gut)
        - ≤60km = 0.4 (akzeptabel)
        - Keine Geodaten = 0.3 (neutral, kein Penalty)
        """
        # Wenn echte Distanz bekannt → nutze sie
        if distance_km is not None:
            if distance_km <= 15:
                return 1.0
            elif distance_km <= 30:
                return 0.7
            elif distance_km <= 60:
                return 0.4
            else:
                return 0.0  # Sollte eigentlich nicht vorkommen (Hard Filter)

        # Fallback: Stadt-Vergleich (wenn keine Geodaten)
        return self._score_city_metro(candidate_city, job_city)

    async def _score_candidates(
        self,
        job: Job,
        candidates: list[MatchCandidate],
        weights: dict[str, float],
    ) -> list[ScoredMatch]:
        """Schicht 2: Berechnet gewichteten Score fuer alle Kandidaten.

        ENTFERNUNG IST KEIN SOFT-SCORE MEHR!
        Entfernung >60km wird schon in Schicht 1 (Hard Filter) entfernt.
        Hier gibt es nur noch einen Bonus fuer besonders nahe Kandidaten.

        Args:
            job: Der Job gegen den gematcht wird
            candidates: Vorgefliterte Kandidaten
            weights: Scoring-Gewichte

        Returns:
            Liste von ScoredMatch, sortiert nach Score
        """
        job_level = job.v2_seniority_level or 2
        job_skills = job.v2_required_skills or []
        job_embedding = job.v2_embedding
        job_city = job.work_location_city or job.city

        # Gewichte normalisieren (Summe = 100)
        total_weight = sum(weights.values())
        if total_weight == 0:
            total_weight = 100

        scored = []
        for cand in candidates:
            # Einzelne Scores berechnen (alle 0.0 - 1.0)
            skill_score = self._score_skill_overlap(
                cand.structured_skills, job_skills
            )
            seniority_score = self._score_seniority_fit(
                cand.seniority_level, job_level
            )
            embedding_score = self._score_embedding_similarity(
                cand.embedding_current, job_embedding
            )
            career_score = self._score_career_fit(
                cand.career_trajectory, cand.seniority_level, job_level
            )
            software_score = self._score_software_match(
                cand.structured_skills, job_skills
            )
            location_score = self._score_location_bonus(
                cand.city, job_city, cand.distance_km
            )

            # Gewichtete Summe (0-100)
            total = (
                skill_score * weights.get("skill_overlap", 35) +
                seniority_score * weights.get("seniority_fit", 20) +
                embedding_score * weights.get("embedding_sim", 20) +
                career_score * weights.get("career_fit", 10) +
                software_score * weights.get("software_match", 10) +
                location_score * weights.get("location_bonus", 5)
            ) / total_weight * 100

            breakdown = {
                "skill_overlap": round(skill_score, 3),
                "seniority_fit": round(seniority_score, 3),
                "embedding_sim": round(embedding_score, 3),
                "career_fit": round(career_score, 3),
                "software_match": round(software_score, 3),
                "location_bonus": round(location_score, 3),
                "distance_km": cand.distance_km,
            }

            scored.append(ScoredMatch(
                candidate_id=cand.id,
                total_score=round(total, 2),
                breakdown=breakdown,
            ))

        # Sortieren nach Score (hoechster zuerst)
        scored.sort(key=lambda x: x.total_score, reverse=True)

        # Rang zuweisen
        for i, m in enumerate(scored):
            m.rank = i + 1

        return scored[:self.TOP_N]

    # ── Schicht 3: Pattern Boost ────────────────────────────

    async def _apply_learned_rules(
        self,
        scored_matches: list[ScoredMatch],
        job: Job,
        candidates_map: dict[UUID, MatchCandidate],
    ) -> list[ScoredMatch]:
        """Schicht 3: Wendet gelernte Regeln an.

        Boosted/Penalized Scores basierend auf gelernten Mustern.
        Beispiel: {HGB + SAP_FI + Level 4} → +10 Punkte fuer Bilanz-Jobs.
        """
        rules = await self._load_rules()
        if not rules:
            return scored_matches

        for match in scored_matches:
            cand = candidates_map.get(match.candidate_id)
            if not cand:
                continue

            for rule in rules:
                try:
                    rule_data = rule.get("rule_json", {})
                    rule_type = rule.get("rule_type", "")
                    confidence = rule.get("confidence", 0.5)

                    if rule_type == "association":
                        # Pruefe ob Bedingungen erfuellt sind
                        conditions = rule_data.get("conditions", {})
                        boost = rule_data.get("boost", 0)

                        if self._check_rule_conditions(conditions, cand, job):
                            match.total_score += boost * confidence
                            match.total_score = min(100, max(0, match.total_score))

                    elif rule_type == "weight_override":
                        # Spezielle Gewichtung fuer bestimmte Konstellationen
                        pass  # Wird in Sprint 4 implementiert

                except Exception as e:
                    logger.debug(f"Regel-Anwendung fehlgeschlagen: {e}")

        # Neu sortieren nach angepassten Scores
        scored_matches.sort(key=lambda x: x.total_score, reverse=True)
        for i, m in enumerate(scored_matches):
            m.rank = i + 1

        return scored_matches

    def _check_rule_conditions(
        self,
        conditions: dict,
        cand: MatchCandidate,
        job: Job,
    ) -> bool:
        """Prueft ob die Bedingungen einer Regel erfuellt sind."""
        # Skill-Bedingungen
        required_skills = conditions.get("has_skills", [])
        if required_skills:
            cand_skill_names = {
                s.get("skill", "").lower() for s in cand.structured_skills
            }
            if not all(sk.lower() in cand_skill_names for sk in required_skills):
                return False

        # Level-Bedingungen
        min_level = conditions.get("min_level")
        if min_level and cand.seniority_level < min_level:
            return False

        max_level = conditions.get("max_level")
        if max_level and cand.seniority_level > max_level:
            return False

        # Experience-Bedingungen
        min_years = conditions.get("min_years")
        if min_years and cand.years_experience < min_years:
            return False

        return True

    # ── Hauptmethoden ───────────────────────────────────────

    async def match_job(
        self,
        job_id: UUID,
        save_to_db: bool = True,
    ) -> MatchResult:
        """Matcht einen Job gegen alle passenden Kandidaten.

        3-Schichten-Pipeline:
        1. Hard Filters (SQL)
        2. Structured Scoring (Python)
        3. Pattern Boost (gelernte Regeln)

        Args:
            job_id: UUID des Jobs
            save_to_db: Ob Matches in DB gespeichert werden sollen

        Returns:
            MatchResult mit Top-50 Matches
        """
        import time
        start = time.perf_counter()

        # Job laden
        job = await self.db.get(Job, job_id)
        if not job:
            raise ValueError(f"Job {job_id} nicht gefunden")

        if not job.v2_profile_created_at:
            raise ValueError(f"Job {job_id} hat kein v2-Profil. Zuerst profilieren!")

        job_level = job.v2_seniority_level or 2
        weights = await self._load_weights()

        # ── Schicht 1: Hard Filters ──
        candidates = await self._hard_filter_candidates(job, job_level)
        total_checked = 2000  # Safety-Limit aus Query

        if not candidates:
            return MatchResult(
                job_id=job_id,
                matches=[],
                total_candidates_checked=total_checked,
                candidates_after_filter=0,
                duration_ms=round((time.perf_counter() - start) * 1000, 1),
                scoring_weights=weights,
            )

        # ── Schicht 2: Structured Scoring ──
        scored = await self._score_candidates(job, candidates, weights)

        # Build Lookup fuer Schicht 3
        cand_map = {c.id: c for c in candidates}

        # ── Schicht 3: Pattern Boost ──
        scored = await self._apply_learned_rules(scored, job, cand_map)

        # ── In DB speichern ──
        if save_to_db and scored:
            await self._save_matches(job_id, scored)

        duration = round((time.perf_counter() - start) * 1000, 1)

        logger.info(
            f"Match fuer Job '{job.position}' bei '{job.company_name}': "
            f"{len(scored)} Matches (Top: {scored[0].total_score if scored else 0}), "
            f"{len(candidates)} nach Filter, {duration}ms"
        )

        return MatchResult(
            job_id=job_id,
            matches=scored,
            total_candidates_checked=total_checked,
            candidates_after_filter=len(candidates),
            duration_ms=duration,
            scoring_weights=weights,
        )

    async def _save_matches(self, job_id: UUID, scored: list[ScoredMatch]):
        """Speichert die Top-Matches in der DB."""
        now = datetime.now(timezone.utc)

        for sm in scored:
            # Prüfe ob Match bereits existiert
            existing = await self.db.execute(
                select(Match.id).where(
                    Match.job_id == job_id,
                    Match.candidate_id == sm.candidate_id,
                )
            )
            match_row = existing.scalar_one_or_none()

            if match_row:
                # Update bestehendes Match
                await self.db.execute(
                    select(Match).where(Match.id == match_row)
                )
                match_obj = await self.db.get(Match, match_row)
                if match_obj:
                    match_obj.v2_score = sm.total_score
                    match_obj.v2_score_breakdown = sm.breakdown
                    match_obj.v2_matched_at = now
            else:
                # Neues Match erstellen
                match = Match(
                    job_id=job_id,
                    candidate_id=sm.candidate_id,
                    v2_score=sm.total_score,
                    v2_score_breakdown=sm.breakdown,
                    v2_matched_at=now,
                    status=MatchStatus.NEW,
                )
                self.db.add(match)

        await self.db.flush()

    async def match_batch(
        self,
        job_ids: list[UUID] | None = None,
        unmatched_only: bool = True,
        max_jobs: int = 0,
        progress_callback=None,
    ) -> BatchMatchResult:
        """Matcht mehrere Jobs in einem Batch.

        Args:
            job_ids: Spezifische Jobs (None = alle ungematchten)
            unmatched_only: Nur Jobs ohne v2-Matches
            max_jobs: Maximum (0 = alle)
            progress_callback: Optional callback(processed, total)

        Returns:
            BatchMatchResult mit Statistiken
        """
        result = BatchMatchResult()

        if job_ids:
            ids = job_ids
        else:
            # FINANCE-Jobs ohne v2-Matches laden
            query = (
                select(Job.id)
                .where(
                    Job.v2_profile_created_at.isnot(None),
                    Job.deleted_at.is_(None),
                    Job.hotlist_category == "FINANCE",
                )
            )

            if unmatched_only:
                # Jobs die noch kein v2-Match haben
                subq = (
                    select(Match.job_id)
                    .where(Match.v2_matched_at.isnot(None))
                    .distinct()
                )
                query = query.where(Job.id.notin_(subq))

            query = query.order_by(Job.created_at.desc())
            if max_jobs > 0:
                query = query.limit(max_jobs)

            ids_result = await self.db.execute(query)
            ids = [row[0] for row in ids_result.all()]

        total = len(ids)
        logger.info(f"Batch-Matching: {total} Jobs zu matchen")

        for i, job_id in enumerate(ids):
            try:
                match_result = await self.match_job(job_id, save_to_db=True)
                result.jobs_matched += 1
                result.total_matches_created += len(match_result.matches)
                result.total_duration_ms += match_result.duration_ms

            except Exception as e:
                if len(result.errors) < 20:
                    result.errors.append(f"Job {job_id}: {str(e)[:100]}")

            # Progress + Commit
            if (i + 1) % 10 == 0:
                await self.db.commit()
                if progress_callback:
                    progress_callback(i + 1, total)
                logger.info(
                    f"Batch-Matching: {i + 1}/{total} Jobs, "
                    f"{result.total_matches_created} Matches"
                )

        # Final commit
        await self.db.commit()

        logger.info(
            f"Batch-Matching abgeschlossen: {result.jobs_matched} Jobs, "
            f"{result.total_matches_created} Matches, "
            f"{result.total_duration_ms:.0f}ms gesamt"
        )
        return result


# ══════════════════════════════════════════════════════════════════
# EMBEDDING GENERATION SERVICE
# ══════════════════════════════════════════════════════════════════

class EmbeddingGenerationService:
    """Generiert Embeddings fuer alle profilierten Kandidaten/Jobs.

    Wird nach dem Profile-Backfill ausgefuehrt.
    """

    def __init__(self, db: AsyncSession):
        self.db = db
        self.embedding_service = EmbeddingService()

    async def close(self):
        await self.embedding_service.close()

    async def generate_candidate_embeddings(
        self,
        batch_size: int = 50,
        max_total: int = 0,
        progress_callback=None,
    ) -> dict:
        """Generiert Embeddings fuer FINANCE-Kandidaten mit v2-Profil aber ohne Embedding."""
        # FINANCE-Kandidaten ohne Embedding laden
        query = (
            select(Candidate.id, Candidate.v2_current_role_summary, Candidate.v2_structured_skills)
            .where(
                Candidate.v2_profile_created_at.isnot(None),
                Candidate.v2_embedding_current.is_(None),
                Candidate.deleted_at.is_(None),
                Candidate.hotlist_category == "FINANCE",
            )
            .order_by(Candidate.created_at.asc())
        )
        if max_total > 0:
            query = query.limit(max_total)

        result = await self.db.execute(query)
        rows = result.all()

        total = len(rows)
        generated = 0
        failed = 0

        logger.info(f"Embedding-Generierung Kandidaten: {total} zu verarbeiten")

        for i in range(0, total, batch_size):
            batch = rows[i:i + batch_size]

            # Texte fuer current embedding vorbereiten
            texts = []
            for row in batch:
                summary = row[1] or ""
                skills = row[2] or []
                skill_str = ", ".join(s.get("skill", "") for s in skills[:10])
                texts.append(f"{summary} Skills: {skill_str}")

            # Batch-Embedding
            embeddings = await self.embedding_service.embed_batch(texts)

            for j, (row, emb) in enumerate(zip(batch, embeddings)):
                if emb:
                    cand = await self.db.get(Candidate, row[0])
                    if cand:
                        cand.v2_embedding_current = emb
                        generated += 1
                else:
                    failed += 1

            await self.db.commit()
            if progress_callback:
                progress_callback(min(i + batch_size, total), total)
            logger.info(
                f"Embedding Kandidaten: {min(i + batch_size, total)}/{total} "
                f"({generated} OK, {failed} Fehler)"
            )

        return {"total": total, "generated": generated, "failed": failed}

    async def generate_job_embeddings(
        self,
        batch_size: int = 50,
        max_total: int = 0,
        progress_callback=None,
    ) -> dict:
        """Generiert Embeddings fuer FINANCE-Jobs mit v2-Profil aber ohne Embedding."""
        query = (
            select(Job.id, Job.v2_role_summary, Job.v2_required_skills, Job.position)
            .where(
                Job.v2_profile_created_at.isnot(None),
                Job.v2_embedding.is_(None),
                Job.deleted_at.is_(None),
                Job.hotlist_category == "FINANCE",
            )
            .order_by(Job.created_at.asc())
        )
        if max_total > 0:
            query = query.limit(max_total)

        result = await self.db.execute(query)
        rows = result.all()

        total = len(rows)
        generated = 0
        failed = 0

        logger.info(f"Embedding-Generierung Jobs: {total} zu verarbeiten")

        for i in range(0, total, batch_size):
            batch = rows[i:i + batch_size]

            texts = []
            for row in batch:
                summary = row[1] or ""
                skills = row[2] or []
                position = row[3] or ""
                skill_str = ", ".join(s.get("skill", "") for s in skills[:10])
                texts.append(f"{position}. {summary} Skills: {skill_str}")

            embeddings = await self.embedding_service.embed_batch(texts)

            for j, (row, emb) in enumerate(zip(batch, embeddings)):
                if emb:
                    job = await self.db.get(Job, row[0])
                    if job:
                        job.v2_embedding = emb
                        generated += 1
                else:
                    failed += 1

            await self.db.commit()
            if progress_callback:
                progress_callback(min(i + batch_size, total), total)
            logger.info(
                f"Embedding Jobs: {min(i + batch_size, total)}/{total} "
                f"({generated} OK, {failed} Fehler)"
            )

        return {"total": total, "generated": generated, "failed": failed}
