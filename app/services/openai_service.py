"""OpenAI Service - Zentrale KI-Funktionen.

Dieser Service bietet:
- CV-Parsing (strukturierte Extraktion aus Lebensläufen)
- Match-Bewertung (Kandidat-Job-Passung)
- Kosten-Tracking
- Fallback bei Fehlern
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import limits, settings

logger = logging.getLogger(__name__)


# Preise für gpt-4o-mini (Stand: Januar 2026)
# Input: $0.15 / 1M tokens, Output: $0.60 / 1M tokens
PRICE_INPUT_PER_1M = 0.15
PRICE_OUTPUT_PER_1M = 0.60


@dataclass
class OpenAIUsage:
    """Token-Verbrauch und Kosten einer API-Anfrage."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0

    @property
    def cost_usd(self) -> float:
        """Berechnet die Kosten in USD."""
        input_cost = (self.input_tokens / 1_000_000) * PRICE_INPUT_PER_1M
        output_cost = (self.output_tokens / 1_000_000) * PRICE_OUTPUT_PER_1M
        return round(input_cost + output_cost, 6)


@dataclass
class MatchEvaluation:
    """Ergebnis einer Match-Bewertung durch OpenAI."""

    score: float  # 0.0 - 1.0
    explanation: str  # Kurze Erklärung auf Deutsch
    strengths: list[str]  # Stärken
    weaknesses: list[str]  # Schwächen
    usage: OpenAIUsage
    success: bool = True
    error: str | None = None
    source: str = "openai"  # "openai" oder "fallback"


# System-Prompt für Match-Bewertung
MATCHING_SYSTEM_PROMPT = """Du bist ein erfahrener Recruiter, der die Passung zwischen Kandidaten und Stellenangeboten bewertet.

Bewerte die Passung anhand folgender Kriterien:
1. Relevante Berufserfahrung (Branchen, Positionen)
2. Übertragbare Skills und Kenntnisse
3. Fachliche Qualifikationen
4. Gesamte Karriereentwicklung

WICHTIG:
- Bewerte objektiv und fair
- Berücksichtige auch übertragbare Erfahrungen
- Score von 0.0 (keine Passung) bis 1.0 (perfekte Passung)
- Alle Texte auf DEUTSCH

Antworte NUR mit einem validen JSON-Objekt:
{
  "score": 0.75,
  "explanation": "Der Kandidat bringt relevante Erfahrung in der Buchhaltung mit, jedoch fehlt SAP-Erfahrung.",
  "strengths": ["5 Jahre Buchhaltungserfahrung", "DATEV-Kenntnisse"],
  "weaknesses": ["Keine SAP-Erfahrung", "Branchenwechsel"]
}"""


