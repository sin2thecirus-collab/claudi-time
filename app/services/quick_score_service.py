"""Quick-AI Scorer — Schnelle, guenstige KI-Bewertung fuer JEDEN Pre-Match.

Phase C des Lern-Kreislaufs:
- Laeuft automatisch nach der Pre-Match-Generierung
- Schickt einen KURZEN Prompt an OpenAI (gpt-4o-mini)
- Bekommt: Score (0-100) + 1 Satz Begruendung
- Kostet ca. 1/4 eines DeepMatch (kuerzerer Prompt, weniger Tokens)

Unterschied zum DeepMatch:
- DeepMatch: Detailliert, teuer, on-demand (Benutzer waehlt), 3 Staerken + 3 Schwaechen
- Quick-AI: Schnell, guenstig, automatisch bei jedem Pre-Match, nur Score + 1 Satz

Der Quick-Score ergaenzt den Pre-Score (regelbasiert) mit einer KI-Einschaetzung.
Beide zusammen ergeben ein besseres Ranking als jeder Score allein.

Kosten-Schaetzung:
- Input: ~200 Tokens pro Match (kurzer Prompt)
- Output: ~50 Tokens pro Match (Score + 1 Satz)
- Bei 1000 Matches: ~$0.04 (4 Cent!)
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.candidate import Candidate
from app.models.job import Job
from app.models.match import Match
from app.services.openai_service import OpenAIService

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# SYSTEM PROMPT — Kurz und praezise
# ═══════════════════════════════════════════════════════════════

QUICK_SCORE_SYSTEM_PROMPT = """Du bist ein erfahrener Recruiter. Bewerte SCHNELL ob ein Kandidat zu einer Stelle passt.

REGELN:
- Score 0-100 (0=keine Passung, 100=perfekt)
- Taetigkeiten zaehlen mehr als Jobtitel
- 1 Satz Begruendung, DEUTSCH, maximal 80 Zeichen

