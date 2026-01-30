"""DeepMatch Service - KI-gestützte Tiefenanalyse für Hotlisten-Matches.

Stufe 3 des Hotlisten-Systems:
- Nutzt OpenAI (gpt-4o-mini) für detaillierten Tätigkeits-Abgleich
- Vergleicht Kandidaten-Erfahrung mit Job-Anforderungen
- Wird nur ON-DEMAND ausgelöst (Benutzer wählt Kandidaten mit Checkboxen)
- Pre-Filter: Nur Kandidaten mit pre_score >= THRESHOLD

Ablauf:
1. Benutzer wählt Kandidaten in der Hotliste (Checkboxen)
2. System prüft pre_score >= 40
3. OpenAI bewertet: Tätigkeiten, Erfahrung, Qualifikationen
4. Ergebnis wird in Match gespeichert (ai_score, ai_explanation, etc.)
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.candidate import Candidate
from app.models.job import Job
from app.models.match import Match, MatchStatus
from app.services.openai_service import OpenAIService, MatchEvaluation

logger = logging.getLogger(__name__)

# Mindest-Pre-Score für DeepMatch (Kandidaten darunter werden übersprungen)
DEEPMATCH_PRE_SCORE_THRESHOLD = 40.0


# ═══════════════════════════════════════════════════════════════
# DEEPMATCH SYSTEM PROMPT
# ═══════════════════════════════════════════════════════════════

DEEPMATCH_SYSTEM_PROMPT = """Du bist ein erfahrener Recruiter-Analyst, der eine TIEFENANALYSE der Passung zwischen Kandidat und Stelle durchführt.

DEINE AUFGABE:
Vergleiche die konkreten TÄTIGKEITEN und ERFAHRUNGEN des Kandidaten mit den ANFORDERUNGEN der Stelle.

BEWERTUNGSKRITERIEN (Gewichtung):
1. **Tätigkeits-Abgleich** (40%) — Passen die bisherigen Aufgaben/Tätigkeiten des Kandidaten zu den Aufgaben der Stelle?
2. **Fachliche Qualifikation** (25%) — Hat der Kandidat die geforderten Qualifikationen, Zertifikate, Kenntnisse?
3. **Branchenerfahrung** (20%) — Kommt der Kandidat aus einer relevanten Branche?
4. **Entwicklungspotenzial** (15%) — Kann der Kandidat sich in die Stelle hineinentwickeln?

WICHTIG:
- Bewerte KONKRET und DETAILLIERT, nicht allgemein
- Nenne spezifische Übereinstimmungen und Lücken
- Score 0.0 (keine Passung) bis 1.0 (perfekte Passung)
- Alle Texte auf DEUTSCH
- Maximal 3 Stärken, 3 Schwächen

