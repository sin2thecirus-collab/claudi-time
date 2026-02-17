"""Matching Engine v2 — 3-Schichten Enterprise-Matching.

Schicht 1: Hard Filters (SQL, <5ms) — eliminiert 85-90% der Kandidaten
Schicht 2: Structured Scoring (Python, <50ms/200 Kandidaten) — gewichtete Score-Berechnung
Schicht 3: Pattern Boost/Penalty — gelernte Regeln anwenden

Kosten pro Match: $0.00 (alles lokal/vorberechnet)
"""

import json
import logging
import math
import re
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
    # v3: Kandidaten-Rolle fuer Gate-Checks
    primary_role: str | None = None  # z.B. "Finanzbuchhalter/in"
    classification_data: dict = field(default_factory=dict)
    # Phase 10: Google Maps Fahrzeit
    drive_time_car_min: int | None = None
    drive_time_transit_min: int | None = None
    postal_code: str | None = None  # PLZ für Fahrzeit-Caching
    _lat: float | None = None  # Breitengrad (für Distance Matrix API)
    _lng: float | None = None  # Längengrad (für Distance Matrix API)


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
    "skill_overlap": 15.0,   # Skills (reduziert von 25 — Simulation: Korrelation -41.1, optimal 3-15)
    "seniority_fit": 25.0,   # Level-Matching (bewaehrt — Korrelation +9.8)
    "job_title_fit": 0.0,    # RAUS — Titel sind zu oft falsch
    "embedding_sim": 21.0,   # Semantische Aehnlichkeit (leicht erhoeht — zuverlaessigstes Signal, Korrelation +14.0)
    "industry_fit": 12.0,    # Branchenerfahrung (erhoeht — Korrelation +3.4)
    "career_fit": 12.0,      # Karriere-Richtung (erhoeht — Korrelation +7.4)
    "software_match": 15.0,  # DATEV/SAP-Ecosystem (erhoeht — Korrelation +6.5, wichtig fuer Lohn/StFA)
    # Summe: 15 + 25 + 0 + 21 + 12 + 12 + 15 = 100
    # Begruendung: Erweiterte Simulation (11.200 Kombinationen) — Q-Score 1290 vs 1164 aktuell
    # skill_overlap ist verrauschtes Signal (avg 0.12-0.27), Software/Embedding sind zuverlaessiger
    # Location ist KEIN Score — nur Hard Filter (30km)
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

    TOP_N = 50   # Max. Matches pro Job (zurueck von 200 — Bloat-Fix)
    MIN_SCORE = 25.0  # Matches unter diesem Score werden nicht gespeichert

    # Skill-Weights Cache (Klassen-Level, einmal laden)
    _skill_weights: dict | None = None
    _skill_to_category: dict[str, tuple[str, int]] | None = None  # skill_lower → (kategorie, weight)

    # Skill-Hierarchie Cache (Klassen-Level, einmal laden)
    _skill_hierarchy: dict | None = None

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
    def _load_skill_hierarchy(cls) -> dict:
        """Laedt skill_hierarchy.json (cached auf Klassen-Level).

        Die Hierarchie definiert Parent→Children-Beziehungen fuer Skills.
        Beispiel: Job sucht 'Finanzbuchhaltung' → expandiert zu
        'Kreditorenbuchhaltung', 'Debitorenbuchhaltung', etc.
        """
        if cls._skill_hierarchy is not None:
            return cls._skill_hierarchy

        config_path = Path(__file__).parent.parent / "config" / "skill_hierarchy.json"
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cls._skill_hierarchy = json.load(f)
            total_parents = sum(len(cats) for cats in cls._skill_hierarchy.values())
            logger.info(
                f"Skill-Hierarchie geladen: {len(cls._skill_hierarchy)} Rollen, "
                f"{total_parents} Parent-Skills"
            )
        except FileNotFoundError:
            logger.warning(f"skill_hierarchy.json nicht gefunden: {config_path}")
            cls._skill_hierarchy = {}
        except json.JSONDecodeError as e:
            logger.error(f"skill_hierarchy.json Parse-Fehler: {e}")
            cls._skill_hierarchy = {}
        return cls._skill_hierarchy

    @classmethod
    def _expand_job_skills_with_hierarchy(
        cls,
        job_skills: list[dict],
        job_role: str,
    ) -> list[dict]:
        """Expandiert generische Job-Skills mit Hierarchie-Children.

        Beispiel fuer Rolle 'finanzbuchhalter':
          Input:  [{"skill": "Finanzbuchhaltung", ...}, {"skill": "DATEV", ...}]
          Output: [{"skill": "Finanzbuchhaltung", ...}, {"skill": "DATEV", ...},
                   {"skill": "Kreditorenbuchhaltung", ...},
                   {"skill": "Debitorenbuchhaltung", ...}, ...]

        Children bekommen dieselben Attribute (importance, category) wie der Parent,
        aber mit einem Flag 'from_hierarchy': True fuer Debugging.
        """
        hierarchy = cls._load_skill_hierarchy()
        role_hierarchy = hierarchy.get(job_role, {})
        if not role_hierarchy:
            return job_skills

        # Baue Lookup: parent_skill_lower → config
        parent_lookup: dict[str, dict] = {}
        for parent_skill, config in role_hierarchy.items():
            parent_lookup[parent_skill.lower().strip()] = config
            # Auch Synonym-normalisierten Namen registrieren
            normalized = cls._normalize_skill(parent_skill.lower().strip())
            if normalized not in parent_lookup:
                parent_lookup[normalized] = config

        # Sammle existierende Skill-Namen (lowercase) um Duplikate zu vermeiden
        existing_skills = {js.get("skill", "").lower().strip() for js in job_skills if isinstance(js, dict)}

        expanded = list(job_skills)  # Kopie — Original nicht veraendern

        for js in job_skills:
            if not isinstance(js, dict):
                continue
            skill_name = js.get("skill", "").lower().strip()
            if not skill_name:
                continue

            # Check ob dieser Job-Skill ein Parent in der Hierarchie ist
            config = parent_lookup.get(skill_name)
            if config is None:
                # Auch normalisierten Namen versuchen
                config = parent_lookup.get(cls._normalize_skill(skill_name))
            if config is None:
                # Auch Kern-Skill versuchen (z.B. "Finanzbuchhaltung (allg.)" → "finanzbuchhaltung")
                core = cls._extract_core_skill(skill_name)
                if core != skill_name:
                    config = parent_lookup.get(core)

            if config is None:
                continue

            # Parent gefunden → Children hinzufuegen
            for child_skill in config.get("children", []):
                child_lower = child_skill.lower().strip()
                if child_lower not in existing_skills:
                    expanded.append({
                        "skill": child_skill,
                        "importance": js.get("importance", "preferred"),
                        "category": js.get("category", ""),
                        "from_hierarchy": True,
                    })
                    existing_skills.add(child_lower)

        if len(expanded) > len(job_skills):
            added = len(expanded) - len(job_skills)
            logger.debug(
                f"Skill-Hierarchie ({job_role}): {added} Children hinzugefuegt "
                f"({len(job_skills)} → {len(expanded)} Skills)"
            )

        return expanded

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
    def _detect_job_role(cls, job_title: str | None, position: str | None, classification_data: dict | None = None) -> str | None:
        """Erkennt die Job-Rolle fuer Skill-Weight-Lookup.

        PRIORITAET (V2):
        1. classification_data.primary_role (von Deep Classification) — zuverlaessigste Quelle
        2. Fallback: Titel/Position Keywords (wie bisher)
        """
        if cls._skill_weights is None:
            cls._load_skill_weights()

        # V2: classification_data hat hoechste Prioritaet
        if classification_data and isinstance(classification_data, dict):
            primary_role = classification_data.get("primary_role")
            if primary_role:
                # Mapping: GPT-Rolle → skill_weights.json Key
                role_mapping = {
                    "Bilanzbuchhalter/in": "bilanzbuchhalter",
                    "Finanzbuchhalter/in": "finanzbuchhalter",
                    "Kreditorenbuchhalter/in": "kreditorenbuchhalter",
                    "Debitorenbuchhalter/in": "debitorenbuchhalter",
                    "Lohnbuchhalter/in": "lohnbuchhalter",
                    "Steuerfachangestellte/r": "steuerfachangestellte",
                }
                role_key = role_mapping.get(primary_role)
                if role_key and role_key in (cls._skill_weights or {}):
                    return role_key

        # Fallback: Titel/Position Keywords
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
        if "kreditorenbuchhalter" in search_text or ("kreditoren" in search_text and "debitoren" not in search_text):
            return "kreditorenbuchhalter"
        if "debitorenbuchhalter" in search_text or ("debitoren" in search_text and "kreditoren" not in search_text):
            return "debitorenbuchhalter"
        if "finanzbuchhalter" in search_text:
            return "finanzbuchhalter"
        if "lohnbuchhalter" in search_text:
            return "lohnbuchhalter"

        # Englische Jobtitel → deutsche Rollen-Keys
        if "accounts payable" in search_text and "accounts receivable" not in search_text:
            return "kreditorenbuchhalter"
        if "accounts receivable" in search_text and "accounts payable" not in search_text:
            return "debitorenbuchhalter"
        if "financial accountant" in search_text or "financial accounting" in search_text:
            return "finanzbuchhalter"
        if "accountant" in search_text and "tax" not in search_text:
            return "finanzbuchhalter"
        if "bookkeeper" in search_text or "book keeper" in search_text:
            return "finanzbuchhalter"
        if "payroll" in search_text:
            return "lohnbuchhalter"
        if "tax accountant" in search_text or "tax consultant" in search_text:
            return "steuerfachangestellte"
        if "financial controller" in search_text or "head of accounting" in search_text:
            return "bilanzbuchhalter"
        if "senior accountant" in search_text or "chief accountant" in search_text:
            return "bilanzbuchhalter"

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
                # Phase 10: PLZ + Koordinaten für Google Maps Fahrzeit
                Candidate.postal_code,             # 16
                func.ST_Y(func.ST_GeomFromWKB(Candidate.address_coords)).label("cand_lat"),  # 17
                func.ST_X(func.ST_GeomFromWKB(Candidate.address_coords)).label("cand_lng"),  # 18
                # v3: Kandidaten-Rolle fuer Gate-Checks
                Candidate.hotlist_job_title,        # 19 (primary_role)
                Candidate.classification_data,      # 20
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
                postal_code=row[16],
                _lat=row[17],
                _lng=row[18],
                # v3: Kandidaten-Rolle
                primary_role=row[19],
                classification_data=row[20] or {},
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
        "ms office (excel)": "excel",
        "ms office excel": "excel",
        "ms-office excel": "excel",
        "addison": "addison",
        "addison oneclick": "addison",
        "oracle": "oracle",
        "oracle financials": "oracle",
        # ── Weitere Software ──
        "sap hcm": "sap hr",
        "sap hr": "sap hr",
        "sap mm": "sap mm",
        "sap sd": "sap sd",
        "sage": "sage",
        "sage 100": "sage",
        "sage office line": "sage",
        "diamant": "diamant",
        "diamant/3": "diamant",
        "loga": "loga",
        "p&i loga": "loga",
        "paisy": "paisy",
        "adp": "adp",
        "personio": "personio",
        "lucanet": "lucanet",
        "dynamics 365": "dynamics",
        "microsoft dynamics 365": "dynamics",
        "microsoft dynamics": "dynamics",
        # ── Kreditoren/Debitoren-spezifisch ──
        "vendor management": "lieferantenmanagement",
        "lieferantenmanagement": "lieferantenmanagement",
        "eingangsrechnungsprüfung": "rechnungspruefung",
        "eingangsrechnungspruefung": "rechnungspruefung",
        "invoice processing": "rechnungspruefung",
        "collections": "mahnwesen",
        "mahnwesen": "mahnwesen",
        "credit management": "forderungsmanagement",
        "forderungsmanagement": "forderungsmanagement",
        "cash application": "zahlungsabgleich",
        "zahlungsabgleich": "zahlungsabgleich",
        "fakturierung": "fakturierung",
        "invoicing": "fakturierung",
        # ── Allgemeine Buchhaltungs-Synonyme ──
        "sachkontenbuchhaltung": "hauptbuchhaltung",
        "hauptbuchhaltung": "hauptbuchhaltung",
        "general ledger": "hauptbuchhaltung",
        "gl accounting": "hauptbuchhaltung",
        "account reconciliation": "kontenabstimmung",
        "kontenabstimmung": "kontenabstimmung",
        "bank reconciliation": "kontenabstimmung",
        "kontenklärung": "kontenabstimmung",
        "kontenklaerung": "kontenabstimmung",
        "payment processing": "zahlungsverkehr",
        "zahlungsverkehr": "zahlungsverkehr",
        "rechnungswesen": "finanzbuchhaltung",
        "buchhaltung": "finanzbuchhaltung",
        # ── Anlagenbuchhaltung ──
        "fixed assets": "anlagenbuchhaltung",
        "asset accounting": "anlagenbuchhaltung",
        "fixed asset accounting": "anlagenbuchhaltung",
        # ── Reporting ──
        "reporting": "berichtswesen",
        "financial reporting": "berichtswesen",
        "berichtswesen": "berichtswesen",
        # ── OP-Verwaltung ──
        "op-verwaltung": "op-verwaltung",
        "offene posten": "op-verwaltung",
        "offene-posten-verwaltung": "op-verwaltung",
    }

    @classmethod
    def _normalize_skill(cls, name: str) -> str:
        """Normalisiert einen Skill-Namen ueber die Synonym-Tabelle."""
        cleaned = name.lower().strip()
        return cls.SKILL_SYNONYMS.get(cleaned, cleaned)

    # Kategorien die im Skill-Overlap IGNORIERT werden (druecken Score kuenstlich runter)
    _SKIP_CATEGORIES: set[str] = {"sprachlich", "sprache", "soft_skill", "softskill"}

    # Sprach-Patterns: "Deutsch (sehr gut)" → "deutsch", "Englisch (gut)" → "englisch"
    _LANG_BRACKET_RE = re.compile(r"^(.+?)\s*\(.*\)$")

    @classmethod
    def _extract_core_skill(cls, skill_name: str) -> str:
        """Extrahiert den Kern-Skill-Namen.

        'Englisch (gut)' → 'englisch'
        'MS Office (Excel)' → 'ms office (excel)'  (kein Sprach-Pattern)
        'HGB-Abschlusserstellung' → versucht auch 'hgb'
        """
        name = skill_name.lower().strip()
        # Sprach-Pattern: "Englisch (gut)" → "englisch"
        m = cls._LANG_BRACKET_RE.match(name)
        if m:
            core = m.group(1).strip()
            # Nur fuer bekannte Sprachen strippen, nicht fuer "MS Office (Excel)"
            known_langs = {"deutsch", "englisch", "französisch", "franzoesisch",
                           "spanisch", "italienisch", "russisch", "türkisch",
                           "tuerkisch", "polnisch", "tschechisch", "portugiesisch",
                           "niederländisch", "niederlaendisch", "chinesisch",
                           "japanisch", "arabisch", "korean"}
            if core in known_langs:
                return core
        return name

    def _is_irrelevant_skill(self, skill_name: str, category: str | None) -> bool:
        """Prueft ob ein Skill fuer den fachlichen Overlap irrelevant ist.

        Ignoriert: Sprachen, Soft Skills (Teamarbeit, Analytisches Denken etc.)
        """
        if category and category.lower() in self._SKIP_CATEGORIES:
            return True

        name = skill_name.lower().strip()

        # Sprachen erkennen (auch ohne category-Tag)
        known_langs = {"deutsch", "englisch", "französisch", "franzoesisch",
                       "spanisch", "italienisch", "russisch", "türkisch",
                       "tuerkisch", "polnisch", "tschechisch", "portugiesisch",
                       "niederländisch", "niederlaendisch"}
        core = self._extract_core_skill(name)
        if core in known_langs:
            return True
        # Pattern: "Deutsch (sehr gut)", "Englisch (C1)" etc.
        if self._LANG_BRACKET_RE.match(name) and core in known_langs:
            return True

        # Soft Skills erkennen
        soft_skills = {"teamarbeit", "analytisches denken", "kommunikation",
                       "eigeninitiative", "selbstständigkeit", "selbststaendigkeit",
                       "zuverlässigkeit", "zuverlaessigkeit", "flexibilität",
                       "flexibilitaet", "belastbarkeit", "organisationsfähigkeit",
                       "organisationsfaehigkeit", "problemlösung", "problemloesung",
                       "zeitmanagement", "motivation", "eigenmotivation",
                       "kundenorientierung", "serviceorientierung", "sorgfalt",
                       "genauigkeit", "teamfähigkeit", "teamfaehigkeit"}
        if name in soft_skills:
            return True

        return False

    def _score_skill_overlap(
        self,
        candidate_skills: list[dict],
        job_skills: list[dict],
        job_role: str | None = None,
        candidate_certifications: list[str] | None = None,
    ) -> float:
        """Berechnet Skill-Overlap Score (0.0 - 1.0).

        v2.6 Verbesserungen:
          1. Sprachen & Soft Skills werden aus Job-Skills gefiltert (irrelevant)
          2. Zertifizierungen (v2_certifications) werden als Bonus-Skills einbezogen
          3. Fuzzy-Matching: "HGB-Abschlusserstellung" matched "HGB" ueber contains-Check
          4. Sprach-Normalisierung: "Englisch (gut)" matched "Englisch"

        Wenn job_role bekannt (z.B. "bilanzbuchhalter"):
          → Kategorie-gewichtetes Scoring aus skill_weights.json
          → Qualifikationen (10) > Fachkenntnisse (9) > Taetigkeiten (7) > Software (3)

        Fallback (unbekannte Rolle):
          → Essential 70% / Preferred 30% (altes System)
        """
        if not job_skills or not candidate_skills:
            return 0.0

        # ── Kandidaten-Skills aufbauen (normalisiert) ──
        cand_skill_map: dict[str, dict] = {}
        for s in candidate_skills:
            if not isinstance(s, dict):
                continue
            name = s.get("skill", "").lower().strip()
            if name:
                normalized = self._normalize_skill(name)
                cand_skill_map[normalized] = s
                if name != normalized:
                    cand_skill_map[name] = s
                # Auch Kern-Name registrieren (z.B. "englisch" aus "englisch (gut)")
                core = self._extract_core_skill(name)
                if core != name and core != normalized:
                    cand_skill_map[core] = s

        # ── Zertifizierungen als Bonus-Skills hinzufuegen (weight=qualifikationen) ──
        if candidate_certifications:
            for cert in candidate_certifications:
                cert_lower = cert.lower().strip()
                if cert_lower and cert_lower not in cand_skill_map:
                    normalized_cert = self._normalize_skill(cert_lower)
                    cert_entry = {
                        "skill": cert,
                        "proficiency": "experte",
                        "recency": "aktuell",
                        "category": "zertifizierung",
                    }
                    cand_skill_map[cert_lower] = cert_entry
                    cand_skill_map[normalized_cert] = cert_entry

        # ── Job-Skills filtern: Sprachen & Soft Skills raus ──
        filtered_job_skills = []
        for js in job_skills:
            if not isinstance(js, dict):
                continue
            skill_name = js.get("skill", "")
            category = js.get("category", "")
            if not self._is_irrelevant_skill(skill_name, category):
                filtered_job_skills.append(js)

        if not filtered_job_skills:
            return 0.0

        # ── Skill-Hierarchie: Generische Job-Skills expandieren ──
        # Beispiel: "Finanzbuchhaltung" → + "Kreditorenbuchhaltung", "Debitorenbuchhaltung", etc.
        if job_role:
            filtered_job_skills = self._expand_job_skills_with_hierarchy(
                filtered_job_skills, job_role
            )

        # ── Matching-Funktion (shared fuer beide Systeme) ──
        def _match_skill(skill_name: str) -> tuple[dict | None, float]:
            """Findet den besten Match fuer einen Job-Skill.
            Returns: (matched_skill_dict, base_score)
            """
            name = skill_name.lower().strip()

            # 1. Exakter Match
            matched = cand_skill_map.get(name)
            if matched:
                return matched, 1.0

            # 2. Synonym-Match
            normalized_job = self._normalize_skill(name)
            matched = cand_skill_map.get(normalized_job)
            if matched:
                return matched, 0.9

            # 3. Kern-Skill Match (Sprach-Normalisierung)
            core = self._extract_core_skill(name)
            if core != name and core != normalized_job:
                matched = cand_skill_map.get(core)
                if matched:
                    return matched, 0.85

            # 4. Contains-Match: "HGB-Abschlusserstellung" → suche "hgb" im Kandidaten
            #    Nur fuer bekannte Fachbegriffe, nicht fuer alles!
            #    Splitting: "HGB-Abschlusserstellung" → ["hgb", "abschlusserstellung"]
            parts = re.split(r"[-/\s]+", name)
            if len(parts) >= 2:
                for part in parts:
                    if len(part) >= 3:  # Mindestens 3 Zeichen
                        part_norm = self._normalize_skill(part)
                        matched = cand_skill_map.get(part_norm)
                        if matched:
                            return matched, 0.7
                        matched = cand_skill_map.get(part)
                        if matched:
                            return matched, 0.7

            return None, 0.0

        def _apply_modifiers(matched_skill: dict, base_score: float) -> float:
            """Wendet Recency- und Proficiency-Modifier an."""
            score = base_score

            # Recency-Abschlag
            recency = matched_skill.get("recency", "aktuell")
            if recency == "kuerzlich":
                score *= 0.75  # gelockert von 0.7 — noch relativ aktuell
            elif recency == "veraltet":
                score *= 0.4   # gelockert von 0.3 — alt aber nicht komplett irrelevant

            # Proficiency-Bonus
            prof = matched_skill.get("proficiency", "grundlagen")
            if prof == "experte":
                score = min(1.0, score * 1.1)

            return score

        # ── Scoring-System waehlen ──
        use_weighted = (
            job_role is not None
            and self._skill_weights is not None
            and job_role in self._skill_weights
        )

        if use_weighted:
            # ── GEWICHTETES SYSTEM: Kategorie-Weights aus skill_weights.json ──
            weighted_score_sum = 0.0
            weighted_max_sum = 0.0

            for js in filtered_job_skills:
                skill_name = js.get("skill", "").lower().strip()
                if not skill_name:
                    continue

                # Bestimme Gewicht fuer diesen Skill (auch ueber Synonym/Contains)
                skill_weight = self._get_skill_weight(job_role, skill_name)
                if skill_weight is None:
                    # Versuche auch Synonym-normalisierten Namen
                    norm_name = self._normalize_skill(skill_name)
                    skill_weight = self._get_skill_weight(job_role, norm_name)
                if skill_weight is None:
                    # Versuche Teile: "HGB-Abschlusserstellung" → "HGB"
                    parts = re.split(r"[-/\s]+", skill_name)
                    for part in parts:
                        if len(part) >= 3:
                            w = self._get_skill_weight(job_role, part)
                            if w is not None:
                                skill_weight = w
                                break
                if skill_weight is None:
                    skill_weight = 5  # Fallback

                weighted_max_sum += skill_weight

                matched_skill, base_score = _match_skill(skill_name)
                if matched_skill and base_score > 0:
                    final_score = _apply_modifiers(matched_skill, base_score)
                    weighted_score_sum += final_score * skill_weight

            if weighted_max_sum == 0:
                return 0.0
            return weighted_score_sum / weighted_max_sum

        else:
            # ── FALLBACK: Altes essential/preferred System ──
            essential_total = 0
            essential_matched = 0.0
            preferred_total = 0
            preferred_matched = 0.0

            for js in filtered_job_skills:
                skill_name = js.get("skill", "").lower().strip()
                importance = js.get("importance", "preferred")

                if importance == "essential":
                    essential_total += 1
                else:
                    preferred_total += 1

                matched_skill, base_score = _match_skill(skill_name)
                if matched_skill and base_score > 0:
                    final_score = _apply_modifiers(matched_skill, base_score)
                    if importance == "essential":
                        essential_matched += final_score
                    else:
                        preferred_matched += final_score

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
            return 0.80, "leicht_ueberqualifiziert"  # gelockert von 0.7 — leicht ueberqualifiziert ist gut matchbar
        elif gap == -1:
            return 0.80, "leicht_unterqualifiziert"  # gelockert von 0.75 — kann in Rolle reinwachsen
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
                if not isinstance(s, dict):
                    continue
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

        # Skill-Weights + Skill-Hierarchie laden (einmalig) + Job-Rolle erkennen
        self._load_skill_weights()
        self._load_skill_hierarchy()
        job_role = self._detect_job_role(
            getattr(job, "hotlist_job_title", None),
            job.position,
            getattr(job, "classification_data", None),
        )

        # Rollen-Check: Welche Qualifikation braucht der Job?
        # V2.7: Nutzt job_role von _detect_job_role (gleiche Logik, keine Doppelpflege)
        job_requires_bibu = job_role == "bilanzbuchhalter"
        job_requires_fibu = job_role == "finanzbuchhalter"
        job_requires_lohn = job_role == "lohnbuchhalter"
        job_requires_stfa = job_role == "steuerfachangestellte"
        job_requires_kredibu = job_role == "kreditorenbuchhalter"
        job_requires_debibu = job_role == "debitorenbuchhalter"

        # Gewichte normalisieren (Summe = 100)
        total_weight = sum(weights.values())
        if total_weight == 0:
            total_weight = 100

        scored = []
        for cand in candidates:
            # ── 7 Score-Dimensionen (alle 0.0 - 1.0) ──

            skill_score = self._score_skill_overlap(
                cand.structured_skills, job_skills, job_role=job_role,
                candidate_certifications=cand.certifications,
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
                skill_score * weights.get("skill_overlap", 15) +
                seniority_score * weights.get("seniority_fit", 25) +
                job_title_score * weights.get("job_title_fit", 0) +
                embedding_score * weights.get("embedding_sim", 21) +
                industry_score * weights.get("industry_fit", 12) +
                career_score * weights.get("career_fit", 12) +
                software_score * weights.get("software_match", 15)
            ) / total_weight * 100

            # ── Minimum Skill Threshold (Anti-False-Positive) ──
            # Wenn Skill-Overlap < 0.20 → Score cap bei 60
            # Simulation: THRESHOLD_020_60 eliminiert alle False Positives (Q=933 vs 920)
            skill_capped = False
            if skill_score < 0.20:
                total = min(total, 60)
                skill_capped = True

            # ── Qualifikations-Multiplikator (NACH Gewichtung, VOR Speichern) ──
            # v2.8: Symmetrische Multiplier fuer ALLE 6 Rollen (Bonus + Penalty)
            role_multiplier = 1.0
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
                    role_multiplier = 1.20  # +20% Bonus (erhoeht von 1.15)
                else:
                    role_multiplier = 0.6   # -40% Penalty
                total *= role_multiplier

            elif job_requires_fibu:
                # FiBu-Multiplikator: Symmetrisch — Bonus UND Penalty
                candidate_has_fibu = False
                # Check 1: Zertifizierungen
                if cand.certifications:
                    candidate_has_fibu = any(
                        any(kw in c.lower() for kw in ["finanzbuchhalter", "buchhalter ihk", "steuerfachangestellte"])
                        for c in cand.certifications
                    )
                # Check 2: structured_skills
                if not candidate_has_fibu and cand.structured_skills:
                    candidate_has_fibu = any(
                        any(kw in s.get("skill", "").lower() for kw in ["finanzbuchhalter", "finanzbuchhaltung", "buchhalter"])
                        and s.get("category", "") in ("zertifizierung", "qualifikation", "")
                        for s in cand.structured_skills
                    )
                # Check 3: Job-Titel / Positionen
                if not candidate_has_fibu and cand.job_titles:
                    candidate_has_fibu = any(
                        any(kw in t.lower() for kw in ["finanzbuchhalter", "finanzbuchhaltung", "buchhalter"])
                        for t in cand.job_titles
                    )
                if candidate_has_fibu:
                    role_multiplier = 1.20  # +20% Bonus fuer passende FiBu-Qualifikation (erhoeht von 1.15)
                else:
                    role_multiplier = 0.7   # -30% Penalty fuer Nicht-FiBu auf FiBu-Job
                total *= role_multiplier

            elif job_requires_stfa:
                # StFA-Multiplikator: NUR Bonus, KEIN Penalty
                # Grund: Qualifikations-Erkennung fuer StFA ist unzuverlaessig —
                # 66% der Kandidaten bekamen Penalty, was StFA-Scores zerstoert hat
                candidate_has_stfa = False
                if cand.certifications:
                    candidate_has_stfa = any(
                        any(kw in c.lower() for kw in [
                            "steuerfachangestellte", "steuerfachangestellter", "steuerfachwirt",
                            "steuerberater", "steuerlehre", "steuerfachschule"
                        ])
                        for c in cand.certifications
                    )
                if not candidate_has_stfa and cand.structured_skills:
                    candidate_has_stfa = any(
                        any(kw in s.get("skill", "").lower() for kw in [
                            "steuerfachangestellte", "steuerfachangestellter", "steuerfachwirt",
                            "steuerberater", "steuerrecht", "steuerkanzlei"
                        ])
                        for s in cand.structured_skills
                    )
                if candidate_has_stfa:
                    role_multiplier = 1.15  # +15% Bonus fuer StFA-Qualifikation
                    total *= role_multiplier
                # Kein else/penalty — zu viele False Negatives bei Qualifikations-Erkennung

            elif job_requires_lohn:
                # Lohn-Multiplikator: NUR Bonus, KEIN Penalty
                # Grund: Lohn-Keywords in Profildaten sind oft unvollstaendig
                candidate_has_lohn = False
                if cand.certifications:
                    candidate_has_lohn = any(
                        any(kw in c.lower() for kw in ["lohnbuchhalter", "entgeltabrechner", "payroll"])
                        for c in cand.certifications
                    )
                if not candidate_has_lohn and cand.structured_skills:
                    candidate_has_lohn = any(
                        any(kw in s.get("skill", "").lower() for kw in [
                            "lohnbuchhaltung", "lohnabrechnung", "gehaltsabrechnung",
                            "entgeltabrechnung", "payroll", "lohn- und gehaltsabrechnung"
                        ])
                        for s in cand.structured_skills
                    )
                if not candidate_has_lohn and cand.job_titles:
                    candidate_has_lohn = any(
                        any(kw in t.lower() for kw in ["lohnbuchhalter", "payroll", "entgelt", "gehaltsabrechnung"])
                        for t in cand.job_titles
                    )
                if candidate_has_lohn:
                    role_multiplier = 1.20  # +20% Bonus (Lohn ist spezialisiert)
                    total *= role_multiplier
                # Kein else/penalty — zu wenig Lohn-Matches, Penalty wuerde alles zerstoeren

            elif job_requires_kredibu:
                # KrediBu-Multiplikator: NUR Bonus, KEIN Penalty
                candidate_has_kredi = False
                if cand.structured_skills:
                    candidate_has_kredi = any(
                        any(kw in s.get("skill", "").lower() for kw in [
                            "kreditorenbuchhaltung", "kreditoren", "accounts payable",
                            "eingangsrechnungen", "rechnungsprüfung", "rechnungspruefung"
                        ])
                        for s in cand.structured_skills
                    )
                if not candidate_has_kredi and cand.job_titles:
                    candidate_has_kredi = any(
                        any(kw in t.lower() for kw in ["kreditorenbuchhalter", "kreditoren", "accounts payable"])
                        for t in cand.job_titles
                    )
                if candidate_has_kredi:
                    role_multiplier = 1.15  # +15% Bonus fuer Kreditoren-Erfahrung
                    total *= role_multiplier
                # Kein else/penalty — Skill-Weights differenzieren bereits

            elif job_requires_debibu:
                # DebiBu-Multiplikator: NUR Bonus, KEIN Penalty
                candidate_has_debi = False
                if cand.structured_skills:
                    candidate_has_debi = any(
                        any(kw in s.get("skill", "").lower() for kw in [
                            "debitorenbuchhaltung", "debitoren", "accounts receivable",
                            "mahnwesen", "forderungsmanagement", "fakturierung"
                        ])
                        for s in cand.structured_skills
                    )
                if not candidate_has_debi and cand.job_titles:
                    candidate_has_debi = any(
                        any(kw in t.lower() for kw in ["debitorenbuchhalter", "debitoren", "accounts receivable"])
                        for t in cand.job_titles
                    )
                if candidate_has_debi:
                    role_multiplier = 1.15  # +15% Bonus fuer Debitoren-Erfahrung
                    total *= role_multiplier
                # Kein else/penalty — Skill-Weights differenzieren bereits

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
                # Phase 10: Google Maps Fahrzeit
                "drive_time_car_min": cand.drive_time_car_min,
                "drive_time_transit_min": cand.drive_time_transit_min,
                # v2.5 Tags
                "qualification_tag": qualification_tag,
                "candidate_level": cand.seniority_level,
                "job_level": job_level,
                "role_multiplier": role_multiplier if role_multiplier != 1.0 else None,
                "skill_capped": skill_capped or None,
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

        # Score-Minimum: Matches unter MIN_SCORE nicht speichern
        scored = [s for s in scored if s.total_score >= self.MIN_SCORE]

        # Rang zuweisen
        for i, m in enumerate(scored):
            m.rank = i + 1

        return scored[:self.TOP_N]

    # ═══════════════════════════════════════════════════════════════
    # V3 SCORING: Qualification-First Multi-Gate Scoring
    # ═══════════════════════════════════════════════════════════════

    # Rollen-Kompatibilitaets-Matrix (geladen aus Config oder inline)
    _ROLE_COMPATIBILITY: dict[str, list[str]] = {
        "bilanzbuchhalter": ["bilanzbuchhalter", "finanzbuchhalter", "kreditorenbuchhalter", "debitorenbuchhalter", "steuerfachangestellte"],
        "finanzbuchhalter": ["finanzbuchhalter", "kreditorenbuchhalter", "debitorenbuchhalter"],
        "kreditorenbuchhalter": ["kreditorenbuchhalter"],
        "debitorenbuchhalter": ["debitorenbuchhalter"],
        "lohnbuchhalter": ["lohnbuchhalter"],
        "steuerfachangestellte": ["steuerfachangestellte", "finanzbuchhalter", "kreditorenbuchhalter", "debitorenbuchhalter"],
    }

    # Mapping: primary_role (GPT-Format) → interner Key
    _ROLE_KEY_MAP: dict[str, str] = {
        "Bilanzbuchhalter/in": "bilanzbuchhalter",
        "Finanzbuchhalter/in": "finanzbuchhalter",
        "Kreditorenbuchhalter/in": "kreditorenbuchhalter",
        "Debitorenbuchhalter/in": "debitorenbuchhalter",
        "Lohnbuchhalter/in": "lohnbuchhalter",
        "Steuerfachangestellte/r": "steuerfachangestellte",
    }

    def _get_candidate_role_key(self, cand: MatchCandidate) -> str | None:
        """Ermittelt die interne Rolle des Kandidaten (z.B. 'finanzbuchhalter')."""
        # Primaer: classification_data.primary_role
        cd = cand.classification_data or {}
        pr = cd.get("primary_role") or cand.primary_role
        if pr and pr in self._ROLE_KEY_MAP:
            return self._ROLE_KEY_MAP[pr]
        # Fallback: Erste Rolle aus classification_data.roles
        for role in cd.get("roles", []):
            if role in self._ROLE_KEY_MAP:
                return self._ROLE_KEY_MAP[role]
        # Fallback: hotlist_job_titles
        for title in (cand.job_titles or []):
            t = title.lower()
            if "bilanzbuchhalter" in t:
                return "bilanzbuchhalter"
            if "lohnbuchhalter" in t or "payroll" in t:
                return "lohnbuchhalter"
            if "steuerfachangestellte" in t:
                return "steuerfachangestellte"
            if "kreditoren" in t and "debitoren" not in t:
                return "kreditorenbuchhalter"
            if "debitoren" in t and "kreditoren" not in t:
                return "debitorenbuchhalter"
            if "finanzbuchhalter" in t or "buchhalter" in t:
                return "finanzbuchhalter"
        return None

    def _check_role_compatibility(self, candidate_role: str | None, job_role: str | None) -> bool:
        """Prueft ob die Kandidaten-Rolle mit der Job-Rolle kompatibel ist."""
        if not job_role or not candidate_role:
            return True  # Wenn Rolle unbekannt, kein Gate (um Datenluecken nicht zu bestrafen)
        allowed = self._ROLE_COMPATIBILITY.get(candidate_role, [])
        return job_role in allowed

    def _score_role_depth(self, candidate_role: str | None, job_role: str | None) -> int:
        """Layer 1A: Wie tief passt die Rolle? (0-15 Punkte)"""
        if not candidate_role or not job_role:
            return 8  # Keine Daten → neutral
        if candidate_role == job_role:
            return 15  # Exakte Rolle
        # BiBu auf alles andere (ueberqualifiziert aber passend)
        if candidate_role == "bilanzbuchhalter":
            return 12
        # StFA auf FiBu/KrediBu/DebiBu (breite Ausbildung)
        if candidate_role == "steuerfachangestellte" and job_role in ("finanzbuchhalter", "kreditorenbuchhalter", "debitorenbuchhalter"):
            return 10
        # FiBu auf KrediBu/DebiBu (Generalist auf Spezialisierung)
        if candidate_role == "finanzbuchhalter" and job_role in ("kreditorenbuchhalter", "debitorenbuchhalter"):
            return 8
        return 5  # Sonstige erlaubte Kombination

    def _score_skill_depth_v3(self, cand_skills: list[dict], job_skills: list[dict],
                               job_role: str | None, cand_certifications: list[str]) -> tuple[int, int]:
        """Layer 1B: Skill-Tiefe (0-20 Punkte) + Anzahl fachkenntnisse-Matches fuer Gate 2.

        Returns:
            (skill_points, fachkenntnisse_match_count)
        """
        if not job_skills:
            return 10, 1  # Keine Job-Skills → neutral

        # Kandidaten-Skills normalisieren
        cand_skill_names = set()
        for s in (cand_skills or []):
            if not isinstance(s, dict):
                continue
            name = self._normalize_skill(s.get("skill", "").lower().strip())
            if name and not self._is_irrelevant_skill(name, s.get("category", "")):
                cand_skill_names.add(name)
        # Zertifizierungen als Skills hinzufuegen
        for cert in (cand_certifications or []):
            cand_skill_names.add(cert.lower().strip())
        # ERP-Skills aus structured_skills
        for s in (cand_skills or []):
            if not isinstance(s, dict):
                continue
            if s.get("category") in ("software", "erp", "tool"):
                cand_skill_names.add(self._normalize_skill(s.get("skill", "").lower().strip()))

        points = 0.0
        fachkenntnisse_matches = 0

        for js in job_skills:
            if not isinstance(js, dict):
                continue
            js_name = self._normalize_skill(js.get("skill", "").lower().strip())
            if not js_name or self._is_irrelevant_skill(js_name, js.get("category", "")):
                continue

            # Skill-Gewicht aus Config
            weight = self._get_skill_weight(js_name, job_role) if job_role else 5
            is_fachkenntnis = weight >= 9  # fachkenntnisse haben Gewicht 9-10
            is_taetigkeit = 7 <= weight < 9
            is_software = weight <= 4

            if is_software:
                continue  # Software wird in Layer 2 bewertet

            # Match-Suche
            match_score = 0.0
            matched = False

            # Exact match
            if js_name in cand_skill_names:
                match_score = 2.0 if is_fachkenntnis else 1.5
                matched = True
            else:
                # Synonym match
                for cs in cand_skill_names:
                    core_js = self._extract_core_skill(js_name)
                    core_cs = self._extract_core_skill(cs)
                    if core_js and core_cs and core_js == core_cs:
                        match_score = 1.5 if is_fachkenntnis else 1.0
                        matched = True
                        break
                    # Contains match
                    if len(js_name) > 3 and len(cs) > 3:
                        if js_name in cs or cs in js_name:
                            match_score = 0.8 if is_fachkenntnis else 0.5
                            matched = True
                            break

            if matched:
                points += match_score
                if is_fachkenntnis:
                    fachkenntnisse_matches += 1

        return min(20, int(round(points))), fachkenntnisse_matches

    def _score_certification_match_v3(self, cand: MatchCandidate, job_role: str | None) -> int:
        """Layer 1C: Zertifizierungs-Match (0-10 Punkte)"""
        if not job_role:
            return 5  # Neutral

        certs_lower = [c.lower() for c in (cand.certifications or [])]
        skills_text = " ".join(s.get("skill", "").lower() for s in (cand.structured_skills or []))
        titles_lower = [t.lower() for t in (cand.job_titles or [])]

        def has_keyword(keywords: list[str], sources: list[str] = None) -> bool:
            all_text = " ".join(certs_lower) + " " + skills_text + " " + " ".join(titles_lower)
            if sources:
                all_text = " ".join(sources)
            return any(kw in all_text for kw in keywords)

        if job_role == "bilanzbuchhalter":
            if has_keyword(["bilanzbuchhalter", "bilanzbuchhalterin"], certs_lower):
                return 10
            if has_keyword(["steuerfachangestellte", "steuerfachwirt"]) and has_keyword(["jahresabschluss", "abschluss"]):
                return 7
            if has_keyword(["finanzbuchhalter", "buchhalterin"]):
                return 3
            return 0

        if job_role == "finanzbuchhalter":
            if has_keyword(["finanzbuchhalter", "finanzbuchhalterin", "bilanzbuchhalter"], certs_lower):
                return 10
            if has_keyword(["steuerfachangestellte", "steuerfachwirt"]):
                return 8
            if has_keyword(["buchhalter", "industriekaufmann", "industriekauffrau", "bürokaufmann", "bürokauffrau"]):
                return 5
            return 0

        if job_role == "lohnbuchhalter":
            if has_keyword(["lohnbuchhalter", "lohnbuchhalterin", "entgeltabrechner", "payroll"]):
                return 10
            if has_keyword(["steuerfachangestellte"]) and has_keyword(["lohn", "gehalt", "payroll"]):
                return 6
            return 0

        if job_role == "steuerfachangestellte":
            if has_keyword(["steuerfachangestellte", "steuerfachangestellter", "steuerfachwirt", "steuerberater"]):
                return 10
            if has_keyword(["finanzbuchhalter"]) and has_keyword(["steuer", "umsatzsteuer"]):
                return 4
            return 0

        if job_role == "kreditorenbuchhalter":
            if has_keyword(["finanzbuchhalter", "bilanzbuchhalter", "steuerfachangestellte"]):
                return 8
            if has_keyword(["buchhalter", "industriekaufmann"]):
                return 5
            if has_keyword(["kreditorenbuchhal", "accounts payable"]):
                return 10
            return 2

        if job_role == "debitorenbuchhalter":
            if has_keyword(["finanzbuchhalter", "bilanzbuchhalter", "steuerfachangestellte"]):
                return 8
            if has_keyword(["buchhalter", "industriekaufmann"]):
                return 5
            if has_keyword(["debitorenbuchhal", "accounts receivable"]):
                return 10
            return 2

        return 5  # Unbekannte Rolle → neutral

    # ── V3 Seniority Fit (0-12) ──

    def _score_seniority_v3(self, cand_level: int, job_level: int) -> tuple[int, str]:
        """Layer 2A: Seniority-Fit (0-12 Punkte)"""
        gap = cand_level - job_level
        if gap == 0:
            return 12, "passt"
        if gap == 1:
            return 9, "leicht_ueberqualifiziert"
        if gap == -1:
            return 9, "leicht_unterqualifiziert"
        if gap == 2:
            return 3, "ueberqualifiziert"
        if gap == -2:
            return 3, "unterqualifiziert"
        if gap >= 3:
            return 0, "stark_ueberqualifiziert"
        return 0, "stark_unterqualifiziert"

    # ── V3 Software Match (0-10) ──

    def _score_software_v3(self, cand_skills: list[dict], job_skills: list[dict], cand_erp: list[str]) -> int:
        """Layer 2B: Software-Ecosystem (0-10 Punkte)"""
        raw = self._score_software_match(cand_skills, job_skills, cand_erp)
        # raw ist 0.0-1.0, skalieren auf 0-10
        if raw >= 0.95:
            return 10
        if raw >= 0.45:
            return 6  # Job hat kein Requirement oder Kandidat hat beides
        if raw >= 0.25:
            return 3  # Kandidat hat kein ERP
        return 1  # Cross-Ecosystem

    # ── V3 Main Scoring ──

    async def _score_candidates_v3(
        self,
        job: Job,
        candidates: list[MatchCandidate],
    ) -> list[ScoredMatch]:
        """V3 Scoring: Qualification-First Multi-Gate Scoring.

        Layer 0: Hard Gates (Pass/Fail)
        Layer 1: Qualifikations-Score (0-45)
        Layer 2: Kompatibilitaets-Score (0-40)
        Layer 3: Kontext-Score (0-15)
        Total: 0-100, Minimum 35 fuer Speicherung
        """
        job_level = job.v2_seniority_level or 2
        job_skills = job.v2_required_skills or []
        job_embedding = job.v2_embedding
        job_industry = job.industry

        # Job-Rolle erkennen
        self._load_skill_weights()
        self._load_skill_hierarchy()
        job_role = self._detect_job_role(
            getattr(job, "hotlist_job_title", None),
            job.position,
            getattr(job, "classification_data", None),
        )

        # Quality Gate: Jobs mit quality_score="low" → REJECT
        job_cd = getattr(job, "classification_data", None) or {}
        quality_score = job_cd.get("quality_score")
        quality_cap = 100
        if quality_score == "low":
            logger.info(f"V3 Gate: Job {job.id} quality_score=low → REJECTED")
            return []
        if quality_score == "medium":
            quality_cap = 75

        # Job is_leadership pruefen
        job_is_leadership = job_cd.get("is_leadership", False)

        # Expanded Job-Skills (Hierarchie)
        expanded_job_skills = self._expand_job_skills_with_hierarchy(job_skills, job_role)

        scored = []
        gate_rejected = 0

        for cand in candidates:
            # ═══ LAYER 0: HARD GATES ═══
            reject_reason = None

            # Gate 1: Rollen-Kompatibilitaet
            cand_role = self._get_candidate_role_key(cand)
            if not self._check_role_compatibility(cand_role, job_role):
                reject_reason = f"role_incompatible:{cand_role}→{job_role}"

            # Gate 2: Minimum-Skill (mindestens 1 fachkenntnisse-Match)
            if not reject_reason:
                _, fk_matches = self._score_skill_depth_v3(
                    cand.structured_skills, expanded_job_skills, job_role, cand.certifications
                )
                if fk_matches == 0:
                    reject_reason = "zero_fachkenntnisse"

            # Gate 5: Leadership-Filter
            if not reject_reason:
                if not job_is_leadership and cand.seniority_level >= 6:
                    reject_reason = "executive_on_ic_job"
                if job_is_leadership and cand.seniority_level <= 2:
                    reject_reason = "junior_on_leadership_job"

            if reject_reason:
                gate_rejected += 1
                continue

            # ═══ LAYER 1: QUALIFIKATIONS-SCORE (0-45) ═══

            # 1A: Rollen-Tiefe (0-15)
            role_depth = self._score_role_depth(cand_role, job_role)

            # 1B: Skill-Tiefe (0-20)
            skill_depth, _ = self._score_skill_depth_v3(
                cand.structured_skills, expanded_job_skills, job_role, cand.certifications
            )

            # 1C: Zertifizierungs-Match (0-10)
            cert_match = self._score_certification_match_v3(cand, job_role)

            layer1 = role_depth + skill_depth + cert_match  # 0-45

            # Layer 1 Minimum: Wenn < 15 → REJECT
            if layer1 < 15:
                gate_rejected += 1
                continue

            # ═══ LAYER 2: KOMPATIBILITAETS-SCORE (0-40) ═══

            # 2A: Seniority-Fit (0-12)
            seniority_pts, qualification_tag = self._score_seniority_v3(
                cand.seniority_level, job_level
            )

            # 2B: Software-Ecosystem (0-10)
            software_pts = self._score_software_v3(
                cand.structured_skills, job_skills, cand.erp
            )

            # 2C: Embedding-Similarity (0-8)
            emb_raw = self._score_embedding_similarity(
                cand.embedding_current, job_embedding
            )
            embedding_pts = min(8, int(round(emb_raw * 8)))

            # 2D: Career-Fit (0-10)
            career_raw, career_note = self._score_career_fit(
                cand.career_trajectory, cand.seniority_level, job_level
            )
            career_pts = min(10, int(round(career_raw * 10)))

            layer2 = seniority_pts + software_pts + embedding_pts + career_pts  # 0-40

            # ═══ LAYER 3: KONTEXT-SCORE (0-15) ═══

            # 3A: Branchen-Fit (0-5)
            ind_raw = self._score_industry_fit(cand.industries, job_industry)
            if ind_raw >= 0.9:
                industry_pts = 5
            elif ind_raw >= 0.5:
                industry_pts = 3
            elif ind_raw >= 0.25:
                industry_pts = 2
            else:
                industry_pts = 1

            # 3B: Recency (0-5) — basiert auf career_trajectory
            trajectory = (cand.career_trajectory or "").lower()
            if trajectory in ("aufsteigend", "lateral"):
                recency_pts = 5  # Aktiv in Karriere
            elif trajectory == "einstieg":
                recency_pts = 4  # Neueinsteiger
            elif trajectory == "absteigend":
                recency_pts = 3  # Bewusster Downshift
            else:
                recency_pts = 3  # Unbekannt

            # 3C: Standort-Qualitaet (0-5)
            if cand.distance_km is not None:
                if cand.distance_km <= 15:
                    location_pts = 5
                elif cand.distance_km <= 25:
                    location_pts = 4
                elif cand.distance_km <= 30:
                    location_pts = 3
                else:
                    location_pts = 1
            else:
                location_pts = 2  # Remote oder keine Daten

            layer3 = industry_pts + recency_pts + location_pts  # 0-15

            # ═══ GESAMT ═══

            total = float(layer1 + layer2 + layer3)

            # Quality Cap (medium jobs → max 75)
            total = min(total, quality_cap)

            # Empty CV Penalty (behalten — schuetzt vor leeren Profilen)
            empty_cv_penalty = None
            role_summary = (cand.current_role_summary or "").lower()
            if "keine berufserfahrung" in role_summary:
                empty_cv_penalty = 0.1
                total *= 0.1
            elif "keine ausbildung" in role_summary:
                empty_cv_penalty = 0.2
                total *= 0.2
            elif len(cand.structured_skills) < 3:
                empty_cv_penalty = 0.3
                total *= 0.3

            total = min(100, max(0, total))

            # Score-Breakdown (V3 Format)
            breakdown = {
                # V3 Layer Scores
                "v3_layer1_qualification": layer1,
                "v3_layer2_compatibility": layer2,
                "v3_layer3_context": layer3,
                # V3 Detail
                "v3_role_depth": role_depth,
                "v3_skill_depth": skill_depth,
                "v3_cert_match": cert_match,
                "v3_seniority_pts": seniority_pts,
                "v3_software_pts": software_pts,
                "v3_embedding_pts": embedding_pts,
                "v3_career_pts": career_pts,
                "v3_industry_pts": industry_pts,
                "v3_recency_pts": recency_pts,
                "v3_location_pts": location_pts,
                # Kompatibilitaet mit altem Format
                "skill_overlap": round(skill_depth / 20, 3),  # Normalisiert 0-1 fuer Templates
                "seniority_fit": round(seniority_pts / 12, 3),
                "embedding_sim": round(emb_raw, 3),
                "industry_fit": round(ind_raw, 3),
                "career_fit": round(career_raw, 3),
                "software_match": round(software_pts / 10, 3),
                "job_title_fit": 0.0,  # Deaktiviert
                # Metadaten
                "distance_km": cand.distance_km,
                "drive_time_car_min": cand.drive_time_car_min,
                "drive_time_transit_min": cand.drive_time_transit_min,
                "qualification_tag": qualification_tag,
                "candidate_level": cand.seniority_level,
                "job_level": job_level,
                "job_role": job_role,
                "candidate_role": cand_role,
                "empty_cv_penalty": empty_cv_penalty,
                "scoring_version": "v3",
            }
            if career_note:
                breakdown["career_note"] = career_note

            scored.append(ScoredMatch(
                candidate_id=cand.id,
                total_score=round(total, 2),
                breakdown=breakdown,
            ))

        logger.info(
            f"V3 Scoring: {len(scored)} scored, {gate_rejected} gate-rejected "
            f"(Rolle: {job_role}, Level: {job_level})"
        )

        # Sortieren + Minimum + Top-N
        scored.sort(key=lambda x: x.total_score, reverse=True)
        scored = [s for s in scored if s.total_score >= 35]  # V3 Minimum: 35

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

        # ── Quality Gate: Jobs mit quality_score="low" werden NICHT gematcht ──
        if getattr(job, "quality_score", None) == "low":
            logger.info(
                f"Quality Gate: Job '{job.position}' bei '{job.company_name}' "
                f"uebersprungen (quality_score=low)"
            )
            return MatchResult(
                job_id=job_id,
                matches=[],
                total_candidates_checked=0,
                candidates_after_filter=0,
                duration_ms=round((time.perf_counter() - start) * 1000, 1),
                scoring_weights={},
            )

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

        # ── Schicht 2: V3 Qualification-First Multi-Gate Scoring ──
        scored = await self._score_candidates_v3(job, candidates)

        # Build Lookup fuer Schicht 3
        cand_map = {c.id: c for c in candidates}

        # ── Schicht 3: Pattern Boost ──
        scored = await self._apply_learned_rules(scored, job, cand_map)

        # ── Phase 10: Google Maps Fahrzeit (NUR für Score ≥ Threshold) ──
        from app.api.routes_settings import get_drive_time_threshold
        DRIVE_TIME_SCORE_THRESHOLD = await get_drive_time_threshold(self.db)
        try:
            from app.services.distance_matrix_service import distance_matrix_service

            if distance_matrix_service.has_api_key and job.location_coords is not None:
                # Job-Koordinaten extrahieren
                from sqlalchemy import func as sa_func
                job_lat = None
                job_lng = None
                if hasattr(job, "location_coords") and job.location_coords is not None:
                    coord_result = await self.db.execute(
                        select(
                            sa_func.ST_Y(sa_func.ST_GeomFromWKB(job.location_coords)).label("lat"),
                            sa_func.ST_X(sa_func.ST_GeomFromWKB(job.location_coords)).label("lng"),
                        )
                    )
                    coord_row = coord_result.first()
                    if coord_row:
                        job_lat = coord_row[0]
                        job_lng = coord_row[1]

                if job_lat and job_lng:
                    # Nur Top-Matches (Score ≥ 70) MIT Koordinaten
                    top_candidate_ids = {
                        sm.candidate_id for sm in scored
                        if sm.total_score >= DRIVE_TIME_SCORE_THRESHOLD
                    }
                    cands_with_coords = [
                        {
                            "candidate_id": str(c.id),
                            "lat": c._lat,
                            "lng": c._lng,
                            "plz": c.postal_code,
                        }
                        for c in candidates
                        if c.id in top_candidate_ids and c._lat is not None and c._lng is not None
                    ]

                    if cands_with_coords:
                        drive_times = await distance_matrix_service.batch_drive_times(
                            job_lat=job_lat,
                            job_lng=job_lng,
                            job_plz=job.postal_code,
                            candidates=cands_with_coords,
                        )

                        # Fahrzeit in die Breakdowns der ScoredMatches schreiben
                        for sm in scored:
                            result = drive_times.get(str(sm.candidate_id))
                            if result and sm.breakdown:
                                sm.breakdown["drive_time_car_min"] = result.car_min
                                sm.breakdown["drive_time_transit_min"] = result.transit_min

                        logger.info(
                            f"Google Maps Fahrzeit: {len(drive_times)} Ergebnisse "
                            f"für {len(cands_with_coords)} Top-Kandidaten "
                            f"(Score ≥ {DRIVE_TIME_SCORE_THRESHOLD}, "
                            f"von {len(scored)} Matches gesamt)"
                        )
        except Exception as e:
            logger.warning(f"Google Maps Fahrzeit-Fehler (nicht kritisch): {e}")

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

            # Distanz + Fahrzeit aus Breakdown extrahieren
            dist = sm.breakdown.get("distance_km") if sm.breakdown else None
            car_min = sm.breakdown.get("drive_time_car_min") if sm.breakdown else None
            transit_min = sm.breakdown.get("drive_time_transit_min") if sm.breakdown else None

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
                    # Fahrzeit NUR überschreiben wenn neue Daten vorhanden
                    # → Schützt bestehende Werte bei Re-Matching ohne Google Maps
                    if car_min is not None:
                        match_obj.drive_time_car_min = car_min
                    if transit_min is not None:
                        match_obj.drive_time_transit_min = transit_min
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
                    drive_time_car_min=car_min,
                    drive_time_transit_min=transit_min,
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