Antworte NUR mit JSON:
{"score": 65, "reason": "Fibu-Erfahrung vorhanden, aber kein HGB-Abschluss"}"""


# ═══════════════════════════════════════════════════════════════
# DATENKLASSEN
# ═══════════════════════════════════════════════════════════════


@dataclass
class QuickScoreResult:
    """Ergebnis eines einzelnen Quick-AI-Scores."""
    match_id: UUID
    score: int  # 0-100
    reason: str
    success: bool
    error: str | None = None


@dataclass
class QuickScoreBatchResult:
    """Ergebnis einer Batch Quick-Score Operation."""
    total_matches: int = 0
    scored: int = 0
    skipped_already: int = 0
    skipped_error: int = 0
    avg_score: float = 0.0
    total_cost_usd: float = 0.0
    errors: list[str] = field(default_factory=list)


# ═══════════════════════════════════════════════════════════════
# SERVICE
# ═══════════════════════════════════════════════════════════════


class QuickScoreService:
    """
    Schnelle KI-Bewertung fuer Pre-Matches.

    Schickt einen kurzen Prompt an OpenAI und bekommt:
    - Score (0-100)
    - 1 Satz Begruendung

    Viel guenstiger als DeepMatch weil:
    - Kuerzerer System-Prompt (~100 Tokens vs ~800)
    - Kuerzerer User-Prompt (~200 Tokens vs ~1500)
    - Kuerzere Antwort (~50 Tokens vs ~200)
    """

    def __init__(self, db: AsyncSession, openai_service: OpenAIService | None = None):
        self.db = db
        self._openai = openai_service or OpenAIService()

    async def close(self) -> None:
        """Schliesst den OpenAI-Client."""
        await self._openai.close()

    # ──────────────────────────────────────────────────
    # Einzelnes Match bewerten
    # ──────────────────────────────────────────────────

    async def score_match(
        self,
        match_id: UUID,
        candidate: Candidate | None = None,
        job: Job | None = None,
        match: Match | None = None,
    ) -> QuickScoreResult:
        """
        Fuehrt einen Quick-AI-Score fuer ein einzelnes Match durch.

        Args:
            match_id: ID des Match-Eintrags
            candidate: Optional — Kandidat (spart DB-Query wenn schon geladen)
            job: Optional — Job (spart DB-Query wenn schon geladen)
            match: Optional — Match (spart DB-Query wenn schon geladen)

        Returns:
            QuickScoreResult mit Score und Begruendung
        """
        # Daten laden falls nicht uebergeben
        if not candidate or not job or not match:
            result = await self.db.execute(
                select(Match, Candidate, Job)
                .join(Candidate, Match.candidate_id == Candidate.id)
                .join(Job, Match.job_id == Job.id)
                .where(Match.id == match_id)
            )
            row = result.first()
            if not row:
                return QuickScoreResult(
                    match_id=match_id, score=0, reason="Match nicht gefunden",
                    success=False, error="Match nicht gefunden",
                )
            match, candidate, job = row

        # Kurzen Prompt bauen
        user_prompt = self._build_quick_prompt(job, candidate)

        try:
            # OpenAI aufrufen
            client = await self._openai._get_client()
            response = await client.post(
                "/chat/completions",
                json={
                    "model": self._openai.MODEL,
                    "messages": [
                        {"role": "system", "content": QUICK_SCORE_SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.2,
                    "max_tokens": 100,  # Nur Score + 1 Satz
                    "response_format": {"type": "json_object"},
                },
            )
            response.raise_for_status()
            result_json = response.json()

            # Usage tracken
            from app.services.openai_service import OpenAIUsage
            usage = OpenAIUsage(
                input_tokens=result_json.get("usage", {}).get("prompt_tokens", 0),
                output_tokens=result_json.get("usage", {}).get("completion_tokens", 0),
                total_tokens=result_json.get("usage", {}).get("total_tokens", 0),
            )
            self._openai._add_usage(usage)

            # Antwort parsen
            content = result_json["choices"][0]["message"]["content"]
            parsed = json.loads(content)

            score = max(0, min(100, int(parsed.get("score", 50))))
            reason = parsed.get("reason", "Keine Begruendung")[:200]

            # Ergebnis auf Match speichern
            match.quick_score = score
            match.quick_reason = reason
            match.quick_scored_at = datetime.now(timezone.utc)

            return QuickScoreResult(
                match_id=match_id, score=score, reason=reason, success=True,
            )

        except Exception as e:
            logger.error(f"Quick-Score Fehler fuer Match {match_id}: {e}")
            return QuickScoreResult(
                match_id=match_id, score=0, reason="",
                success=False, error=str(e)[:200],
            )

    # ──────────────────────────────────────────────────
    # Prompt Builder — KURZ!
    # ──────────────────────────────────────────────────

    def _build_quick_prompt(self, job: Job, candidate: Candidate) -> str:
        """
        Baut einen KURZEN Prompt fuer den Quick-Score.

        Maximal ~200 Tokens. Nur das Wichtigste:
        - Job: Titel, 3 Zeilen Stellenbeschreibung
        - Kandidat: Titel, letzte 2 Stationen, Skills
        """
        # Job: Titel + kurze Beschreibung
        job_title = job.position or job.hotlist_job_title or "Unbekannt"
        job_text = (job.job_text or "")[:300]  # Nur die ersten 300 Zeichen

        # Kandidat: Titel + letzte Stationen
        cand_title = candidate.hotlist_job_title or candidate.current_position or "Unbekannt"

        # Letzte 2 Stationen aus Werdegang
        work = candidate.work_history or []
        stations = []
        for entry in work[:2]:
            if isinstance(entry, dict):
                pos = entry.get("position", "?")
                comp = entry.get("company", "?")
                desc = (entry.get("description", "") or "")[:100]
                station = f"{pos} bei {comp}"
                if desc:
                    station += f" ({desc})"
                stations.append(station)
        work_text = "\n".join(stations) if stations else "Kein Werdegang"

        # Skills (max 10)
        skills = candidate.skills or []
        skills_text = ", ".join(skills[:10]) if skills else "Keine"

        return f"""STELLE: {job_title}
{job_text}

