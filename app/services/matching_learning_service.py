"""Matching Learning Service — Feedback-basiertes Lernen fuer Matching Engine v2.

Sprint 3: Feedback aufnehmen, Gewichte anpassen, Muster erkennen.

3 Stufen des Lernens:
1. Micro-Adjustment: Jedes Feedback verschiebt Gewichte minimal (±0.5-1.5%)
2. Bayesian Weight Optimization: Ab 100 Feedbacks — Gewichte datengetrieben optimieren
3. Pattern Mining: Ab 200 Feedbacks — Association Rules entdecken

Kosten: $0.00 (alles lokal, kein KI-Aufruf)
"""

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select, func, and_, case, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.match import Match
from app.models.candidate import Candidate
from app.models.job import Job
from app.models.match_v2_models import (
    MatchV2TrainingData,
    MatchV2LearnedRule,
    MatchV2ScoringWeight,
)

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════
# DATENKLASSEN
# ══════════════════════════════════════════════════════════════════

@dataclass
class FeedbackResult:
    """Ergebnis nach Feedback-Verarbeitung."""
    match_id: UUID
    outcome: str  # "good" / "bad" / "neutral"
    weights_adjusted: bool
    adjustments: dict  # {component: delta, ...}
    training_data_id: UUID | None = None
    learning_stage: str = "cold_start"  # cold_start / micro_adjustment / correlation / mature


@dataclass
class WeightOptimizationResult:
    """Ergebnis einer Gewichts-Optimierung."""
    method: str  # "micro_adjustment" / "bayesian" / "correlation"
    adjustments: dict[str, float]  # {component: new_weight}
    total_feedbacks_used: int
    improvement_estimate: float  # Geschaetzte Verbesserung 0.0-1.0


@dataclass
class LearningStats:
    """Statistiken zum Lernfortschritt."""
    total_feedbacks: int
    good_feedbacks: int
    bad_feedbacks: int
    neutral_feedbacks: int
    total_rules: int
    active_rules: int
    total_weight_adjustments: int
    top_performing_components: list[dict]  # [{component, avg_score_good, avg_score_bad}]
    learning_stage: str  # "cold_start" / "micro_adjustment" / "bayesian" / "mature"


# ══════════════════════════════════════════════════════════════════
# LEARNING SERVICE
# ══════════════════════════════════════════════════════════════════

