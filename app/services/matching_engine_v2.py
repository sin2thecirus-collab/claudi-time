"""Matching Engine v2 — 3-Schichten Enterprise-Matching.

Schicht 1: Hard Filters (SQL, <5ms) — eliminiert 85-90% der Kandidaten
Schicht 2: Structured Scoring (Python, <50ms/200 Kandidaten) — gewichtete Score-Berechnung
Schicht 3: Pattern Boost/Penalty — gelernte Regeln anwenden

Kosten pro Match: $0.00 (alles lokal/vorberechnet)
"""

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence
from uuid import UUID

from sqlalchemy import select, func, and_, or_, text, literal_column
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
    # v2.5 Felder
    certifications: list[str] = field(default_factory=list)  # z.B. ["Bilanzbuchhalter"]
    industries: list[str] = field(default_factory=list)  # z.B. ["Maschinenbau"]
    erp: list[str] = field(default_factory=list)  # z.B. ["SAP", "DATEV"]
    job_titles: list[str] = field(default_factory=list)  # hotlist_job_titles + manual_job_titles


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
    "skill_overlap": 27.0,
    "seniority_fit": 20.0,
    "job_title_fit": 18.0,  # NEU: Titel-Match (v2.5)
    "embedding_sim": 15.0,
    "industry_fit": 8.0,  # NEU: Branchenerfahrung (v2.5)
    "career_fit": 7.0,
    "software_match": 5.0,
    # Summe: 27 + 20 + 18 + 15 + 8 + 7 + 5 = 100
    # Location ist KEIN Score mehr — nur Hard Filter (30km)
}