KANDIDAT: {cand_title}
Werdegang:
{work_text}
Skills: {skills_text}"""

    # ──────────────────────────────────────────────────
    # Batch-Scoring: Alle neuen Matches einer Kategorie
    # ──────────────────────────────────────────────────

    async def score_batch(
        self,
        category: str = "FINANCE",
        max_matches: int = 500,
        progress_callback: Callable[[str, str], None] | None = None,
    ) -> QuickScoreBatchResult:
        """
        Bewertet alle Matches einer Kategorie die noch keinen Quick-Score haben.

        Wird automatisch nach der Pre-Match-Generierung aufgerufen.

        Args:
            category: FINANCE oder ENGINEERING
            max_matches: Maximum Anzahl pro Durchlauf
            progress_callback: Optional callback(step, detail) fuer UI

        Returns:
            QuickScoreBatchResult mit Statistiken
        """
        result = QuickScoreBatchResult()

        if progress_callback:
            progress_callback("quick_ai", "Lade Matches ohne Quick-Score...")

        # Alle Matches ohne Quick-Score laden
        query = (
            select(Match, Candidate, Job)
            .join(Candidate, Match.candidate_id == Candidate.id)
            .join(Job, Match.job_id == Job.id)
            .where(
                and_(
                    Job.hotlist_category == category,
                    Job.deleted_at.is_(None),
                    Candidate.hidden == False,  # noqa: E712
                    Candidate.deleted_at.is_(None),
                    Match.quick_score.is_(None),  # Noch nicht bewertet
                )
            )
            .order_by(Match.pre_score.desc().nullslast())  # Beste zuerst
            .limit(max_matches)
        )

        db_result = await self.db.execute(query)
        rows = db_result.all()

        result.total_matches = len(rows)

        if not rows:
            if progress_callback:
                progress_callback("quick_ai", "Alle Matches haben bereits Quick-Scores")
            return result

        if progress_callback:
            cost_est = self._estimate_cost(len(rows))
            progress_callback(
                "quick_ai",
                f"{len(rows)} Matches gefunden. "
                f"Geschaetzte Kosten: ~${cost_est:.3f}",
            )

        score_sum = 0.0

        for i, (match, candidate, job) in enumerate(rows):
            try:
                qr = await self.score_match(
                    match_id=match.id,
                    candidate=candidate,
                    job=job,
                    match=match,
                )

                if qr.success:
                    result.scored += 1
                    score_sum += qr.score
                else:
                    result.skipped_error += 1
                    if qr.error:
                        result.errors.append(f"Match {match.id}: {qr.error[:80]}")

                # Fortschritt alle 10 Matches
                if progress_callback and (i % 10 == 0 or i == len(rows) - 1):
                    avg = score_sum / result.scored if result.scored > 0 else 0
                    progress_callback(
                        "quick_ai",
                        f"{i + 1}/{len(rows)} | "
                        f"Ø Score: {avg:.0f} | "
                        f"Kosten: ~${self._openai.total_usage.cost_usd:.3f}",
                    )

                # Batch commit alle 25 Matches
                if (i + 1) % 25 == 0:
                    await self.db.commit()

            except Exception as e:
                logger.error(f"Quick-Score Batch Fehler: {e}")
                result.skipped_error += 1
                result.errors.append(str(e)[:100])

        # Finaler Commit
        await self.db.commit()

        result.avg_score = round(score_sum / result.scored, 1) if result.scored > 0 else 0
        result.total_cost_usd = self._openai.total_usage.cost_usd

        if progress_callback:
            progress_callback(
                "quick_ai_done",
                f"Quick-AI fertig! {result.scored}/{result.total_matches} bewertet, "
                f"Ø Score: {result.avg_score:.0f}, "
                f"Kosten: ${result.total_cost_usd:.3f}",
            )

        logger.info(
            f"Quick-Score Batch {category}: {result.scored}/{result.total_matches}, "
            f"Ø={result.avg_score:.0f}, Kosten=${result.total_cost_usd:.3f}"
        )

        return result

    def _estimate_cost(self, num_matches: int) -> float:
        """Schaetzt Kosten fuer Quick-Scores."""
        # Quick-Score: ~200 Input + ~50 Output Tokens
        avg_input = 200
        avg_output = 50
        total_input = num_matches * avg_input
        total_output = num_matches * avg_output
        from app.services.openai_service import PRICE_INPUT_PER_1M, PRICE_OUTPUT_PER_1M
        input_cost = (total_input / 1_000_000) * PRICE_INPUT_PER_1M
        output_cost = (total_output / 1_000_000) * PRICE_OUTPUT_PER_1M
        return round(input_cost + output_cost, 4)

    async def __aenter__(self) -> "QuickScoreService":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()