class MatchingLearningService:
    """Feedback-basiertes Lernen fuer die Matching Engine v2.

    Lern-Stufen:
    - 0-50 Feedbacks: Cold Start (nur speichern, keine Anpassung)
    - 50-100: Micro-Adjustment (±0.5% pro Feedback)
    - 100-200: Korrelations-basierte Optimierung
    - 200+: Bayesian / XGBoost (Sprint 4)
    """

    # Minimum Feedbacks bevor Gewichte angepasst werden
    MIN_FEEDBACKS_FOR_ADJUSTMENT = 20
    # Minimum fuer Korrelationsanalyse
    MIN_FEEDBACKS_FOR_CORRELATION = 80
    # Minimum fuer fortgeschrittenes Lernen
    MIN_FEEDBACKS_FOR_ADVANCED = 200

    # Micro-Adjustment Staerke: Wie viel % wird pro Feedback verschoben
    MICRO_ADJUSTMENT_RATE = 0.008  # 0.8% pro Feedback

    # Gewichts-Grenzen (kein Component darf zu dominant oder zu schwach werden)
    MIN_WEIGHT = 2.0
    MAX_WEIGHT = 50.0

    def __init__(self, db: AsyncSession):
        self.db = db

    # ── Feedback aufnehmen ───────────────────────────────

    async def record_feedback(
        self,
        match_id: UUID,
        outcome: str,
        note: str | None = None,
        source: str = "user_feedback",
        rejection_reason: str | None = None,
        job_category: str | None = None,
    ) -> FeedbackResult:
        """Nimmt Feedback fuer einen Match auf und passt Gewichte an.

        Args:
            match_id: UUID des Matches
            outcome: "good" / "bad" / "neutral"
            note: Optionale Notiz vom Recruiter
            source: "user_feedback" / "placed" / "rejected"
            rejection_reason: "bad_distance" / "bad_skills" / "bad_seniority" (optional)
            job_category: Job-Kategorie fuer pro-Kategorie-Lernen (z.B. "Bilanzbuchhalter")

        Returns:
            FeedbackResult mit Details zur Anpassung
        """
        if outcome not in ("good", "bad", "neutral"):
            raise ValueError(f"Outcome muss 'good', 'bad' oder 'neutral' sein, nicht '{outcome}'")

        # Match laden mit Score-Breakdown
        match = await self.db.get(Match, match_id)
        if not match:
            raise ValueError(f"Match {match_id} nicht gefunden")

        # Feature-Snapshot erstellen
        features = self._extract_features(match)

        # Rejection-Reason in Features aufnehmen (fuer spaeteres Pattern Mining)
        if rejection_reason:
            features["rejection_reason"] = rejection_reason

        # Feedback auf dem Match speichern (wird auch vom Endpoint gesetzt,
        # aber hier nochmal fuer den Fall dass record_feedback() direkt aufgerufen wird)
        if not match.user_feedback:
            match.user_feedback = outcome
            match.feedback_note = note
            match.feedback_at = datetime.now(timezone.utc)

        # Training-Daten speichern (mit Grund und Job-Kategorie)
        training_data = MatchV2TrainingData(
            match_id=match_id,
            job_id=match.job_id,
            candidate_id=match.candidate_id,
            features=features,
            outcome=outcome,
            outcome_source=source,
            rejection_reason=rejection_reason,
            job_category=job_category,
        )
        self.db.add(training_data)
        await self.db.flush()

        # Gewichte anpassen (wenn genug Feedbacks)
        adjustments = {}
        weights_adjusted = False

        total_feedbacks = await self._count_feedbacks()

        if total_feedbacks >= self.MIN_FEEDBACKS_FOR_ADJUSTMENT and outcome != "neutral":
            if total_feedbacks >= self.MIN_FEEDBACKS_FOR_CORRELATION:
                # Ab 80 Feedbacks: Korrelations-basierte Anpassung (pro Kategorie)
                adjustments = await self._correlation_based_adjustment(job_category=job_category)
                weights_adjusted = bool(adjustments)
            else:
                # 20-80 Feedbacks: Micro-Adjustment (pro Kategorie)
                adjustments = await self._micro_adjust_weights(features, outcome, job_category=job_category)
                weights_adjusted = bool(adjustments)

        await self.db.commit()

        # Lern-Stufe bestimmen
        if total_feedbacks < 20:
            stage = "cold_start"
        elif total_feedbacks < 80:
            stage = "micro_adjustment"
        elif total_feedbacks < 200:
            stage = "correlation"
        else:
            stage = "mature"

        logger.info(
            f"Feedback '{outcome}' fuer Match {match_id} gespeichert "
            f"(reason={rejection_reason}, category={job_category}, "
            f"Total: {total_feedbacks}, Stage: {stage}, Weights adjusted: {weights_adjusted})"
        )

        return FeedbackResult(
            match_id=match_id,
            outcome=outcome,
            weights_adjusted=weights_adjusted,
            adjustments=adjustments,
            training_data_id=training_data.id,
            learning_stage=stage,
        )

    async def record_placement(self, match_id: UUID) -> FeedbackResult:
        """Spezial-Feedback: Kandidat wurde erfolgreich platziert.

        Platzierung = staerkstes positives Signal.
        """
        match = await self.db.get(Match, match_id)
        if match:
            match.placed_at = datetime.now(timezone.utc)

        return await self.record_feedback(
            match_id=match_id,
            outcome="good",
            note="Kandidat wurde erfolgreich platziert",
            source="placed",
        )

    # ── Feature-Extraktion ───────────────────────────────

    def _extract_features(self, match: Match) -> dict:
        """Extrahiert Features aus einem Match fuer Training-Daten.

        Speichert den Score-Breakdown als Feature-Snapshot,
        damit man spaeter analysieren kann welche Komponenten
        bei guten vs. schlechten Matches hoch/niedrig waren.
        """
        features = {}

        # v2 Score Breakdown (die wichtigsten Features)
        if match.v2_score_breakdown:
            features.update(match.v2_score_breakdown)

        # Gesamtscore
        if match.v2_score is not None:
            features["v2_total_score"] = match.v2_score

        # Legacy-Scores (fuer Vergleich)
        if match.ai_score is not None:
            features["legacy_ai_score"] = match.ai_score
        if match.pre_score is not None:
            features["legacy_pre_score"] = match.pre_score

        return features

    # ── Micro-Adjustment ─────────────────────────────────

    # Die 7 echten Scoring-Komponenten (muessen mit DEFAULT_WEIGHTS in matching_engine_v2 uebereinstimmen)
    SCORE_COMPONENTS = [
        "skill_overlap", "seniority_fit", "job_title_fit",
        "embedding_sim", "industry_fit", "career_fit", "software_match",
    ]

    async def _micro_adjust_weights(
        self,
        features: dict,
        outcome: str,
        job_category: str | None = None,
    ) -> dict[str, float]:
        """Passt Gewichte minimal an basierend auf einem einzelnen Feedback.

        Logik:
        - "good" Match: Erhoehe Gewicht von Komponenten die hoch waren,
          reduziere Gewicht von Komponenten die niedrig waren
        - "bad" Match: Umgekehrt — reduziere hoch-bewertete Komponenten,
          erhoehe niedrig-bewertete

        Pro-Kategorie: Wenn job_category angegeben, werden kategorie-spezifische
        Gewichte angepasst (oder aus globalen Defaults kopiert).

        Distanz: distance_km wird als Lern-Signal genutzt — bei bad_distance Feedback
        wird der Distanz-Impact indirekt ueber die anderen Komponenten sichtbar.

        Staerke: ±0.8% pro Feedback (selbst-limitierend, konvergiert)
        """
        # Score-Komponenten aus dem Breakdown
        score_components = {
            comp: features.get(comp) for comp in self.SCORE_COMPONENTS
        }

        # Nur Komponenten mit Werten
        valid = {k: v for k, v in score_components.items() if v is not None}
        if not valid:
            return {}

        # Durchschnitt aller Scores
        avg = sum(valid.values()) / len(valid)

        # Gewichte laden (pro Kategorie oder global)
        weights = await self._load_weights_for_category(job_category)

        adjustments = {}
        now = datetime.now(timezone.utc)

        for component, score in valid.items():
            if component not in weights:
                continue

            w = weights[component]
            deviation = score - avg  # Wie weit ueber/unter Durchschnitt

            if outcome == "good":
                # Guter Match: Belohne Komponenten die ueberdurchschnittlich waren
                delta = deviation * self.MICRO_ADJUSTMENT_RATE * w.weight
            else:
                # Schlechter Match: Bestrafe Komponenten die ueberdurchschnittlich waren
                delta = -deviation * self.MICRO_ADJUSTMENT_RATE * w.weight

            # Gewicht anpassen (mit Grenzen)
            new_weight = max(self.MIN_WEIGHT, min(self.MAX_WEIGHT, w.weight + delta))

            if abs(new_weight - w.weight) > 0.001:  # Nur wenn tatsaechlich Aenderung
                w.weight = round(new_weight, 3)
                w.adjustment_count += 1
                w.last_adjusted_at = now
                adjustments[component] = round(delta, 4)

        # Gewichte re-normalisieren (Summe = 100, pro Kategorie)
        if adjustments:
            await self._normalize_weights(job_category)

        return adjustments

    async def _load_weights_for_category(
        self, job_category: str | None = None,
    ) -> dict[str, "MatchV2ScoringWeight"]:
        """Laedt Gewichte fuer eine Kategorie. Erstellt kategorie-spezifische Kopie
        aus globalen Defaults falls noch nicht vorhanden.

        Args:
            job_category: z.B. "Bilanzbuchhalter" oder None fuer global.

        Returns:
            Dict {component_name: MatchV2ScoringWeight ORM-Objekt}
        """
        if job_category:
            # Versuche kategorie-spezifische Gewichte zu laden
            result = await self.db.execute(
                select(MatchV2ScoringWeight)
                .where(MatchV2ScoringWeight.job_category == job_category)
            )
            cat_weights = {w.component: w for w in result.scalars().all()}

            if cat_weights:
                return cat_weights

            # Keine kategorie-spezifischen Gewichte → aus globalen kopieren
            global_weights = await self._load_global_weights()
            now = datetime.now(timezone.utc)
            cat_weights = {}
            for comp, gw in global_weights.items():
                new_w = MatchV2ScoringWeight(
                    component=comp,
                    weight=gw.weight,
                    default_weight=gw.default_weight,
                    job_category=job_category,
                    adjustment_count=0,
                    last_adjusted_at=now,
                )
                self.db.add(new_w)
                cat_weights[comp] = new_w
            await self.db.flush()
            return cat_weights

        # Kein job_category → globale Gewichte
        return await self._load_global_weights()

    async def _load_global_weights(self) -> dict[str, "MatchV2ScoringWeight"]:
        """Laedt globale Gewichte (job_category IS NULL)."""
        result = await self.db.execute(
            select(MatchV2ScoringWeight)
            .where(MatchV2ScoringWeight.job_category.is_(None))
        )
        return {w.component: w for w in result.scalars().all()}

    # ── Korrelations-basierte Optimierung ─────────────────

    async def _correlation_based_adjustment(
        self, job_category: str | None = None,
    ) -> dict[str, float]:
        """Analysiert Korrelation zwischen Score-Komponenten und Outcome.

        Fuer jede Komponente berechnen:
        - Durchschnittlicher Score bei "good" Matches
        - Durchschnittlicher Score bei "bad" Matches
        - Unterschied = "Trennkraft" der Komponente

        Komponenten mit hoher Trennkraft bekommen mehr Gewicht.

        Pro-Kategorie: Wenn job_category angegeben, werden nur Feedbacks dieser
        Kategorie analysiert. Gewichte werden kategorie-spezifisch angepasst.

        Distanz-Lernen: distance_km wird als Analyse-Dimension aufgenommen.
        Bei hoher Trennkraft (bad_distance haeuft sich) wird das geloggt.
        """
        # Feedbacks laden (pro Kategorie oder alle)
        query = (
            select(
                MatchV2TrainingData.features,
                MatchV2TrainingData.outcome,
            )
            .where(MatchV2TrainingData.outcome.in_(["good", "bad"]))
            .order_by(MatchV2TrainingData.created_at.desc())
            .limit(500)
        )
        if job_category:
            query = query.where(MatchV2TrainingData.job_category == job_category)

        result = await self.db.execute(query)
        rows = result.all()

        if len(rows) < self.MIN_FEEDBACKS_FOR_CORRELATION:
            return {}

        # Sammle Scores pro Komponente und Outcome (inkl. distance_km als Analyse)
        analysis_components = self.SCORE_COMPONENTS + ["distance_km"]

        stats: dict[str, dict] = {}
        for comp in analysis_components:
            stats[comp] = {"good_scores": [], "bad_scores": []}

        for features, outcome in rows:
            if not features:
                continue
            for comp in analysis_components:
                val = features.get(comp)
                if val is not None:
                    stats[comp][f"{outcome}_scores"].append(val)

        # Trennkraft berechnen (Differenz zwischen Durchschnitt good vs. bad)
        separation_power = {}
        for comp in analysis_components:
            good = stats[comp]["good_scores"]
            bad = stats[comp]["bad_scores"]

            if len(good) >= 10 and len(bad) >= 10:
                avg_good = sum(good) / len(good)
                avg_bad = sum(bad) / len(bad)
                separation_power[comp] = avg_good - avg_bad
            else:
                separation_power[comp] = 0.0

        # Distanz-Trennkraft loggen (distance_km ist kein Gewicht, aber liefert Info)
        dist_sep = separation_power.pop("distance_km", 0.0)
        if abs(dist_sep) > 0.5:
            logger.info(
                f"Distanz-Trennkraft: {dist_sep:.3f} "
                f"(category={job_category or 'global'}) — "
                f"Distanz ist ein starkes Unterscheidungsmerkmal"
            )

        if not separation_power or max(abs(v) for v in separation_power.values()) < 0.01:
            return {}

        # Gewichte proportional zur Trennkraft setzen
        # Normalisiere so dass Summe = 100
        pos_power = {k: max(0.01, v) for k, v in separation_power.items()}
        total_power = sum(pos_power.values())

        target_weights = {
            k: round(v / total_power * 100, 2) for k, v in pos_power.items()
        }

        # Sanfte Anpassung: Bewege aktuelle Gewichte 20% Richtung Ziel
        blend_rate = 0.2

        weights = await self._load_weights_for_category(job_category)

        adjustments = {}
        now = datetime.now(timezone.utc)

        for comp, target in target_weights.items():
            if comp not in weights:
                continue

            w = weights[comp]
            target_clamped = max(self.MIN_WEIGHT, min(self.MAX_WEIGHT, target))
            blended = w.weight + blend_rate * (target_clamped - w.weight)
            blended = max(self.MIN_WEIGHT, min(self.MAX_WEIGHT, round(blended, 3)))

            if abs(blended - w.weight) > 0.01:
                delta = blended - w.weight
                w.weight = blended
                w.adjustment_count += 1
                w.last_adjusted_at = now
                adjustments[comp] = round(delta, 4)

        if adjustments:
            await self._normalize_weights(job_category)
            logger.info(
                f"Korrelations-Optimierung ({job_category or 'global'}): "
                f"{len(adjustments)} Gewichte angepasst "
                f"(Trennkraft: {separation_power})"
            )

        return adjustments

    # ── Hilfsmethoden ────────────────────────────────────

    async def _count_feedbacks(self) -> int:
        """Zaehlt die Gesamtanzahl der Feedbacks."""
        result = await self.db.execute(
            select(func.count(MatchV2TrainingData.id))
        )
        return result.scalar() or 0

    async def _normalize_weights(self, job_category: str | None = None):
        """Normalisiert Gewichte so dass die Summe = 100.

        Wenn job_category angegeben, werden nur die Gewichte dieser Kategorie normalisiert.
        Sonst nur die globalen (job_category IS NULL).
        """
        if job_category:
            query = select(MatchV2ScoringWeight).where(
                MatchV2ScoringWeight.job_category == job_category
            )
        else:
            query = select(MatchV2ScoringWeight).where(
                MatchV2ScoringWeight.job_category.is_(None)
            )

        result = await self.db.execute(query)
        weights = result.scalars().all()

        total = sum(w.weight for w in weights)
        if total > 0 and abs(total - 100) > 0.1:
            factor = 100.0 / total
            for w in weights:
                w.weight = round(w.weight * factor, 3)

    # ── Statistiken ──────────────────────────────────────

    async def get_learning_stats(self) -> LearningStats:
        """Gibt umfassende Lern-Statistiken zurueck."""
        # Feedback-Zaehlung
        result = await self.db.execute(
            select(
                MatchV2TrainingData.outcome,
                func.count(MatchV2TrainingData.id),
            )
            .group_by(MatchV2TrainingData.outcome)
        )
        outcome_counts = {row[0]: row[1] for row in result.all()}

        total = sum(outcome_counts.values())
        good = outcome_counts.get("good", 0)
        bad = outcome_counts.get("bad", 0)
        neutral = outcome_counts.get("neutral", 0)

        # Regeln
        rules_result = await self.db.execute(
            select(
                func.count(MatchV2LearnedRule.id),
                func.sum(case(
                    (MatchV2LearnedRule.active == True, 1),
                    else_=0,
                )),
            )
        )
        rules_row = rules_result.one()
        total_rules = rules_row[0] or 0
        active_rules = rules_row[1] or 0

        # Gewichts-Anpassungen
        weights_result = await self.db.execute(
            select(func.sum(MatchV2ScoringWeight.adjustment_count))
        )
        total_adjustments = weights_result.scalar() or 0

        # Top-Performing Components (welche Komponenten trennen gut/schlecht am besten)
        top_components = await self._analyze_component_performance()

        # Lern-Stufe bestimmen
        if total < 20:
            stage = "cold_start"
        elif total < 80:
            stage = "micro_adjustment"
        elif total < 200:
            stage = "correlation"
        else:
            stage = "mature"

        return LearningStats(
            total_feedbacks=total,
            good_feedbacks=good,
            bad_feedbacks=bad,
            neutral_feedbacks=neutral,
            total_rules=total_rules,
            active_rules=active_rules,
            total_weight_adjustments=total_adjustments,
            top_performing_components=top_components,
            learning_stage=stage,
        )

    async def _analyze_component_performance(self) -> list[dict]:
        """Analysiert welche Scoring-Komponenten am besten gut/schlecht trennen."""
        result = await self.db.execute(
            select(
                MatchV2TrainingData.features,
                MatchV2TrainingData.outcome,
            )
            .where(MatchV2TrainingData.outcome.in_(["good", "bad"]))
            .limit(500)
        )
        rows = result.all()

        # Alle 7 Score-Komponenten + distance_km als Analyse-Dimension
        analysis_components = self.SCORE_COMPONENTS + ["distance_km"]

        stats = {}
        for comp in analysis_components:
            stats[comp] = {"good": [], "bad": []}

        for features, outcome in rows:
            if not features:
                continue
            for comp in analysis_components:
                val = features.get(comp)
                if val is not None:
                    stats[comp][outcome].append(val)

        performance = []
        for comp in analysis_components:
            good = stats[comp]["good"]
            bad = stats[comp]["bad"]

            avg_good = sum(good) / len(good) if good else 0
            avg_bad = sum(bad) / len(bad) if bad else 0
            separation = avg_good - avg_bad

            performance.append({
                "component": comp,
                "avg_score_good_matches": round(avg_good, 3),
                "avg_score_bad_matches": round(avg_bad, 3),
                "separation_power": round(separation, 3),
                "sample_count_good": len(good),
                "sample_count_bad": len(bad),
            })

        # Sortiere nach Trennkraft (hoechste zuerst)
        performance.sort(key=lambda x: abs(x["separation_power"]), reverse=True)

        return performance

    # ── Aktuelle Gewichte ────────────────────────────────

    async def get_current_weights(self) -> dict:
        """Gibt die aktuellen Gewichte mit Aenderungshistorie zurueck."""
        result = await self.db.execute(
            select(MatchV2ScoringWeight).order_by(MatchV2ScoringWeight.weight.desc())
        )
        weights = result.scalars().all()

        return {
            "weights": [
                {
                    "component": w.component,
                    "weight": w.weight,
                    "default_weight": w.default_weight,
                    "change_from_default": round(w.weight - w.default_weight, 3),
                    "adjustment_count": w.adjustment_count,
                    "last_adjusted": w.last_adjusted_at.isoformat() if w.last_adjusted_at else None,
                }
                for w in weights
            ],
            "total_weight": round(sum(w.weight for w in weights), 1),
        }

    # ── Gewichte manuell zuruecksetzen ───────────────────

    async def reset_weights(self) -> dict:
        """Setzt alle Gewichte auf die Default-Werte zurueck."""
        result = await self.db.execute(select(MatchV2ScoringWeight))
        weights = result.scalars().all()

        for w in weights:
            w.weight = w.default_weight
            w.adjustment_count = 0
            w.last_adjusted_at = None

        await self.db.commit()

        logger.info("Gewichte auf Defaults zurueckgesetzt")
        return {"status": "reset", "weights": {w.component: w.weight for w in weights}}

    # ── Feedback-Historie ────────────────────────────────

    async def get_feedback_history(
        self,
        limit: int = 50,
        outcome_filter: str | None = None,
    ) -> list[dict]:
        """Gibt die letzten Feedbacks zurueck."""
        query = (
            select(MatchV2TrainingData)
            .order_by(MatchV2TrainingData.created_at.desc())
            .limit(limit)
        )

        if outcome_filter:
            query = query.where(MatchV2TrainingData.outcome == outcome_filter)

        result = await self.db.execute(query)
        rows = result.scalars().all()

        return [
            {
                "id": str(r.id),
                "match_id": str(r.match_id) if r.match_id else None,
                "job_id": str(r.job_id) if r.job_id else None,
                "candidate_id": str(r.candidate_id) if r.candidate_id else None,
                "outcome": r.outcome,
                "outcome_source": r.outcome_source,
                "rejection_reason": r.rejection_reason,
                "job_category": r.job_category,
                "features": r.features,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]

    # ── Erweiterte Analyse-Statistiken ──────────────────

    async def get_extended_stats(self) -> dict:
        """Erweiterte Lern-Statistiken mit Ablehnungsgruenden, Job-Kategorien und Gewichts-Verlauf.

        Returns:
            {
                "overview": {...},
                "rejection_reasons": {...},
                "by_job_category": [...],
                "weight_changes": [...],
                "recent_feedbacks": [...],
            }
        """
        # 1. Basis-Stats (wiederverwenden)
        basic_stats = await self.get_learning_stats()

        # 2. Ablehnungsgruende-Verteilung
        reason_result = await self.db.execute(
            select(
                MatchV2TrainingData.rejection_reason,
                func.count(MatchV2TrainingData.id),
            )
            .where(MatchV2TrainingData.rejection_reason.isnot(None))
            .group_by(MatchV2TrainingData.rejection_reason)
        )
        reason_labels = {
            "bad_distance": "Distanz passt nicht",
            "bad_skills": "Taetigkeiten passen nicht",
            "bad_seniority": "Seniority passt nicht",
        }
        rejection_reasons = {
            row[0]: {"count": row[1], "label": reason_labels.get(row[0], row[0])}
            for row in reason_result.all()
        }

        # 3. Aufschluesselung pro Job-Kategorie
        cat_result = await self.db.execute(
            select(
                MatchV2TrainingData.job_category,
                MatchV2TrainingData.outcome,
                func.count(MatchV2TrainingData.id),
            )
            .where(MatchV2TrainingData.job_category.isnot(None))
            .group_by(MatchV2TrainingData.job_category, MatchV2TrainingData.outcome)
        )
        cat_data: dict[str, dict] = {}
        for category, outcome, count in cat_result.all():
            if category not in cat_data:
                cat_data[category] = {"category": category, "good": 0, "bad": 0, "neutral": 0, "total": 0}
            cat_data[category][outcome] = count
            cat_data[category]["total"] += count

        by_job_category = sorted(cat_data.values(), key=lambda x: x["total"], reverse=True)

        # 4. Gewichts-Veraenderungen pro Kategorie
        weights_result = await self.db.execute(
            select(MatchV2ScoringWeight)
            .order_by(
                MatchV2ScoringWeight.job_category.asc().nullsfirst(),
                MatchV2ScoringWeight.weight.desc(),
            )
        )
        weight_rows = weights_result.scalars().all()

        weight_changes = []
        for w in weight_rows:
            weight_changes.append({
                "component": w.component,
                "job_category": w.job_category or "__global__",
                "weight": w.weight,
                "default_weight": w.default_weight,
                "change": round(w.weight - w.default_weight, 3),
                "adjustment_count": w.adjustment_count,
                "last_adjusted": w.last_adjusted_at.isoformat() if w.last_adjusted_at else None,
            })

        # 5. Letzte 20 Feedbacks
        recent = await self.get_feedback_history(limit=20)

        return {
            "overview": {
                "total_feedbacks": basic_stats.total_feedbacks,
                "good": basic_stats.good_feedbacks,
                "bad": basic_stats.bad_feedbacks,
                "neutral": basic_stats.neutral_feedbacks,
                "learning_stage": basic_stats.learning_stage,
                "total_rules": basic_stats.total_rules,
                "active_rules": basic_stats.active_rules,
                "total_weight_adjustments": basic_stats.total_weight_adjustments,
            },
            "rejection_reasons": rejection_reasons,
            "by_job_category": by_job_category,
            "weight_changes": weight_changes,
            "component_performance": basic_stats.top_performing_components,
            "recent_feedbacks": recent,
        }
