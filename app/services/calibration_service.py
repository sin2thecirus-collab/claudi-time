"""Calibration Service — Lernt aus DeepMatch-Ergebnissen.

Analysiert alle AI-bewerteten Matches und leitet Korrekturfaktoren ab:

1. Rollen-Kalibrierung:
   → Gruppiere nach (Job-Rolle, Kandidat-Rolle)
   → Berechne Durchschnitt AI-Score pro Paar
   → Vergleiche mit aktuellem Matrix-Wert
   → Wenn Abweichung > 0.1: Anpassen

2. Power-Keywords:
   → Fuer gute Matches (ai_score > 0.7): Welche Keywords kommen haeufig vor?
   → Fuer schlechte Matches (ai_score < 0.3): Welche fehlen?
   → Power-Keywords zaehlen doppelt im Keyword-Score

3. Ausschluss-Muster:
   → Wenn (job_role, cand_role) IMMER ai_score < 0.2 ergibt
   → Diese Kombination wird im Pre-Match ausgeschlossen

4. Staerken/Schwaechen-Muster (NEU):
   → Analysiert ai_strengths + ai_weaknesses Texte
   → Erkennt wiederkehrende Muster in guten/schlechten Matches
   → Identifiziert "Must-Have" Skills (fehlen = schlecht)
   → Identifiziert "Deal-Breaker" (kommen = schlecht)
   → Pro Rollen-Paar: Was macht den Unterschied?

Die Ergebnisse werden als JSON in einer DB-Tabelle gespeichert
und vom Pre-Scoring-Service beim naechsten Lauf geladen.
"""

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

from sqlalchemy import and_, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.candidate import Candidate
from app.models.job import Job
from app.models.match import Match

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# DATENKLASSEN
# ═══════════════════════════════════════════════════════════════


@dataclass
class RolePairStats:
    """Statistiken fuer ein Rollen-Paar aus der AI-Bewertung."""

    job_role: str
    candidate_role: str
    sample_count: int
    avg_ai_score: float
    min_ai_score: float
    max_ai_score: float
    current_matrix_value: float | None  # Aktueller Wert in der Similarity-Matrix
    suggested_value: float  # Vorgeschlagener neuer Wert
    deviation: float  # Abweichung: suggested - current


@dataclass
class PowerKeyword:
    """Ein Keyword, das stark mit guten/schlechten Matches korreliert."""

    keyword: str
    good_match_count: int  # Wie oft in Matches mit ai_score > 0.7
    bad_match_count: int  # Wie oft in Matches mit ai_score < 0.3
    total_count: int  # Insgesamt in allen AI-Matches
    power_ratio: float  # good_count / total_count (hoeher = staerker)
    suggested_weight: float  # 2.0 fuer Power-Keywords, 0.5 fuer Penalty-Keywords


@dataclass
class StrengthWeaknessPattern:
    """Ein wiederkehrendes Muster in AI-Staerken oder AI-Schwaechen."""

    pattern: str  # Der erkannte Text/Begriff (z.B. "DATEV", "SAP", "Monatsabschluss")
    category: str  # "must_have", "deal_breaker", "nice_to_have", "minor_issue"
    in_strengths_good: int  # Wie oft als Staerke in guten Matches
    in_strengths_bad: int  # Wie oft als Staerke in schlechten Matches
    in_weaknesses_good: int  # Wie oft als Schwaeche in guten Matches
    in_weaknesses_bad: int  # Wie oft als Schwaeche in schlechten Matches
    total_mentions: int  # Gesamtanzahl Erwaehungen
    impact_score: float  # -1.0 (Deal-Breaker) bis +1.0 (Must-Have)
    role_context: str | None = None  # In welchem Rollen-Kontext relevant