Antworte NUR mit einem validen JSON-Objekt:
{
  "score": 0.72,
  "explanation": "Konkreter Vergleich der Tätigkeiten...",
  "strengths": ["Stärke 1", "Stärke 2", "Stärke 3"],
  "weaknesses": ["Schwäche 1", "Schwäche 2"]
}"""


# ═══════════════════════════════════════════════════════════════
# DATENKLASSEN
# ═══════════════════════════════════════════════════════════════

@dataclass
class DeepMatchResult:
    """Ergebnis eines einzelnen DeepMatch."""
    match_id: UUID
    candidate_name: str
    job_position: str
    ai_score: float
    explanation: str
    strengths: list[str]
    weaknesses: list[str]
    success: bool
    error: str | None = None


@dataclass
class DeepMatchBatchResult:
    """Ergebnis einer Batch-DeepMatch-Operation."""
    total_requested: int
    evaluated: int
    skipped_low_score: int
    skipped_error: int
    avg_ai_score: float
    results: list[DeepMatchResult]
    total_cost_usd: float


# ═══════════════════════════════════════════════════════════════
# SERVICE
# ═══════════════════════════════════════════════════════════════

class DeepMatchService:
    """
    KI-gestützte Tiefenanalyse für Kandidat-Job-Matches.

    Erweitert den bestehenden OpenAI-Service mit:
    - Spezialisiertem Prompt für Tätigkeits-Abgleich
    - Pre-Score-Filter
    - Batch-Verarbeitung
    - Ergebnis-Speicherung in der DB
    """

    def __init__(self, db: AsyncSession, openai_service: OpenAIService | None = None):
        self.db = db
        self._openai = openai_service or OpenAIService()

    async def close(self) -> None:
        """Schließt den OpenAI-Client."""
        await self._openai.close()

    # ──────────────────────────────────────────────────
    # Einzelnes Match bewerten
    # ──────────────────────────────────────────────────

    async def evaluate_match(self, match_id: UUID) -> DeepMatchResult:
        """
        Führt eine DeepMatch-Analyse für ein einzelnes Match durch.

        Args:
            match_id: ID des Match-Eintrags

        Returns:
            DeepMatchResult mit KI-Bewertung
        """
        # Match mit Kandidat und Job laden
        result = await self.db.execute(
            select(Match, Candidate, Job)
            .join(Candidate, Match.candidate_id == Candidate.id)
            .join(Job, Match.job_id == Job.id)
            .where(Match.id == match_id)
        )
        row = result.first()

        if not row:
            return DeepMatchResult(
                match_id=match_id,
                candidate_name="Unbekannt",
                job_position="Unbekannt",
                ai_score=0.0,
                explanation="Match nicht gefunden",
                strengths=[],
                weaknesses=[],
                success=False,
                error="Match nicht gefunden",
            )

        match, candidate, job = row

        # Pre-Score-Check
        if match.pre_score is not None and match.pre_score < DEEPMATCH_PRE_SCORE_THRESHOLD:
            return DeepMatchResult(
                match_id=match_id,
                candidate_name=candidate.full_name,
                job_position=job.position,
                ai_score=0.0,
                explanation=f"Pre-Score zu niedrig ({match.pre_score:.0f} < {DEEPMATCH_PRE_SCORE_THRESHOLD:.0f})",
                strengths=[],
                weaknesses=[],
                success=False,
                error="Pre-Score unter Schwellenwert",
            )

        # Job-Daten vorbereiten
        job_data = {
            "position": job.position,
            "company_name": job.company_name,
            "industry": job.industry,
            "job_text": job.job_text,
            "city": job.display_city,
            "hotlist_category": job.hotlist_category,
        }

        # Kandidaten-Daten vorbereiten
        candidate_data = {
            "full_name": candidate.full_name,
            "current_position": candidate.current_position,
            "current_company": candidate.current_company,
            "skills": candidate.skills,
            "work_history": candidate.work_history,
            "education": candidate.education,
            "languages": candidate.languages,
            "it_skills": candidate.it_skills,
            "hotlist_category": candidate.hotlist_category,
        }

        # OpenAI-Bewertung mit DeepMatch-Prompt
        evaluation = await self._openai.evaluate_match(
            job_data=job_data,
            candidate_data=candidate_data,
        )

        # Ergebnis in DB speichern
        match.ai_score = evaluation.score
        match.ai_explanation = evaluation.explanation
        match.ai_strengths = evaluation.strengths
        match.ai_weaknesses = evaluation.weaknesses
        match.ai_checked_at = datetime.now(timezone.utc)

        if match.status == MatchStatus.NEW:
            match.status = MatchStatus.AI_CHECKED

        await self.db.commit()

        logger.info(
            f"DeepMatch für {candidate.full_name} ↔ {job.position}: "
            f"Score={evaluation.score:.2f}"
        )

        return DeepMatchResult(
            match_id=match_id,
            candidate_name=candidate.full_name,
            job_position=job.position,
            ai_score=evaluation.score,
            explanation=evaluation.explanation,
            strengths=evaluation.strengths,
            weaknesses=evaluation.weaknesses,
            success=evaluation.success,
            error=evaluation.error,
        )

    # ──────────────────────────────────────────────────
    # Batch-Bewertung (Benutzer wählt Kandidaten)
    # ──────────────────────────────────────────────────

    async def evaluate_selected_matches(
        self,
        match_ids: list[UUID],
    ) -> DeepMatchBatchResult:
        """
        Führt DeepMatch für eine Auswahl von Matches durch.

        Dies ist die Hauptfunktion, die der Benutzer auslöst,
        wenn er Kandidaten mit Checkboxen auswählt.

        Args:
            match_ids: Liste der ausgewählten Match-IDs

        Returns:
            DeepMatchBatchResult mit allen Ergebnissen
        """
        results: list[DeepMatchResult] = []
        evaluated = 0
        skipped_low = 0
        skipped_error = 0
        score_sum = 0.0

        for match_id in match_ids:
            try:
                result = await self.evaluate_match(match_id)
                results.append(result)

                if result.success:
                    evaluated += 1
                    score_sum += result.ai_score
                elif result.error == "Pre-Score unter Schwellenwert":
                    skipped_low += 1
                else:
                    skipped_error += 1

            except Exception as e:
                logger.error(f"DeepMatch Fehler für Match {match_id}: {e}")
                skipped_error += 1
                results.append(DeepMatchResult(
                    match_id=match_id,
                    candidate_name="Fehler",
                    job_position="Fehler",
                    ai_score=0.0,
                    explanation=str(e),
                    strengths=[],
                    weaknesses=[],
                    success=False,
                    error=str(e),
                ))

        avg_score = score_sum / evaluated if evaluated > 0 else 0.0

        logger.info(
            f"DeepMatch Batch abgeschlossen: {evaluated}/{len(match_ids)} bewertet, "
            f"{skipped_low} zu niedriger Pre-Score, "
            f"{skipped_error} Fehler, Ø Score={avg_score:.2f}"
        )

        return DeepMatchBatchResult(
            total_requested=len(match_ids),
            evaluated=evaluated,
            skipped_low_score=skipped_low,
            skipped_error=skipped_error,
            avg_ai_score=round(avg_score, 2),
            results=results,
            total_cost_usd=self._openai.total_usage.cost_usd,
        )

    # ──────────────────────────────────────────────────
    # User-Feedback speichern
    # ──────────────────────────────────────────────────

    async def save_feedback(
        self,
        match_id: UUID,
        feedback: str,
        note: str | None = None,
    ) -> bool:
        """
        Speichert Benutzer-Feedback zu einem DeepMatch-Ergebnis.

        Args:
            match_id: Match-ID
            feedback: "good", "neutral", "bad"
            note: Optionale Notiz

        Returns:
            True bei Erfolg
        """
        result = await self.db.execute(
            select(Match).where(Match.id == match_id)
        )
        match = result.scalar_one_or_none()

        if not match:
            return False

        match.user_feedback = feedback
        match.feedback_note = note
        match.feedback_at = datetime.now(timezone.utc)
        await self.db.commit()

        logger.info(f"Feedback für Match {match_id}: {feedback}")
        return True

    # ──────────────────────────────────────────────────
    # Kosten-Schätzung
    # ──────────────────────────────────────────────────

    def estimate_cost(self, num_candidates: int) -> dict:
        """
        Schätzt die Kosten für einen DeepMatch-Batch.

        Args:
            num_candidates: Anzahl der Kandidaten

        Returns:
            Dict mit Kosten-Informationen
        """
        cost = self._openai.estimate_cost(num_candidates)
        return {
            "num_candidates": num_candidates,
            "estimated_cost_usd": cost,
            "estimated_cost_eur": round(cost * 0.92, 4),  # Grobe USD→EUR Umrechnung
            "model": self._openai.MODEL,
        }

    async def __aenter__(self) -> "DeepMatchService":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()