class OpenAIService:
    """Service für OpenAI API-Aufrufe.

    Unterstützt:
    - Match-Bewertung (Kandidat-Job-Passung)
    - Kosten-Tracking
    - Retry bei Timeout
    - Fallback bei Fehlern
    """

    MODEL = "gpt-4o-mini"

    def __init__(self, api_key: str | None = None):
        """Initialisiert den OpenAI-Service.

        Args:
            api_key: Optional API-Key (Standard: aus Settings)
        """
        self.api_key = api_key or settings.openai_api_key
        self._client: httpx.AsyncClient | None = None
        self._total_usage = OpenAIUsage()

        if not self.api_key:
            logger.warning("OpenAI API-Key nicht konfiguriert")

    async def _get_client(self) -> httpx.AsyncClient:
        """Gibt den HTTP-Client zurück."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url="https://api.openai.com/v1",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=httpx.Timeout(limits.TIMEOUT_OPENAI),
            )
        return self._client

    async def close(self) -> None:
        """Schließt den HTTP-Client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    @property
    def is_configured(self) -> bool:
        """Prüft, ob der Service konfiguriert ist."""
        return bool(self.api_key)

    @property
    def total_usage(self) -> OpenAIUsage:
        """Gesamter Token-Verbrauch der Session."""
        return self._total_usage

    async def evaluate_match(
        self,
        job_data: dict[str, Any],
        candidate_data: dict[str, Any],
        retry_count: int = 2,
    ) -> MatchEvaluation:
        """Bewertet die Passung zwischen Kandidat und Job.

        Args:
            job_data: Job-Informationen (position, company, job_text, etc.)
            candidate_data: Kandidaten-Informationen (name, skills, work_history, etc.)
            retry_count: Anzahl Retry-Versuche bei Timeout

        Returns:
            MatchEvaluation mit Score, Erklärung und Stärken/Schwächen
        """
        if not self.is_configured:
            return self._create_fallback_evaluation(
                error="OpenAI nicht konfiguriert",
                candidate_data=candidate_data,
                job_data=job_data,
            )

        # Prompt erstellen
        user_prompt = self._create_match_prompt(job_data, candidate_data)

        for attempt in range(retry_count + 1):
            try:
                client = await self._get_client()

                response = await client.post(
                    "/chat/completions",
                    json={
                        "model": self.MODEL,
                        "messages": [
                            {"role": "system", "content": MATCHING_SYSTEM_PROMPT},
                            {"role": "user", "content": user_prompt},
                        ],
                        "temperature": 0.3,
                        "max_tokens": 500,
                        "response_format": {"type": "json_object"},
                    },
                )

                response.raise_for_status()
                result = response.json()

                # Usage tracken
                usage = OpenAIUsage(
                    input_tokens=result.get("usage", {}).get("prompt_tokens", 0),
                    output_tokens=result.get("usage", {}).get("completion_tokens", 0),
                    total_tokens=result.get("usage", {}).get("total_tokens", 0),
                )
                self._add_usage(usage)

                # Response parsen
                content = result["choices"][0]["message"]["content"]
                parsed = json.loads(content)

                return MatchEvaluation(
                    score=min(1.0, max(0.0, float(parsed.get("score", 0.5)))),
                    explanation=parsed.get("explanation", "Keine Erklärung verfügbar"),
                    strengths=parsed.get("strengths", []),
                    weaknesses=parsed.get("weaknesses", []),
                    usage=usage,
                    success=True,
                    source="openai",
                )

            except httpx.TimeoutException:
                if attempt < retry_count:
                    logger.warning(f"OpenAI Timeout, Versuch {attempt + 2}/{retry_count + 1}")
                    continue
                return self._create_fallback_evaluation(
                    error="OpenAI Timeout nach mehreren Versuchen",
                    candidate_data=candidate_data,
                    job_data=job_data,
                )

            except json.JSONDecodeError as e:
                logger.error(f"OpenAI JSON-Fehler: {e}")
                return self._create_fallback_evaluation(
                    error="Ungültige OpenAI-Antwort",
                    candidate_data=candidate_data,
                    job_data=job_data,
                )

            except httpx.HTTPStatusError as e:
                logger.error(f"OpenAI HTTP-Fehler: {e.response.status_code}")
                return self._create_fallback_evaluation(
                    error=f"OpenAI-Fehler: {e.response.status_code}",
                    candidate_data=candidate_data,
                    job_data=job_data,
                )

            except Exception as e:
                logger.exception(f"OpenAI Fehler: {e}")
                return self._create_fallback_evaluation(
                    error=str(e),
                    candidate_data=candidate_data,
                    job_data=job_data,
                )

        # Sollte nie erreicht werden
        return self._create_fallback_evaluation(
            error="Unbekannter Fehler",
            candidate_data=candidate_data,
            job_data=job_data,
        )

    async def evaluate_matches(
        self,
        job_data: dict[str, Any],
        candidates: list[dict[str, Any]],
    ) -> list[MatchEvaluation]:
        """Bewertet mehrere Kandidaten für einen Job.

        Args:
            job_data: Job-Informationen
            candidates: Liste von Kandidaten-Daten

        Returns:
            Liste von MatchEvaluations
        """
        results = []
        for candidate_data in candidates:
            evaluation = await self.evaluate_match(job_data, candidate_data)
            results.append(evaluation)
        return results

    def _create_match_prompt(
        self,
        job_data: dict[str, Any],
        candidate_data: dict[str, Any],
    ) -> str:
        """Erstellt den Prompt für die Match-Bewertung."""
        job_text = job_data.get("job_text", "")
        if len(job_text) > 3000:
            job_text = job_text[:3000] + "..."

        work_history = candidate_data.get("work_history", [])
        work_history_text = ""
        if work_history:
            for entry in work_history[:5]:  # Max. 5 Einträge
                if isinstance(entry, dict):
                    work_history_text += (
                        f"- {entry.get('position', 'Position')} bei {entry.get('company', 'Firma')} "
                        f"({entry.get('start_date', '?')} - {entry.get('end_date', '?')})\n"
                    )

        education = candidate_data.get("education", [])
        education_text = ""
        if education:
            for entry in education[:3]:  # Max. 3 Einträge
                if isinstance(entry, dict):
                    education_text += (
                        f"- {entry.get('degree', 'Abschluss')} "
                        f"({entry.get('institution', 'Institution')})\n"
                    )

        skills = candidate_data.get("skills", [])
        skills_text = ", ".join(skills[:15]) if skills else "Keine angegeben"

        return f"""STELLENANGEBOT:
Position: {job_data.get('position', 'Nicht angegeben')}
Unternehmen: {job_data.get('company_name', 'Nicht angegeben')}
Branche: {job_data.get('industry', 'Nicht angegeben')}
Beschreibung:
{job_text}

KANDIDAT:
Name: {candidate_data.get('full_name', 'Unbekannt')}
Aktuelle Position: {candidate_data.get('current_position', 'Nicht angegeben')}
Aktuelles Unternehmen: {candidate_data.get('current_company', 'Nicht angegeben')}
Skills: {skills_text}

Berufserfahrung:
{work_history_text or 'Keine angegeben'}

Ausbildung:
{education_text or 'Keine angegeben'}

Bewerte die Passung zwischen diesem Kandidaten und der Stelle."""

    def _create_fallback_evaluation(
        self,
        error: str,
        candidate_data: dict[str, Any],
        job_data: dict[str, Any],
    ) -> MatchEvaluation:
        """Erstellt eine Fallback-Bewertung bei Fehlern.

        Verwendet einfaches Keyword-Matching als Ersatz.
        """
        logger.warning(f"Verwende Fallback-Bewertung: {error}")

        # Einfaches Keyword-Matching
        skills = set(s.lower() for s in (candidate_data.get("skills") or []))
        job_text = (job_data.get("job_text") or "").lower()

        matched = [s for s in skills if s in job_text]
        score = min(1.0, len(matched) / 5) if skills else 0.3

        return MatchEvaluation(
            score=score,
            explanation=f"KI-Bewertung fehlgeschlagen: {error}. Keyword-basierter Score.",
            strengths=matched[:3] if matched else ["Keyword-Analyse nicht verfügbar"],
            weaknesses=["KI-Bewertung konnte nicht durchgeführt werden"],
            usage=OpenAIUsage(),
            success=False,
            error=error,
            source="fallback",
        )

    def _add_usage(self, usage: OpenAIUsage) -> None:
        """Addiert Usage zum Gesamtverbrauch."""
        self._total_usage.input_tokens += usage.input_tokens
        self._total_usage.output_tokens += usage.output_tokens
        self._total_usage.total_tokens += usage.total_tokens

    def estimate_cost(self, num_candidates: int) -> float:
        """Schätzt die Kosten für eine Anzahl von KI-Checks.

        Args:
            num_candidates: Anzahl der zu prüfenden Kandidaten

        Returns:
            Geschätzte Kosten in USD
        """
        # Durchschnittlich ca. 800 Input-Tokens und 150 Output-Tokens pro Check
        avg_input = 800
        avg_output = 150

        total_input = num_candidates * avg_input
        total_output = num_candidates * avg_output

        input_cost = (total_input / 1_000_000) * PRICE_INPUT_PER_1M
        output_cost = (total_output / 1_000_000) * PRICE_OUTPUT_PER_1M

        return round(input_cost + output_cost, 4)

    async def __aenter__(self) -> "OpenAIService":
        """Context-Manager Entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context-Manager Exit."""
        await self.close()