# Entfernung ist ein HARD FILTER, kein Soft-Score!
# Max. 30km Luftlinie — realistisches Pendel-Maximum.
# Remote-Jobs ueberspringen diesen Filter (siehe _hard_filter_candidates).
MAX_DISTANCE_KM = 30


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

    TOP_N = 200  # Max. Matches pro Job (erhoeht von 50)

    # Skill-Weights Cache (Klassen-Level, einmal laden)
    _skill_weights: dict | None = None
    _skill_to_category: dict[str, tuple[str, int]] | None = None  # skill_lower → (kategorie, weight)

    @classmethod
    def _load_skill_weights(cls) -> dict:
        """Laedt skill_weights.json (cached auf Klassen-Level)."""
        if cls._skill_weights is not None:
            return cls._skill_weights

        config_path = Path(__file__).parent.parent / "config" / "skill_weights.json"
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cls._skill_weights = json.load(f)
            # Baue Reverse-Lookup: skill_name_lower → (kategorie, weight)
            cls._skill_to_category = {}
            for role, categories in cls._skill_weights.items():
                for cat_name, cat_data in categories.items():
                    weight = cat_data.get("weight", 5)
                    for skill in cat_data.get("skills", []):
                        # Speichere pro Rolle: "bilanzbuchhalter::skill_lower" → (cat, weight)
                        key = f"{role}::{skill.lower().strip()}"
                        cls._skill_to_category[key] = (cat_name, weight)
            logger.info(f"Skill-Weights geladen: {len(cls._skill_weights)} Rollen, {len(cls._skill_to_category)} Skill-Mappings")
        except FileNotFoundError:
            logger.warning(f"skill_weights.json nicht gefunden: {config_path}")
            cls._skill_weights = {}
            cls._skill_to_category = {}
        except json.JSONDecodeError as e:
            logger.error(f"skill_weights.json Parse-Fehler: {e}")
            cls._skill_weights = {}
            cls._skill_to_category = {}
        return cls._skill_weights

    @classmethod
    def _get_skill_weight(cls, role: str, skill_name: str) -> int | None:
        """Gibt das Kategorie-Gewicht fuer einen Skill zurueck (oder None wenn nicht gefunden)."""
        if cls._skill_to_category is None:
            cls._load_skill_weights()
        if not cls._skill_to_category:
            return None
        # Suche: rolle::skill_name_lower
        key = f"{role}::{skill_name.lower().strip()}"
        result = cls._skill_to_category.get(key)
        if result:
            return result[1]
        # Auch normalisierten Skill-Namen versuchen
        normalized = cls._normalize_skill(skill_name)
        key_norm = f"{role}::{normalized}"
        result = cls._skill_to_category.get(key_norm)
        return result[1] if result else None

    @classmethod
    def _detect_job_role(cls, job_title: str | None, position: str | None) -> str | None:
        """Erkennt die Job-Rolle aus Titel/Position fuer Skill-Weight-Lookup."""
        if cls._skill_weights is None:
            cls._load_skill_weights()

        search_text = ""
        if job_title:
            search_text += job_title.lower()
        if position:
            search_text += " " + position.lower()

        if not search_text.strip():
            return None

        # Prioritaets-Reihenfolge: spezifischer zuerst
        if "bilanzbuchhalter" in search_text:
            return "bilanzbuchhalter"
        if "steuerfachangestellte" in search_text or "steuerfachangestellter" in search_text:
            return "steuerfachangestellte"
        if "finanzbuchhalter" in search_text:
            return "finanzbuchhalter"
        if "lohnbuchhalter" in search_text:
            return "lohnbuchhalter"

        return None

    def __init__(self, db: AsyncSession):
        self.db = db
        self._weights: dict[str, float] | None = None
        self._rules: list[dict] | None = None
        self._embedding_service = EmbeddingService()

    async def _load_weights(self, job_category: str | None = None) -> dict[str, float]:
        """Laedt aktuelle Scoring-Gewichte aus der DB.

        Pro-Kategorie-Lernen: Wenn eine job_category angegeben wird,
        werden zuerst kategorie-spezifische Gewichte gesucht.
        Falls keine vorhanden → globale Gewichte (job_category IS NULL).
        Falls auch keine → DEFAULT_WEIGHTS.
        """
        # Cache nur fuer globale Gewichte (pro-Job Gewichte werden frisch geladen)
        if job_category is None and self._weights is not None:
            return self._weights

        if job_category:
            # Versuche kategorie-spezifische Gewichte
            result = await self.db.execute(
                select(MatchV2ScoringWeight.component, MatchV2ScoringWeight.weight)
                .where(MatchV2ScoringWeight.job_category == job_category)
            )
            rows = result.all()
            if rows:
                return {r[0]: r[1] for r in rows}

        # Globale Gewichte (job_category IS NULL)
        result = await self.db.execute(
            select(MatchV2ScoringWeight.component, MatchV2ScoringWeight.weight)
            .where(MatchV2ScoringWeight.job_category.is_(None))
        )
        rows = result.all()

        if rows:
            weights = {r[0]: r[1] for r in rows}
        else:
            weights = DEFAULT_WEIGHTS.copy()

        # Nur globale Gewichte cachen
        if job_category is None:
            self._weights = weights

        return weights

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
        - ZU WEIT WEG: >60km Luftlinie → HARD FILTER!
          (Remote-Jobs ueberspringen den Entfernungs-Filter)

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

        # HARD FILTER: Entfernung max. 30km (wenn Job Koordinaten hat)
        # Remote-Jobs ueberspringen den Entfernungs-Filter komplett!
        # Kandidaten OHNE Koordinaten werden AUSGESCHLOSSEN (nicht mehr durchgelassen).
        # Grund: Sonst werden z.B. Kandidaten aus Bayern mit Jobs in Hamburg gematcht.
        job_is_remote = getattr(job, "work_arrangement", None) == "remote"

        if job_has_coords and not job_is_remote:
            conditions.append(
                # Kandidat MUSS Koordinaten haben UND innerhalb MAX_DISTANCE_KM sein
                func.ST_DWithin(
                    Candidate.address_coords,
                    job.location_coords,
                    MAX_DISTANCE_KM * 1000,  # ST_DWithin nutzt Meter bei Geography
                ),
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
            distance_expr = literal_column("NULL::float").label("distance_m")

        query = (
            select(
                Candidate.id,                    # 0
                Candidate.v2_seniority_level,     # 1
                Candidate.v2_career_trajectory,   # 2
                Candidate.v2_years_experience,    # 3
                Candidate.v2_structured_skills,   # 4
                Candidate.v2_current_role_summary, # 5
                Candidate.v2_embedding_current,   # 6
                Candidate.v2_embedding_full,      # 7
                Candidate.city,                   # 8
                Candidate.hotlist_category,        # 9
                distance_expr,                    # 10
                # v2.5 Felder
                Candidate.v2_certifications,      # 11
                Candidate.v2_industries,          # 12
                Candidate.erp,                    # 13
                Candidate.hotlist_job_titles,      # 14
                Candidate.manual_job_titles,       # 15
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

            # Job-Titel zusammenmergen (hotlist + manual)
            all_titles = list(row[14] or []) + list(row[15] or [])

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
                certifications=row[11] or [],
                industries=row[12] or [],
                erp=row[13] or [],
                job_titles=all_titles,
            ))

        logger.info(
            f"Hard Filter: {len(candidates)} Kandidaten fuer Job Level {job_level} "
            f"(Range {min_level}-{max_level}, Category: {job.hotlist_category}, "
            f"Distance Hard Filter: {'REMOTE — kein Limit' if job_is_remote else '<=' + str(MAX_DISTANCE_KM) + 'km' if job_has_coords else 'keine Geodaten'})"
        )
        return candidates

    # ── Schicht 2: Structured Scoring ───────────────────────

    # Synonym-Tabelle: Varianten desselben Skills → gleicher normalisierter Name
    # Nur ECHTE Synonyme, keine "aehnlichen" Skills!
    SKILL_SYNONYMS: dict[str, str] = {
        # ── Qualifikationen / Zertifizierungen ──
        "bilanzbuchhalter ihk": "bilanzbuchhalter ihk",
        "ihk bilanzbuchhalter": "bilanzbuchhalter ihk",
        "geprüfter bilanzbuchhalter": "bilanzbuchhalter ihk",
        "gepruefter bilanzbuchhalter": "bilanzbuchhalter ihk",
        "bilanzbuchhalter (ihk)": "bilanzbuchhalter ihk",
        "bilanzbuchhalter": "bilanzbuchhalter ihk",
        "steuerfachangestellte": "steuerfachangestellte",
        "steuerfachangestellter": "steuerfachangestellte",
        "steuerfachangestellte/r": "steuerfachangestellte",
        "geprüfter steuerfachangestellter": "steuerfachangestellte",
        "gepruefter steuerfachangestellter": "steuerfachangestellte",
        "steuerfachwirt": "steuerfachwirt",
        "steuerfachwirt/in": "steuerfachwirt",
        "geprüfter finanzbuchhalter": "finanzbuchhalter ihk",
        "gepruefter finanzbuchhalter": "finanzbuchhalter ihk",
        "finanzbuchhalter ihk": "finanzbuchhalter ihk",
        "finanzbuchhalter (ihk)": "finanzbuchhalter ihk",
        # ── Finanzbuchhaltung-Varianten ──
        "finanzbuchhaltung": "finanzbuchhaltung",
        "fibu": "finanzbuchhaltung",
        "finanz- und rechnungswesen": "finanzbuchhaltung",
        # ── Kreditorenbuchhaltung-Varianten ──
        "kreditorenbuchhaltung": "kreditorenbuchhaltung",
        "kreditoren": "kreditorenbuchhaltung",
        "accounts payable": "kreditorenbuchhaltung",
        "ap": "kreditorenbuchhaltung",
        # ── Debitorenbuchhaltung-Varianten ──
        "debitorenbuchhaltung": "debitorenbuchhaltung",
        "debitoren": "debitorenbuchhaltung",
        "accounts receivable": "debitorenbuchhaltung",
        "ar": "debitorenbuchhaltung",
        # ── Anlagenbuchhaltung ──
        "anlagenbuchhaltung": "anlagenbuchhaltung",
        "anlagevermögen": "anlagenbuchhaltung",
        "anlagevermoegen": "anlagenbuchhaltung",
        # ── Abschluesse ──
        "jahresabschluss": "jahresabschluss",
        "jahresabschlüsse": "jahresabschluss",
        "abschlusserstellung": "jahresabschluss",
        "monatsabschluss": "monatsabschluss",
        "monatsabschlüsse": "monatsabschluss",
        "quartalsabschluss": "monatsabschluss",
        # ── Umsatzsteuer ──
        "umsatzsteuer": "umsatzsteuer",
        "ust": "umsatzsteuer",
        "umsatzsteuervoranmeldung": "umsatzsteuer",
        "ust-voranmeldung": "umsatzsteuer",
        # ── Lohn ──
        "lohnbuchhaltung": "lohnbuchhaltung",
        "lohn- und gehaltsbuchhaltung": "lohnbuchhaltung",
        "lohn": "lohnbuchhaltung",
        "entgeltabrechnung": "lohnbuchhaltung",
        "gehaltsabrechnung": "lohnbuchhaltung",
        "payroll": "lohnbuchhaltung",
        # ── Konsolidierung ──
        "konsolidierung": "konsolidierung",
        "konzernkonsolidierung": "konsolidierung",
        # ── HGB / IFRS ──
        "hgb": "hgb",
        "handelsgesetzbuch": "hgb",
        "ifrs": "ifrs",
        "international financial reporting standards": "ifrs",
        # ── Intercompany ──
        "intercompany": "intercompany",
        "ic-abstimmung": "intercompany",
        "konzernverrechnungen": "intercompany",
        # ── Controlling ──
        "controlling": "controlling",
        "kostenrechnung": "controlling",
        # ── Steuern ──
        "steuererklärungen": "steuererklaerung",
        "steuererklaerungen": "steuererklaerung",
        "steuererklärung": "steuererklaerung",
        "steuererklaerung": "steuererklaerung",
        # ── Bilanzierung ──
        "bilanzierung": "bilanzierung",
        "bilanzierung nach hgb": "bilanzierung",
        "bilanzierung nach ifrs": "bilanzierung",
        # ── Software-Varianten ──
        "datev": "datev",
        "datev pro": "datev",
        "datev kanzlei": "datev",
        "datev unternehmen online": "datev",
        "datev kanzlei-rechnungswesen": "datev",
        "datev rechnungswesen": "datev",
        "datev lodas": "datev",
        "datev lohn und gehalt": "datev",
        "sap": "sap",
        "sap fi": "sap fi",
        "sap co": "sap co",
        "sap s/4hana": "sap",
        "sap s4hana": "sap",
        "sap r/3": "sap",
        "sap hana": "sap",
        "lexware": "lexware",
        "lexware buchhaltung": "lexware",
        "navision": "navision",
        "microsoft dynamics nav": "navision",
        "dynamics nav": "navision",
        "ms excel": "excel",
        "microsoft excel": "excel",
        "excel": "excel",
        "addison": "addison",
        "addison oneclick": "addison",
        "oracle": "oracle",
        "oracle financials": "oracle",
    }

    @classmethod
    def _normalize_skill(cls, name: str) -> str:
        """Normalisiert einen Skill-Namen ueber die Synonym-Tabelle."""
        cleaned = name.lower().strip()
        return cls.SKILL_SYNONYMS.get(cleaned, cleaned)

    def _score_skill_overlap(
        self,
        candidate_skills: list[dict],
        job_skills: list[dict],
        job_role: str | None = None,
    ) -> float:
        """Berechnet Skill-Overlap Score (0.0 - 1.0).

        Wenn job_role bekannt (z.B. "bilanzbuchhalter"):
          → Kategorie-gewichtetes Scoring aus skill_weights.json
          → Qualifikationen (10) > Fachkenntnisse (9) > Taetigkeiten (7) > Software (3)

        Fallback (unbekannte Rolle):
          → Essential 70% / Preferred 30% (altes System)

        Matching: Exakt + Synonym-Tabelle. KEIN Substring-Matching!
        """
        if not job_skills or not candidate_skills:
            return 0.0

        # Normalisierte Skill-Namen (ueber Synonym-Tabelle)
        cand_skill_map: dict[str, dict] = {}
        for s in candidate_skills:
            name = s.get("skill", "").lower().strip()
            if name:
                normalized = self._normalize_skill(name)
                cand_skill_map[normalized] = s
                if name != normalized:
                    cand_skill_map[name] = s

        # Prüfe ob gewichtetes Scoring moeglich ist
        use_weighted = (
            job_role is not None
            and self._skill_weights is not None
            and job_role in self._skill_weights
        )

        if use_weighted:
            # ── NEUES SYSTEM: Kategorie-gewichtetes Scoring ──
            weighted_score_sum = 0.0
            weighted_max_sum = 0.0

            for js in job_skills:
                skill_name = js.get("skill", "").lower().strip()
                if not skill_name:
                    continue

                # Bestimme Gewicht fuer diesen Skill
                skill_weight = self._get_skill_weight(job_role, skill_name) or 5  # Default 5

                weighted_max_sum += skill_weight

                # 1. Exakter Match
                match_score = 0.0
                matched_skill = cand_skill_map.get(skill_name)

                # 2. Synonym-Match
                if not matched_skill:
                    normalized_job = self._normalize_skill(skill_name)
                    matched_skill = cand_skill_map.get(normalized_job)
                    if matched_skill:
                        match_score = 0.9

                if matched_skill:
                    if match_score == 0.0:
                        match_score = 1.0

                    # Recency-Abschlag
                    recency = matched_skill.get("recency", "aktuell")
                    if recency == "kuerzlich":
                        match_score *= 0.7
                    elif recency == "veraltet":
                        match_score *= 0.3

                    # Proficiency-Bonus
                    prof = matched_skill.get("proficiency", "grundlagen")
                    if prof == "experte":
                        match_score = min(1.0, match_score * 1.1)

                    weighted_score_sum += match_score * skill_weight

            if weighted_max_sum == 0:
                return 0.0
            return weighted_score_sum / weighted_max_sum

        else:
            # ── FALLBACK: Altes essential/preferred System ──
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

                match_score = 0.0
                matched_skill = cand_skill_map.get(skill_name)

                if not matched_skill:
                    normalized_job = self._normalize_skill(skill_name)
                    matched_skill = cand_skill_map.get(normalized_job)
                    if matched_skill:
                        match_score = 0.9

                if matched_skill:
                    if match_score == 0.0:
                        match_score = 1.0

                    recency = matched_skill.get("recency", "aktuell")
                    if recency == "kuerzlich":
                        match_score *= 0.7
                    elif recency == "veraltet":
                        match_score *= 0.3

                    prof = matched_skill.get("proficiency", "grundlagen")
                    if prof == "experte":
                        match_score = min(1.0, match_score * 1.1)

                    if importance == "essential":
                        essential_matched += match_score
                    else:
                        preferred_matched += match_score

            essential_score = (essential_matched / essential_total) if essential_total > 0 else 0.5
            preferred_score = (preferred_matched / preferred_total) if preferred_total > 0 else 0.5

            return essential_score * 0.7 + preferred_score * 0.3

    def _score_seniority_fit(self, candidate_level: int, job_level: int) -> tuple[float, str]:
        """Berechnet Seniority-Fit Score (0.0 - 1.0) + Qualification-Tag.

        Returns:
            (score, tag) — Tag wird im v2_score_breakdown gespeichert.
        """
        gap = candidate_level - job_level  # Positiv = Kandidat hoeher

        if gap == 0:
            return 1.0, "passt"
        elif gap == 1:
            return 0.7, "leicht_ueberqualifiziert"
        elif gap == -1:
            return 0.75, "leicht_unterqualifiziert"
        elif gap == 2:
            return 0.3, "ueberqualifiziert"
        elif gap == -2:
            return 0.3, "unterqualifiziert"
        elif gap >= 3:
            return 0.0, "stark_ueberqualifiziert"
        else:  # gap <= -3
            return 0.0, "stark_unterqualifiziert"

    def _score_career_fit(
        self,
        candidate_trajectory: str,
        candidate_level: int,
        job_level: int,
    ) -> tuple[float, str | None]:
        """Berechnet Karriere-Fit Score (0.0 - 1.0) + Career-Note.

        Returns:
            (score, career_note) — Note wird im v2_score_breakdown gespeichert.
        """
        gap = job_level - candidate_level  # Positiv = Job ist hoeher

        if candidate_trajectory == "aufsteigend":
            if gap == 1:
                return 1.0, "perfekter_naechster_schritt"
            elif gap == 0:
                return 0.8, None
            elif gap == -1:
                return 0.3, "rueckschritt_fuer_aufsteigenden"
            elif gap >= 2:
                return 0.3, None
            else:
                return 0.1, "starker_rueckschritt"
        elif candidate_trajectory == "lateral":
            if gap == 0:
                return 1.0, None
            elif abs(gap) == 1:
                return 0.6, None
            else:
                return 0.2, None
        elif candidate_trajectory == "absteigend":
            if gap == -1:
                return 0.7, "bewusster_downshift"
            elif gap <= -2:
                return 0.5, "starker_downshift"
            elif gap == 0:
                return 0.4, "widerspruch_will_runter_aber_gleiches_level"
            else:
                return 0.4, "widerspruch_will_runter_aber_hoeher"
        elif candidate_trajectory == "einstieg":
            if job_level <= 2:
                return 0.8, "einstieg_passend"
            else:
                return 0.2, "einsteiger_zu_hohe_stelle"

        return 0.5, None

    def _score_software_match(
        self,
        candidate_skills: list[dict],
        job_skills: list[dict],
        candidate_erp: list[str] | None = None,
    ) -> float:
        """Berechnet Software-Match Score (0.0 - 1.0).

        DATEV + DATEV = 1.0
        SAP + SAP = 1.0
        DATEV + SAP = 0.2 (6-12 Monate Umstieg)
        Keine Software-Anforderung = 0.5 (neutral)

        Nutzt BEIDE Quellen: v2_structured_skills UND candidates.erp
        """
        datev_keywords = {"datev", "datev unternehmen online", "datev kanzlei"}
        sap_keywords = {"sap", "sap fi", "sap co", "sap s/4hana", "sap s4hana"}

        def detect_ecosystem(skills: list[dict], erp_list: list[str] | None = None) -> set[str]:
            ecosystems = set()
            for s in skills:
                name = s.get("skill", "").lower()
                if any(kw in name for kw in datev_keywords):
                    ecosystems.add("datev")
                if any(kw in name for kw in sap_keywords):
                    ecosystems.add("sap")
            # Zusaetzlich: ERP-Array pruefen
            if erp_list:
                for erp in erp_list:
                    erp_lower = erp.lower()
                    if any(kw in erp_lower for kw in datev_keywords):
                        ecosystems.add("datev")
                    if any(kw in erp_lower for kw in sap_keywords):
                        ecosystems.add("sap")
            return ecosystems

        job_eco = detect_ecosystem(job_skills)
        cand_eco = detect_ecosystem(candidate_skills, candidate_erp)

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
            return 0.2  # 6-12 Monate Umstieg

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

    # ── v2.5 Neue Dimensionen ──────────────────────────────

    # Job-Titel Verwandtschaftsgruppen (Finance/Buchhaltung)
    JOB_TITLE_GROUPS: dict[str, set[str]] = {
        "finanzbuchhaltung": {
            "finanzbuchhalter", "finanzbuchhalterin", "hauptbuchhalter", "hauptbuchhalterin",
            "bilanzbuchhalter", "bilanzbuchhalterin", "accountant", "buchhalter", "buchhalterin",
            "finanzbuchhalter/in", "bilanzbuchhalter/in", "buchhalter/in",
        },
        "kreditorenbuchhaltung": {
            "kreditorenbuchhalter", "kreditorenbuchhalterin", "sachbearbeiter kreditoren",
            "sachbearbeiterin kreditoren", "ap accountant", "kreditorenbuchhalter/in",
        },
        "debitorenbuchhaltung": {
            "debitorenbuchhalter", "debitorenbuchhalterin", "sachbearbeiter debitoren",
            "sachbearbeiterin debitoren", "ar accountant", "debitorenbuchhalter/in",
        },
        "lohnbuchhaltung": {
            "lohnbuchhalter", "lohnbuchhalterin", "payroll", "entgeltabrechnung",
            "gehaltsabrechnung", "lohnbuchhalter/in", "payroll specialist",
        },
        "controlling": {
            "controller", "controllerin", "financial controller", "kostenrechner",
            "controller/in",
        },
        "steuern": {
            "steuerfachangestellter", "steuerfachangestellte", "tax specialist",
            "steuerberater", "steuerberaterin", "steuerfachangestellte/r",
        },
        "leitung": {
            "leiter rechnungswesen", "leiterin rechnungswesen", "head of accounting",
            "kaufmännischer leiter", "kaufmaennischer leiter", "cfo",
            "leiter buchhaltung", "leiterin buchhaltung",
        },
    }

    def _score_job_title_fit(
        self,
        candidate_titles: list[str],
        job_title: str | None,
        job_manual_title: str | None,
        job_hotlist_title: str | None,
    ) -> float:
        """Berechnet Job-Titel-Fit Score (0.0 - 1.0).

        Exakter Match = 1.0
        Verwandte Gruppe = 0.7
        Cross-Gruppe = 0.2
        Kein Match = 0.3
        """
        if not candidate_titles:
            return 0.3  # Keine Titel-Info → neutral

        # Job-Titel sammeln (manual_title hat Prioritaet)
        job_titles = []
        if job_manual_title:
            job_titles.append(job_manual_title.lower().strip())
        if job_hotlist_title:
            job_titles.append(job_hotlist_title.lower().strip())
        if job_title:
            job_titles.append(job_title.lower().strip())

        if not job_titles:
            return 0.3

        cand_titles_lower = [t.lower().strip() for t in candidate_titles if t]

        # 1. Exakter Match pruefen
        for jt in job_titles:
            for ct in cand_titles_lower:
                if ct == jt or ct in jt or jt in ct:
                    return 1.0

        # 2. Gruppen-Match pruefen
        def find_group(title: str) -> str | None:
            title_lower = title.lower().strip()
            for group_name, members in self.JOB_TITLE_GROUPS.items():
                if title_lower in members:
                    return group_name
                # Teilstring-Check fuer zusammengesetzte Titel
                for member in members:
                    if member in title_lower or title_lower in member:
                        return group_name
            return None

        job_groups = set()
        for jt in job_titles:
            g = find_group(jt)
            if g:
                job_groups.add(g)

        cand_groups = set()
        for ct in cand_titles_lower:
            g = find_group(ct)
            if g:
                cand_groups.add(g)

        if job_groups and cand_groups:
            if job_groups & cand_groups:
                return 0.7  # Gleiche Titel-Gruppe

            # Cross-Gruppe Penalty
            # Lohnbuchhaltung ↔ Finanzbuchhaltung = 0.2 (komplett anderes Fachgebiet)
            cross_penalty_groups = {"lohnbuchhaltung", "controlling", "steuern"}
            if (job_groups & cross_penalty_groups) or (cand_groups & cross_penalty_groups):
                return 0.2
            return 0.4  # Andere Gruppen-Kombination

        return 0.3  # Keine Gruppe erkannt

    def _score_industry_fit(
        self,
        candidate_industries: list[str],
        job_industry: str | None,
    ) -> float:
        """Berechnet Branchenerfahrung Score (0.0 - 1.0).

        Gleiche Branche = 1.0
        Verwandte Branche = 0.6
        Keine Erfahrung = 0.3
        """
        if not job_industry or not candidate_industries:
            return 0.3  # Keine Daten → neutral

        job_ind = job_industry.lower().strip()
        cand_inds = [i.lower().strip() for i in candidate_industries if i]

        # Exakter Match
        for ci in cand_inds:
            if ci == job_ind or ci in job_ind or job_ind in ci:
                return 1.0

        # Verwandte Branchen
        related_groups = {
            "automotive": {"maschinenbau", "automotive", "automobilindustrie", "fahrzeugbau"},
            "pharma": {"pharma", "pharmazeutisch", "chemie", "medizintechnik", "gesundheit"},
            "finance": {"bank", "finanzdienstleistung", "versicherung", "finanz"},
            "tech": {"it", "software", "technologie", "digital", "telekommunikation"},
            "industrie": {"maschinenbau", "produktion", "fertigung", "industrie"},
            "beratung": {"beratung", "consulting", "wirtschaftspruefung", "steuerberatung", "kanzlei"},
        }

        job_related = set()
        cand_related = set()
        for group_name, members in related_groups.items():
            if any(m in job_ind for m in members):
                job_related.add(group_name)
            for ci in cand_inds:
                if any(m in ci for m in members):
                    cand_related.add(group_name)

        if job_related & cand_related:
            return 0.6

        return 0.3  # Keine Branchenerfahrung

    async def _score_candidates(
        self,
        job: Job,
        candidates: list[MatchCandidate],
        weights: dict[str, float],
    ) -> list[ScoredMatch]:
        """Schicht 2: Berechnet gewichteten Score fuer alle Kandidaten.

        v2.5: 7 Dimensionen + BiBu-Multiplikator + Qualification-Tag + Career-Note.
        Location ist NUR Hard Filter (30km) — kein Soft-Score mehr.

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
        job_industry = job.industry

        # Skill-Weights laden (einmalig) + Job-Rolle erkennen
        self._load_skill_weights()
        job_role = self._detect_job_role(
            getattr(job, "hotlist_job_title", None),
            job.position,
        )

        # BiBu-Check: Braucht der Job einen Bilanzbuchhalter?
        # Erweiterte Detection: Titel > Position > Skills
        job_requires_bibu = False
        title_text = ""
        if getattr(job, "hotlist_job_title", None):
            title_text += job.hotlist_job_title.lower()
        if job.position:
            title_text += " " + job.position.lower()
        if "bilanzbuchhalter" in title_text:
            job_requires_bibu = True
        elif job_skills:
            # Fallback: Check in Skills (gelockert — nur Name, ohne importance/category)
            for skill in job_skills:
                name = skill.get("skill", "").lower()
                if "bilanzbuchhalter" in name:
                    job_requires_bibu = True
                    break

        # Gewichte normalisieren (Summe = 100)
        total_weight = sum(weights.values())
        if total_weight == 0:
            total_weight = 100

        scored = []
        for cand in candidates:
            # ── 7 Score-Dimensionen (alle 0.0 - 1.0) ──

            skill_score = self._score_skill_overlap(
                cand.structured_skills, job_skills, job_role=job_role
            )
            seniority_score, qualification_tag = self._score_seniority_fit(
                cand.seniority_level, job_level
            )
            job_title_score = self._score_job_title_fit(
                cand.job_titles,
                job.position,
                getattr(job, "manual_job_title", None),
                job.hotlist_job_title,
            )
            embedding_score = self._score_embedding_similarity(
                cand.embedding_current, job_embedding
            )
            industry_score = self._score_industry_fit(
                cand.industries, job_industry
            )
            career_score, career_note = self._score_career_fit(
                cand.career_trajectory, cand.seniority_level, job_level
            )
            software_score = self._score_software_match(
                cand.structured_skills, job_skills, cand.erp
            )

            # ── Gewichtete Summe (0-100) ──
            total = (
                skill_score * weights.get("skill_overlap", 27) +
                seniority_score * weights.get("seniority_fit", 20) +
                job_title_score * weights.get("job_title_fit", 18) +
                embedding_score * weights.get("embedding_sim", 15) +
                industry_score * weights.get("industry_fit", 8) +
                career_score * weights.get("career_fit", 7) +
                software_score * weights.get("software_match", 5)
            ) / total_weight * 100

            # ── BiBu-Multiplikator (NACH Gewichtung, VOR Speichern) ──
            bibu_multiplier = 1.0
            if job_requires_bibu:
                # Check 1: v2_certifications (z.B. ["Bilanzbuchhalter"])
                candidate_has_bibu = any(
                    "bilanzbuchhalter" in c.lower()
                    for c in cand.certifications
                ) if cand.certifications else False
                # Check 2: structured_skills mit category=zertifizierung
                if not candidate_has_bibu and cand.structured_skills:
                    candidate_has_bibu = any(
                        "bilanzbuchhalter" in s.get("skill", "").lower()
                        and s.get("category", "") == "zertifizierung"
                        for s in cand.structured_skills
                    )
                if candidate_has_bibu:
                    bibu_multiplier = 1.3   # +30% Bonus
                else:
                    bibu_multiplier = 0.6   # -40% Penalty
                total *= bibu_multiplier

            # ── Empty CV Penalty (dreistufig) ──
            empty_cv_penalty = None
            role_summary = (cand.current_role_summary or "").lower()
            if "keine berufserfahrung" in role_summary:
                empty_cv_penalty = 0.1
                total *= 0.1  # 90% Penalty
            elif "keine ausbildung" in role_summary:
                empty_cv_penalty = 0.2
                total *= 0.2  # 80% Penalty
            elif len(cand.structured_skills) < 3:
                empty_cv_penalty = 0.3
                total *= 0.3  # 70% Penalty

            total = min(100, max(0, total))  # Cap 0-100

            breakdown = {
                "skill_overlap": round(skill_score, 3),
                "seniority_fit": round(seniority_score, 3),
                "job_title_fit": round(job_title_score, 3),
                "embedding_sim": round(embedding_score, 3),
                "industry_fit": round(industry_score, 3),
                "career_fit": round(career_score, 3),
                "software_match": round(software_score, 3),
                "distance_km": cand.distance_km,
                # v2.5 Tags
                "qualification_tag": qualification_tag,
                "candidate_level": cand.seniority_level,
                "job_level": job_level,
                "bibu_multiplier": bibu_multiplier if bibu_multiplier != 1.0 else None,
                "empty_cv_penalty": empty_cv_penalty,
                "job_role": job_role,
            }
            if career_note:
                breakdown["career_note"] = career_note

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

        # Job laden (mit Company fuer Standort-Fallback)
        from sqlalchemy.orm import selectinload
        result = await self.db.execute(
            select(Job).options(selectinload(Job.company)).where(Job.id == job_id)
        )
        job = result.scalar_one_or_none()
        if not job:
            raise ValueError(f"Job {job_id} nicht gefunden")

        if not job.v2_profile_created_at:
            raise ValueError(f"Job {job_id} hat kein v2-Profil. Zuerst profilieren!")

        # Fallback: Wenn Job keine Koordinaten hat → nutze Unternehmens-Standort
        if job.location_coords is None and job.company and job.company.location_coords is not None:
            job.location_coords = job.company.location_coords
            logger.info(
                f"Job {job_id}: Nutze Unternehmens-Standort '{job.company.name}' als Distanz-Fallback"
            )

        job_level = job.v2_seniority_level or 2
        job_category = job.hotlist_job_title or job.position
        weights = await self._load_weights(job_category=job_category)

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

            # Distanz aus Breakdown extrahieren
            dist = sm.breakdown.get("distance_km") if sm.breakdown else None

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
                    match_obj.distance_km = dist
                    # Re-Import Schutz: REJECTED/PLACED Status NICHT zuruecksetzen
                    # Nur NEW/AI_CHECKED duerfen aktualisiert werden
                    if match_obj.status not in (MatchStatus.REJECTED, MatchStatus.PLACED, MatchStatus.PRESENTED):
                        match_obj.status = MatchStatus.NEW
            else:
                # Neues Match erstellen
                match = Match(
                    job_id=job_id,
                    candidate_id=sm.candidate_id,
                    v2_score=sm.total_score,
                    v2_score_breakdown=sm.breakdown,
                    v2_matched_at=now,
                    status=MatchStatus.NEW,
                    distance_km=dist,
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
                # Jobs die kein aktives (nicht-rejected) v2-Match haben
                # -> Jobs mit NUR rejected Matches werden erneut gematcht
                subq = (
                    select(Match.job_id)
                    .where(
                        Match.v2_matched_at.isnot(None),
                        Match.status != MatchStatus.REJECTED,
                    )
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