@dataclass
class CalibrationResult:
    """Gesamtergebnis einer Kalibrierung."""

    calibrated_at: str
    total_ai_matches: int
    total_with_roles: int

    # Analyse 1: Rollen-Kalibrierung
    role_pair_stats: list[RolePairStats] = field(default_factory=list)
    role_matrix_overrides: dict[str, float] = field(default_factory=dict)
    role_pairs_analyzed: int = 0

    # Analyse 2: Power-Keywords
    power_keywords: list[str] = field(default_factory=list)
    penalty_keywords: list[str] = field(default_factory=list)
    keyword_weight_boost: dict[str, float] = field(default_factory=dict)
    keyword_stats: list[PowerKeyword] = field(default_factory=list)

    # Analyse 3: Ausschluss-Paare
    exclusion_pairs: list[list[str]] = field(default_factory=list)

    # Analyse 4: Staerken/Schwaechen-Muster (NEU)
    strength_weakness_patterns: list[StrengthWeaknessPattern] = field(default_factory=list)
    must_have_skills: list[str] = field(default_factory=list)  # Skills die in guten Matches als Staerke vorkommen
    deal_breaker_gaps: list[str] = field(default_factory=list)  # Luecken die in schlechten Matches als Schwaeche vorkommen
    top_strengths_good_matches: list[str] = field(default_factory=list)  # Haeufigste Staerken in guten Matches
    top_weaknesses_bad_matches: list[str] = field(default_factory=list)  # Haeufigste Schwaechen in schlechten Matches

    # Meta
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Konvertiert in ein JSON-serialisierbares Dict."""
        return {
            "calibrated_at": self.calibrated_at,
            "total_ai_matches": self.total_ai_matches,
            "total_with_roles": self.total_with_roles,
            "role_pairs_analyzed": self.role_pairs_analyzed,
            "role_matrix_overrides": self.role_matrix_overrides,
            "role_pair_stats": [
                {
                    "job_role": rp.job_role,
                    "candidate_role": rp.candidate_role,
                    "sample_count": rp.sample_count,
                    "avg_ai_score": round(rp.avg_ai_score, 3),
                    "min_ai_score": round(rp.min_ai_score, 3),
                    "max_ai_score": round(rp.max_ai_score, 3),
                    "current_matrix_value": rp.current_matrix_value,
                    "suggested_value": round(rp.suggested_value, 3),
                    "deviation": round(rp.deviation, 3),
                }
                for rp in self.role_pair_stats
            ],
            "power_keywords": self.power_keywords,
            "penalty_keywords": self.penalty_keywords,
            "keyword_weight_boost": {
                k: round(v, 2) for k, v in self.keyword_weight_boost.items()
            },
            "keyword_stats": [
                {
                    "keyword": kw.keyword,
                    "good_match_count": kw.good_match_count,
                    "bad_match_count": kw.bad_match_count,
                    "total_count": kw.total_count,
                    "power_ratio": round(kw.power_ratio, 3),
                    "suggested_weight": kw.suggested_weight,
                }
                for kw in self.keyword_stats
            ],
            "exclusion_pairs": self.exclusion_pairs,
            "strength_weakness_patterns": [
                {
                    "pattern": p.pattern,
                    "category": p.category,
                    "in_strengths_good": p.in_strengths_good,
                    "in_strengths_bad": p.in_strengths_bad,
                    "in_weaknesses_good": p.in_weaknesses_good,
                    "in_weaknesses_bad": p.in_weaknesses_bad,
                    "total_mentions": p.total_mentions,
                    "impact_score": round(p.impact_score, 3),
                    "role_context": p.role_context,
                }
                for p in self.strength_weakness_patterns
            ],
            "must_have_skills": self.must_have_skills,
            "deal_breaker_gaps": self.deal_breaker_gaps,
            "top_strengths_good_matches": self.top_strengths_good_matches,
            "top_weaknesses_bad_matches": self.top_weaknesses_bad_matches,
            "warnings": self.warnings,
        }

    def to_json(self) -> str:
        """Gibt JSON-String zurueck."""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


# Minimum-Samples damit wir eine Rollen-Kombination kalibrieren
MIN_SAMPLES_FOR_CALIBRATION = 3

# Minimum-Samples fuer Ausschluss-Entscheidung (konservativer)
MIN_SAMPLES_FOR_EXCLUSION = 5

# AI-Score Schwellen
GOOD_MATCH_THRESHOLD = 0.7
BAD_MATCH_THRESHOLD = 0.3
EXCLUSION_THRESHOLD = 0.2

# Minimum-Vorkommen damit ein Keyword als "Power" gilt
MIN_KEYWORD_OCCURRENCES = 3

# Minimum Power-Ratio damit ein Keyword als Power-Keyword gilt
MIN_POWER_RATIO = 0.6

# Penalty-Keyword: Kommt hauptsaechlich in schlechten Matches vor
MAX_POWER_RATIO_FOR_PENALTY = 0.25


# ═══════════════════════════════════════════════════════════════
# SERVICE
# ═══════════════════════════════════════════════════════════════


class CalibrationService:
    """
    Analysiert DeepMatch-Ergebnisse und leitet Korrekturfaktoren ab.

    Drei Analysen:
    1. Rollen-Kalibrierung — Matrix-Werte anpassen
    2. Power-Keywords — Keyword-Gewichte anpassen
    3. Ausschluss-Paare — Schlechte Kombis ausschliessen
    """

    def __init__(self, db: AsyncSession):
        self.db = db

    # ──────────────────────────────────────────────────
    # Haupt-Methode: Kalibrierung ausfuehren
    # ──────────────────────────────────────────────────

    async def run_calibration(
        self,
        category: str = "FINANCE",
    ) -> CalibrationResult:
        """
        Fuehrt die komplette Kalibrierung aus.

        1. Laedt alle AI-bewerteten Matches mit Rollen-Infos
        2. Analysiert Rollen-Paare
        3. Analysiert Keywords
        4. Findet Ausschluss-Paare

        Args:
            category: Kategorie (z.B. "FINANCE")

        Returns:
            CalibrationResult mit allen Ergebnissen
        """
        result = CalibrationResult(
            calibrated_at=datetime.now(timezone.utc).isoformat(),
            total_ai_matches=0,
            total_with_roles=0,
        )

        # ── Schritt 1: Daten laden ──
        matches_data = await self._load_ai_matches(category)
        result.total_ai_matches = len(matches_data)

        if not matches_data:
            result.warnings.append(
                "Keine AI-bewerteten Matches gefunden. "
                "Fuehre zuerst einen Bulk-DeepMatch aus."
            )
            return result

        logger.info(
            f"Kalibrierung: {len(matches_data)} AI-Matches geladen "
            f"fuer Kategorie {category}"
        )

        # Filtere Matches mit Rollen-Info
        matches_with_roles = [
            m for m in matches_data if m["job_role"] and m["candidate_role"]
        ]
        result.total_with_roles = len(matches_with_roles)

        if len(matches_with_roles) < MIN_SAMPLES_FOR_CALIBRATION:
            result.warnings.append(
                f"Nur {len(matches_with_roles)} Matches mit Rollen-Info. "
                f"Mindestens {MIN_SAMPLES_FOR_CALIBRATION} benoetigt."
            )

        # ── Schritt 2: Rollen-Kalibrierung ──
        if matches_with_roles:
            self._analyze_role_pairs(matches_with_roles, result)

        # ── Schritt 3: Keyword-Analyse ──
        matches_with_keywords = [
            m for m in matches_data if m["matched_keywords"]
        ]
        if matches_with_keywords:
            self._analyze_keywords(matches_with_keywords, result)
        else:
            result.warnings.append("Keine Matches mit Keywords gefunden.")

        # ── Schritt 4: Ausschluss-Paare ──
        if matches_with_roles:
            self._find_exclusion_pairs(matches_with_roles, result)

        # ── Schritt 5: Staerken/Schwaechen-Tiefenanalyse (NEU) ──
        matches_with_sw = [
            m for m in matches_data
            if m["ai_strengths"] or m["ai_weaknesses"]
        ]
        if matches_with_sw:
            self._analyze_strength_weakness_patterns(matches_with_sw, result)
        else:
            result.warnings.append(
                "Keine Matches mit Staerken/Schwaechen-Daten gefunden."
            )

        logger.info(
            f"Kalibrierung abgeschlossen: "
            f"{result.role_pairs_analyzed} Rollen-Paare, "
            f"{len(result.power_keywords)} Power-Keywords, "
            f"{len(result.exclusion_pairs)} Ausschluss-Paare, "
            f"{len(result.must_have_skills)} Must-Haves, "
            f"{len(result.deal_breaker_gaps)} Deal-Breaker"
        )

        return result

    # ──────────────────────────────────────────────────
    # Daten laden
    # ──────────────────────────────────────────────────

    async def _load_ai_matches(self, category: str) -> list[dict]:
        """
        Laedt alle AI-bewerteten Matches mit relevanten Informationen.

        Returns:
            Liste von Dicts mit:
            - match_id, ai_score, matched_keywords
            - ai_strengths, ai_weaknesses, ai_explanation (NEU — fuer Tiefenanalyse)
            - job_role (Job.hotlist_job_title)
            - candidate_role (Candidate.hotlist_job_title)
            - candidate_roles (Candidate.hotlist_job_titles)
        """
        query = (
            select(
                Match.id,                      # 0
                Match.ai_score,                # 1
                Match.matched_keywords,        # 2
                Match.pre_score,               # 3
                Match.distance_km,             # 4
                Job.hotlist_job_title,          # 5
                Job.hotlist_city,              # 6
                Candidate.hotlist_job_title,    # 7
                Candidate.hotlist_job_titles,   # 8
                Candidate.hotlist_city,         # 9
                Match.ai_strengths,            # 10 — NEU
                Match.ai_weaknesses,           # 11 — NEU
                Match.ai_explanation,          # 12 — NEU
            )
            .join(Job, Match.job_id == Job.id)
            .join(Candidate, Match.candidate_id == Candidate.id)
            .where(
                and_(
                    Match.ai_score.is_not(None),
                    Job.hotlist_category == category,
                    Job.deleted_at.is_(None),
                    Candidate.deleted_at.is_(None),
                )
            )
        )

        result = await self.db.execute(query)
        rows = result.all()

        matches_data = []
        for row in rows:
            matches_data.append({
                "match_id": row[0],
                "ai_score": float(row[1]) if row[1] is not None else 0.0,
                "matched_keywords": row[2] or [],
                "pre_score": float(row[3]) if row[3] is not None else None,
                "distance_km": float(row[4]) if row[4] is not None else None,
                "job_role": (row[5] or "").strip(),
                "job_city": (row[6] or "").strip(),
                "candidate_role": (row[7] or "").strip(),
                "candidate_roles": row[8] or [],
                "candidate_city": (row[9] or "").strip(),
                "ai_strengths": row[10] or [],
                "ai_weaknesses": row[11] or [],
                "ai_explanation": (row[12] or "").strip(),
            })

        return matches_data

    # ──────────────────────────────────────────────────
    # Analyse 1: Rollen-Kalibrierung
    # ──────────────────────────────────────────────────

    def _analyze_role_pairs(
        self,
        matches: list[dict],
        result: CalibrationResult,
    ) -> None:
        """
        Analysiert AI-Scores pro Rollen-Paar und vergleicht mit Matrix.

        Gruppiert nach (job_role, candidate_role), berechnet Durchschnitt,
        und schlaegt neue Werte vor wenn Abweichung > 0.1.
        """
        from app.services.pre_scoring_service import FINANCE_ROLE_SIMILARITY

        # Gruppiere nach (job_role, candidate_role)
        pair_scores: dict[tuple[str, str], list[float]] = defaultdict(list)

        for m in matches:
            job_role = m["job_role"]
            cand_role = m["candidate_role"]
            if job_role and cand_role:
                pair_scores[(job_role, cand_role)].append(m["ai_score"])

        # Analysiere jedes Paar
        for (job_role, cand_role), scores in sorted(pair_scores.items()):
            if len(scores) < MIN_SAMPLES_FOR_CALIBRATION:
                continue

            avg_score = sum(scores) / len(scores)
            min_score = min(scores)
            max_score = max(scores)

            # Aktueller Matrix-Wert
            current_value = FINANCE_ROLE_SIMILARITY.get((job_role, cand_role))

            # Vorgeschlagener Wert: AI-Durchschnitt
            # Wir runden auf 0.05er-Schritte fuer saubere Matrix-Werte
            suggested = round(avg_score * 20) / 20  # Round to nearest 0.05
            suggested = max(0.0, min(1.0, suggested))

            # Abweichung berechnen
            deviation = (suggested - current_value) if current_value is not None else 0.0

            stat = RolePairStats(
                job_role=job_role,
                candidate_role=cand_role,
                sample_count=len(scores),
                avg_ai_score=avg_score,
                min_ai_score=min_score,
                max_ai_score=max_score,
                current_matrix_value=current_value,
                suggested_value=suggested,
                deviation=deviation,
            )
            result.role_pair_stats.append(stat)
            result.role_pairs_analyzed += 1

            # Wenn Abweichung signifikant: Override vorschlagen
            if current_value is not None and abs(deviation) > 0.1:
                key = f"{job_role}|{cand_role}"
                result.role_matrix_overrides[key] = suggested

                logger.info(
                    f"Rollen-Kalibrierung: {job_role} x {cand_role}: "
                    f"Matrix={current_value:.2f}, AI-Avg={avg_score:.2f}, "
                    f"Neu={suggested:.2f} (n={len(scores)})"
                )

    # ──────────────────────────────────────────────────
    # Analyse 2: Keyword-Power
    # ──────────────────────────────────────────────────

    def _analyze_keywords(
        self,
        matches: list[dict],
        result: CalibrationResult,
    ) -> None:
        """
        Findet Power-Keywords (korrelieren mit guten Matches)
        und Penalty-Keywords (korrelieren mit schlechten Matches).

        Power-Keywords: Kommen in > 60% der guten Matches vor
        Penalty-Keywords: Kommen in > 75% der schlechten Matches vor
        """
        # Zaehle Keywords in guten, schlechten und allen Matches
        keyword_good: dict[str, int] = defaultdict(int)
        keyword_bad: dict[str, int] = defaultdict(int)
        keyword_total: dict[str, int] = defaultdict(int)

        good_matches_count = 0
        bad_matches_count = 0

        for m in matches:
            score = m["ai_score"]
            keywords = m["matched_keywords"]

            is_good = score >= GOOD_MATCH_THRESHOLD
            is_bad = score <= BAD_MATCH_THRESHOLD

            if is_good:
                good_matches_count += 1
            if is_bad:
                bad_matches_count += 1

            for kw in keywords:
                kw_lower = kw.strip().lower()
                if not kw_lower:
                    continue
                keyword_total[kw_lower] += 1
                if is_good:
                    keyword_good[kw_lower] += 1
                if is_bad:
                    keyword_bad[kw_lower] += 1

        # Analysiere jedes Keyword
        for kw, total in sorted(keyword_total.items(), key=lambda x: -x[1]):
            if total < MIN_KEYWORD_OCCURRENCES:
                continue

            good_count = keyword_good.get(kw, 0)
            bad_count = keyword_bad.get(kw, 0)

            # Power-Ratio: Anteil guter Matches an allen Vorkommen
            power_ratio = good_count / total if total > 0 else 0.0

            # Bestimme Gewicht
            if power_ratio >= MIN_POWER_RATIO and good_count >= 2:
                suggested_weight = 2.0  # Power-Keyword: zaehlt doppelt
                result.power_keywords.append(kw)
                result.keyword_weight_boost[kw] = suggested_weight
            elif power_ratio <= MAX_POWER_RATIO_FOR_PENALTY and bad_count >= 2:
                suggested_weight = 0.5  # Penalty-Keyword: zaehlt halb
                result.penalty_keywords.append(kw)
                result.keyword_weight_boost[kw] = suggested_weight
            else:
                suggested_weight = 1.0  # Neutral

            stat = PowerKeyword(
                keyword=kw,
                good_match_count=good_count,
                bad_match_count=bad_count,
                total_count=total,
                power_ratio=power_ratio,
                suggested_weight=suggested_weight,
            )
            result.keyword_stats.append(stat)

        logger.info(
            f"Keyword-Analyse: {len(result.power_keywords)} Power-Keywords, "
            f"{len(result.penalty_keywords)} Penalty-Keywords "
            f"(aus {good_matches_count} guten, {bad_matches_count} schlechten Matches)"
        )

    # ──────────────────────────────────────────────────
    # Analyse 3: Ausschluss-Paare
    # ──────────────────────────────────────────────────

    def _find_exclusion_pairs(
        self,
        matches: list[dict],
        result: CalibrationResult,
    ) -> None:
        """
        Findet Rollen-Kombinationen, die IMMER schlecht abschneiden.

        Kriterium:
        - Mindestens MIN_SAMPLES_FOR_EXCLUSION Samples
        - ALLE ai_scores < EXCLUSION_THRESHOLD (0.2)
        - Oder: Durchschnitt < 0.15 und kein einzelner > 0.3
        """
        pair_scores: dict[tuple[str, str], list[float]] = defaultdict(list)

        for m in matches:
            job_role = m["job_role"]
            cand_role = m["candidate_role"]
            if job_role and cand_role:
                pair_scores[(job_role, cand_role)].append(m["ai_score"])

        for (job_role, cand_role), scores in sorted(pair_scores.items()):
            if len(scores) < MIN_SAMPLES_FOR_EXCLUSION:
                continue

            avg_score = sum(scores) / len(scores)
            max_score = max(scores)

            # Strenge Kriterien: Durchschnitt < 0.15 UND kein Ausreisser > 0.3
            if avg_score < 0.15 and max_score < 0.3:
                result.exclusion_pairs.append([job_role, cand_role])
                logger.info(
                    f"Ausschluss-Paar: {job_role} x {cand_role}: "
                    f"Avg={avg_score:.2f}, Max={max_score:.2f} (n={len(scores)})"
                )

    # ──────────────────────────────────────────────────
    # Analyse 4: Staerken/Schwaechen-Muster
    # ──────────────────────────────────────────────────

    def _analyze_strength_weakness_patterns(
        self,
        matches: list[dict],
        result: CalibrationResult,
    ) -> None:
        """
        Analysiert ai_strengths und ai_weaknesses Texte aus DeepMatch-Ergebnissen.

        Extrahiert Schluesselwoerter aus den Freitext-Listen und erkennt:
        - Must-Have Skills: Tauchen als Staerke in guten Matches auf
          UND als Schwaeche ("fehlt", "keine") in schlechten Matches
        - Deal-Breaker: Tauchen als Schwaeche in schlechten Matches auf
          und fehlen selten in guten Matches
        - Top-Staerken: Die haeufigsten Staerken in guten Matches
        - Top-Schwaechen: Die haeufigsten Schwaechen in schlechten Matches
        """
        # Schluesselwoerter extrahieren aus Staerken/Schwaechen-Texten
        # z.B. "5 Jahre DATEV-Erfahrung" → "datev"
        # z.B. "Keine SAP-Kenntnisse" → "sap"

        # Zaehler pro extrahiertem Begriff
        strengths_in_good: dict[str, int] = defaultdict(int)
        strengths_in_bad: dict[str, int] = defaultdict(int)
        weaknesses_in_good: dict[str, int] = defaultdict(int)
        weaknesses_in_bad: dict[str, int] = defaultdict(int)

        # Auch die rohen Texte sammeln fuer Top-Listen
        raw_strengths_good: dict[str, int] = defaultdict(int)
        raw_weaknesses_bad: dict[str, int] = defaultdict(int)

        good_count = 0
        bad_count = 0

        for m in matches:
            score = m["ai_score"]
            is_good = score >= GOOD_MATCH_THRESHOLD
            is_bad = score <= BAD_MATCH_THRESHOLD

            if is_good:
                good_count += 1
            if is_bad:
                bad_count += 1

            # Staerken analysieren
            for s in m["ai_strengths"]:
                s_clean = s.strip()
                if not s_clean:
                    continue

                # Rohen Text fuer Top-Listen
                if is_good:
                    raw_strengths_good[s_clean] += 1

                # Schluesselwoerter extrahieren
                terms = _extract_skill_terms(s_clean)
                for term in terms:
                    if is_good:
                        strengths_in_good[term] += 1
                    if is_bad:
                        strengths_in_bad[term] += 1

            # Schwaechen analysieren
            for w in m["ai_weaknesses"]:
                w_clean = w.strip()
                if not w_clean:
                    continue

                # Rohen Text fuer Top-Listen
                if is_bad:
                    raw_weaknesses_bad[w_clean] += 1

                # Schluesselwoerter extrahieren
                terms = _extract_skill_terms(w_clean)
                for term in terms:
                    if is_good:
                        weaknesses_in_good[term] += 1
                    if is_bad:
                        weaknesses_in_bad[term] += 1

        # ── Top-Staerken in guten Matches (rohe Texte, sortiert) ──
        result.top_strengths_good_matches = [
            text for text, count in sorted(
                raw_strengths_good.items(), key=lambda x: -x[1]
            )[:15]
        ]

        # ── Top-Schwaechen in schlechten Matches (rohe Texte, sortiert) ──
        result.top_weaknesses_bad_matches = [
            text for text, count in sorted(
                raw_weaknesses_bad.items(), key=lambda x: -x[1]
            )[:15]
        ]

        # ── Alle Begriffe zusammenfuehren und Impact berechnen ──
        all_terms = set(strengths_in_good) | set(strengths_in_bad) | \
                    set(weaknesses_in_good) | set(weaknesses_in_bad)

        for term in all_terms:
            sg = strengths_in_good.get(term, 0)
            sb = strengths_in_bad.get(term, 0)
            wg = weaknesses_in_good.get(term, 0)
            wb = weaknesses_in_bad.get(term, 0)
            total = sg + sb + wg + wb

            if total < 2:
                continue  # Zu wenig Daten

            # Impact Score berechnen:
            # Positiv = als Staerke in guten + als Schwaeche in schlechten (fehlt = schlecht)
            # Negativ = als Schwaeche in guten + als Staerke in schlechten (vorhanden = schlecht)
            positive_signal = sg + wb  # Staerke+gut ODER Schwaeche+schlecht = Skill ist wichtig
            negative_signal = sb + wg  # Staerke+schlecht ODER Schwaeche+gut = Skill irrelevant/schaedlich

            if (positive_signal + negative_signal) > 0:
                impact = (positive_signal - negative_signal) / (positive_signal + negative_signal)
            else:
                impact = 0.0

            # Kategorie bestimmen
            if impact >= 0.5 and sg >= 2 and wb >= 1:
                category = "must_have"
                result.must_have_skills.append(term)
            elif impact <= -0.5 and wb >= 2:
                category = "deal_breaker"
                result.deal_breaker_gaps.append(term)
            elif impact > 0:
                category = "nice_to_have"
            else:
                category = "minor_issue"

            pattern = StrengthWeaknessPattern(
                pattern=term,
                category=category,
                in_strengths_good=sg,
                in_strengths_bad=sb,
                in_weaknesses_good=wg,
                in_weaknesses_bad=wb,
                total_mentions=total,
                impact_score=impact,
            )
            result.strength_weakness_patterns.append(pattern)

        # Sortiere nach absolutem Impact (wichtigste zuerst)
        result.strength_weakness_patterns.sort(
            key=lambda p: abs(p.impact_score), reverse=True
        )

        logger.info(
            f"Staerken/Schwaechen-Analyse: "
            f"{len(result.must_have_skills)} Must-Haves, "
            f"{len(result.deal_breaker_gaps)} Deal-Breaker, "
            f"{len(result.strength_weakness_patterns)} Muster gesamt "
            f"(aus {good_count} guten, {bad_count} schlechten Matches)"
        )

    # ──────────────────────────────────────────────────
    # Kalibrierungsdaten laden (fuer Pre-Scoring)
    # ──────────────────────────────────────────────────

    @staticmethod
    async def load_calibration_data(db: AsyncSession) -> CalibrationResult | None:
        """
        Laedt die gespeicherten Kalibrierungsdaten aus der DB.

        Returns:
            CalibrationResult oder None wenn keine Daten vorhanden
        """
        query = text(
            "SELECT data FROM calibration_data "
            "ORDER BY created_at DESC LIMIT 1"
        )
        try:
            result = await db.execute(query)
            row = result.first()
            if not row:
                return None

            data = row[0] if isinstance(row[0], dict) else json.loads(row[0])
            return _dict_to_calibration_result(data)
        except Exception as e:
            logger.warning(f"Keine Kalibrierungsdaten gefunden: {e}")
            return None

    async def save_calibration_data(self, result: CalibrationResult) -> None:
        """
        Speichert die Kalibrierungsdaten in der DB.

        Erstellt die Tabelle falls sie nicht existiert (idempotent).
        """
        # Sicherstellen, dass die Tabelle existiert
        await self.db.execute(text("""
            CREATE TABLE IF NOT EXISTS calibration_data (
                id SERIAL PRIMARY KEY,
                data JSONB NOT NULL,
                category VARCHAR(50) DEFAULT 'FINANCE',
                total_samples INTEGER DEFAULT 0,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """))
        await self.db.commit()

        # Daten speichern
        data_json = result.to_dict()
        await self.db.execute(
            text("""
                INSERT INTO calibration_data (data, category, total_samples)
                VALUES (:data, :category, :samples)
            """),
            {
                "data": json.dumps(data_json, ensure_ascii=False),
                "category": "FINANCE",
                "samples": result.total_ai_matches,
            },
        )
        await self.db.commit()

        logger.info(
            f"Kalibrierungsdaten gespeichert: "
            f"{result.total_ai_matches} Samples, "
            f"{len(result.role_matrix_overrides)} Overrides, "
            f"{len(result.power_keywords)} Power-Keywords"
        )

    # ──────────────────────────────────────────────────
    # Statistiken: Wie viele AI-Matches haben wir?
    # ──────────────────────────────────────────────────

    async def get_ai_match_stats(self, category: str = "FINANCE") -> dict:
        """
        Gibt Statistiken ueber AI-bewertete Matches zurueck.

        Nuetzlich fuer die UI um zu zeigen, ob genug Daten vorhanden sind.
        """
        # Gesamt AI-Matches
        total_query = (
            select(func.count(Match.id))
            .join(Job, Match.job_id == Job.id)
            .where(
                and_(
                    Match.ai_score.is_not(None),
                    Job.hotlist_category == category,
                    Job.deleted_at.is_(None),
                )
            )
        )
        total_result = await self.db.execute(total_query)
        total = total_result.scalar() or 0

        # Durchschnittlicher AI-Score
        avg_query = (
            select(func.avg(Match.ai_score))
            .join(Job, Match.job_id == Job.id)
            .where(
                and_(
                    Match.ai_score.is_not(None),
                    Job.hotlist_category == category,
                    Job.deleted_at.is_(None),
                )
            )
        )
        avg_result = await self.db.execute(avg_query)
        avg_score = avg_result.scalar()

        # Score-Verteilung
        good_query = (
            select(func.count(Match.id))
            .join(Job, Match.job_id == Job.id)
            .where(
                and_(
                    Match.ai_score.is_not(None),
                    Match.ai_score >= GOOD_MATCH_THRESHOLD,
                    Job.hotlist_category == category,
                    Job.deleted_at.is_(None),
                )
            )
        )
        good_result = await self.db.execute(good_query)
        good = good_result.scalar() or 0

        bad_query = (
            select(func.count(Match.id))
            .join(Job, Match.job_id == Job.id)
            .where(
                and_(
                    Match.ai_score.is_not(None),
                    Match.ai_score <= BAD_MATCH_THRESHOLD,
                    Job.hotlist_category == category,
                    Job.deleted_at.is_(None),
                )
            )
        )
        bad_result = await self.db.execute(bad_query)
        bad = bad_result.scalar() or 0

        return {
            "total_ai_matches": total,
            "avg_ai_score": round(float(avg_score), 3) if avg_score else 0.0,
            "good_matches": good,
            "bad_matches": bad,
            "medium_matches": total - good - bad,
            "ready_for_calibration": total >= MIN_SAMPLES_FOR_CALIBRATION,
        }


# ═══════════════════════════════════════════════════════════════
# HELPER
# ═══════════════════════════════════════════════════════════════


# Bekannte Fachbegriffe die wir direkt erkennen wollen
_KNOWN_SKILL_TERMS = {
    # Software/Tools
    "datev", "sap", "lexware", "addison", "sage", "navision", "dynamics",
    "excel", "power bi", "powerbi", "tableau",
    # Buchhaltungs-Fachbegriffe
    "fibu", "finanzbuchhaltung", "anlagenbuchhaltung", "anlagenbuchhalter",
    "konsolidierung", "monatsabschluss", "monatsabschluesse",
    "jahresabschluss", "jahresabschluesse", "quartalsabschluss",
    "hgb", "ifrs", "us-gaap", "gaap",
    "bilanzierung", "bilanz",
    "umsatzsteuer", "ust", "ustva", "mehrwertsteuer",
    "lohnabrechnung", "lohnbuchhaltung", "gehaltsabrechnung", "entgeltabrechnung",
    "kreditorenbuchhaltung", "debitorenbuchhaltung",
    "zahlungsverkehr", "mahnwesen", "rechnungspruefung",
    "kostenrechnung", "controlling", "budgetierung",
    "steuererklarung", "steuererklaerung", "steuerrecht",
    # Qualifikationen
    "bilanzbuchhalter", "steuerfachangestellte", "steuerfachangestellter",
    "buchhalter", "finanzbuchhalter",
    "wirtschaftspruefer", "steuerberater",
    # Soft Skills / Erfahrung
    "berufserfahrung", "branchenerfahrung", "fuehrungserfahrung",
    "teamleitung", "prozessoptimierung",
}

# Woerter die wir ignorieren (zu generisch)
_STOP_WORDS = {
    "der", "die", "das", "und", "oder", "in", "mit", "von", "zu", "fuer",
    "fur", "eine", "ein", "einem", "einer", "den", "dem", "des", "ist",
    "hat", "wird", "sind", "war", "kann", "keine", "kein", "nicht",
    "sehr", "gut", "gute", "guter", "relevante", "relevanter",
    "erfahrung", "kenntnisse", "kenntnis", "faehigkeit", "faehigkeiten",
    "bereich", "jahre", "jahr", "umfassende", "fundierte", "solide",
    "direkte", "praktische", "spezifische", "fehlende", "mangelnde",
}


def _extract_skill_terms(text: str) -> list[str]:
    """
    Extrahiert Fachbegriffe aus einem Staerken/Schwaechen-Text.

    Beispiele:
    - "5 Jahre DATEV-Erfahrung" → ["datev"]
    - "Keine SAP-Kenntnisse" → ["sap"]
    - "Fundierte HGB-Bilanzierungskenntnisse" → ["hgb", "bilanzierung"]
    - "Erfahrung im Monatsabschluss" → ["monatsabschluss"]

    Returns:
        Liste von erkannten Fachbegriffen (lowercase)
    """
    import re

    text_lower = text.lower()
    found_terms = []

    # 1. Direkte Erkennung bekannter Begriffe
    for term in _KNOWN_SKILL_TERMS:
        if term in text_lower:
            found_terms.append(term)

    # 2. Wenn nichts gefunden: Versuch aus dem Text relevante Woerter zu extrahieren
    if not found_terms:
        # Entferne Satzzeichen, splitte in Woerter
        words = re.findall(r'[a-zäöüß]+', text_lower)
        for word in words:
            if len(word) >= 4 and word not in _STOP_WORDS:
                # Pruefe ob es ein zusammengesetztes Wort ist
                # z.B. "buchhaltungserfahrung" → wir nehmen das ganze Wort
                if len(word) >= 6:
                    found_terms.append(word)

    # Deduplizieren und zurueckgeben
    seen = set()
    unique = []
    for t in found_terms:
        if t not in seen:
            seen.add(t)
            unique.append(t)

    return unique


def _dict_to_calibration_result(data: dict) -> CalibrationResult:
    """Konvertiert ein Dict (aus DB) zurueck in ein CalibrationResult."""
    result = CalibrationResult(
        calibrated_at=data.get("calibrated_at", ""),
        total_ai_matches=data.get("total_ai_matches", 0),
        total_with_roles=data.get("total_with_roles", 0),
        role_pairs_analyzed=data.get("role_pairs_analyzed", 0),
        role_matrix_overrides=data.get("role_matrix_overrides", {}),
        power_keywords=data.get("power_keywords", []),
        penalty_keywords=data.get("penalty_keywords", []),
        keyword_weight_boost=data.get("keyword_weight_boost", {}),
        exclusion_pairs=data.get("exclusion_pairs", []),
        must_have_skills=data.get("must_have_skills", []),
        deal_breaker_gaps=data.get("deal_breaker_gaps", []),
        top_strengths_good_matches=data.get("top_strengths_good_matches", []),
        top_weaknesses_bad_matches=data.get("top_weaknesses_bad_matches", []),
        warnings=data.get("warnings", []),
    )

    # Role pair stats
    for rp in data.get("role_pair_stats", []):
        result.role_pair_stats.append(RolePairStats(
            job_role=rp["job_role"],
            candidate_role=rp["candidate_role"],
            sample_count=rp["sample_count"],
            avg_ai_score=rp["avg_ai_score"],
            min_ai_score=rp["min_ai_score"],
            max_ai_score=rp["max_ai_score"],
            current_matrix_value=rp.get("current_matrix_value"),
            suggested_value=rp["suggested_value"],
            deviation=rp["deviation"],
        ))

    # Keyword stats
    for kw in data.get("keyword_stats", []):
        result.keyword_stats.append(PowerKeyword(
            keyword=kw["keyword"],
            good_match_count=kw["good_match_count"],
            bad_match_count=kw["bad_match_count"],
            total_count=kw["total_count"],
            power_ratio=kw["power_ratio"],
            suggested_weight=kw["suggested_weight"],
        ))

    # Strength/Weakness patterns
    for p in data.get("strength_weakness_patterns", []):
        result.strength_weakness_patterns.append(StrengthWeaknessPattern(
            pattern=p["pattern"],
            category=p["category"],
            in_strengths_good=p["in_strengths_good"],
            in_strengths_bad=p["in_strengths_bad"],
            in_weaknesses_good=p["in_weaknesses_good"],
            in_weaknesses_bad=p["in_weaknesses_bad"],
            total_mentions=p["total_mentions"],
            impact_score=p["impact_score"],
            role_context=p.get("role_context"),
        ))

    return result
