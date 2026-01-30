"""Finance Classifier Service — OpenAI-basierte Rollen-Klassifizierung.

Analysiert den gesamten Werdegang von FINANCE-Kandidaten und weist
die echte Berufsrolle zu (Bilanzbuchhalter, Finanzbuchhalter, etc.).

Die Ergebnisse werden als Trainingsdaten gespeichert, um den lokalen
Algorithmus (FinanceRulesEngine) zu trainieren.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import limits, settings
from app.models.candidate import Candidate
from app.models.job import Job

logger = logging.getLogger(__name__)

# Preise für gpt-4o-mini (Stand: Januar 2026)
PRICE_INPUT_PER_1M = 0.15
PRICE_OUTPUT_PER_1M = 0.60

# Erlaubte Rollen — alles andere wird ignoriert
ALLOWED_ROLES = {
    "Bilanzbuchhalter/in",
    "Finanzbuchhalter/in",
    "Kreditorenbuchhalter/in",
    "Debitorenbuchhalter/in",
    "Lohnbuchhalter/in",
    "Steuerfachangestellte/r",
}

# ═══════════════════════════════════════════════════════════════
# SYSTEM PROMPT — Finance-Rollen-Klassifizierung
# ═══════════════════════════════════════════════════════════════

FINANCE_CLASSIFIER_SYSTEM_PROMPT = """ROLLE DES MODELLS

Du bist ein sehr erfahrener Recruiter im Finance-Bereich (Deutschland) mit tiefem Verständnis für:
- Finanzbuchhaltung
- Bilanzbuchhaltung
- Kreditorenbuchhaltung
- Debitorenbuchhaltung
- Lohnbuchhaltung
- Steuerfachangestellte

Du analysierst ausschließlich Fakten aus dem Lebenslauf.
Jobtitel sind nicht verlässlich – Tätigkeiten und Qualifikationen sind entscheidend.

DEINE AUFGABE

Analysiere den gesamten Werdegang eines Kandidaten und:
1. Prüfe zuerst, ob die aktuelle Position eine leitende Position ist
2. Nur wenn keine Leitung, klassifiziere den Kandidaten in eine oder mehrere der definierten Rollen

WICHTIGE GRUNDREGELN (ABSOLUT)

- Gesamter Werdegang berücksichtigen
- Aktuelle Position bestimmt die PRIMARY_ROLE
- Gesamter Werdegang bestimmt ALLE roles (auch vergangene Schwerpunkte)
- Tätigkeiten > Jobtitel
- "Erstellung" ist NICHT gleich "Vorbereitung / Mitwirkung"
- Bilanzbuchhalter NUR mit formaler Qualifikation
- Mehrere Jobtitel sind ausdrücklich erlaubt
- Keine Annahmen, keine Interpretation, keine Vermutungen

SCHRITT 1 – AUSSCHLUSS: LEITENDE POSITION

Als LEITUNG gilt, wenn mindestens eines zutrifft:

Jobtitel enthält: Leiter, Head of, Teamleiter, Abteilungsleiter, Director, CFO, Finance Manager

ODER Tätigkeiten enthalten: disziplinarische Führung, fachliche Führung, Mitarbeiterverantwortung, Budgetverantwortung, Aufbau oder Leitung eines Teams

Wenn Leitung = true: KEINE weitere Klassifizierung durchführen.

SCHRITT 2 – ROLLENDEFINITIONEN

1. Bilanzbuchhalter/in

NUR wenn BEIDE Bedingungen erfüllt sind:

A) Tätigkeiten enthalten explizit:
- Erstellung von Monats-, Quartals- oder Jahresabschlüssen
- Konzernabschluss

UND

B) Qualifikation enthält explizit (in education, further_education oder Zertifikaten):
- "geprüfter Bilanzbuchhalter"
- "Bilanzbuchhalter IHK"
- "Bilanzbuchhalter Lehrgang / Weiterbildung / Zertifikat"

WICHTIG: Fehlt B, dann KEIN Bilanzbuchhalter, auch wenn Abschlüsse erstellt werden. Dann Finanzbuchhalter/in.

2. Finanzbuchhalter/in

Ein Kandidat ist Finanzbuchhalter, wenn mindestens eines zutrifft:
- Kreditoren UND Debitoren kommen innerhalb einer oder mehrerer Positionen gemeinsam vor
- Laufende Buchhaltung
- Kontenabstimmungen

UND / ODER Abschlüsse werden ausschließlich vorbereitend erwähnt:
- Vorbereitung
- Unterstützung
- Zuarbeit
- Mitwirkung
- Mitbearbeitung

Finanzbuchhalter ist eine eigenständige Rolle — wird anhand der Tätigkeiten aktiv erkannt.

3. Kreditorenbuchhalter/in (Accounts Payable)

Ein Kandidat ist Kreditorenbuchhalter, wenn:
- Tätigkeiten überwiegend oder ausschließlich Kreditoren enthalten
- KEINE Debitoren-Tätigkeiten in nennenswertem Umfang vorkommen

Typische Tätigkeiten: Kreditorenbuchhaltung, Accounts Payable, Eingangsrechnungsprüfung, Zahlungsverkehr Lieferanten

ABGRENZUNG: Kreditoren-Tätigkeiten können auch bei Finanzbuchhaltern vorkommen. Entscheidend ist, ob Debitoren ebenfalls regelmäßig ausgeführt wurden.

SONDERREGEL MEHRFACHROLLE:
Wenn Kreditoren UND Debitoren in mindestens 2 Positionen ODER über mindestens 2 Jahre gemeinsam ausgeübt:
ZWEI Titel vergeben: Finanzbuchhalter/in + Kreditorenbuchhalter/in

4. Debitorenbuchhalter/in (Accounts Receivable)

Analog zur Kreditorenbuchhaltung:
- Überwiegend oder ausschließlich Debitoren
- Fakturierung, Mahnwesen, Forderungsmanagement
- Keine oder nur untergeordnete Kreditoren-Tätigkeiten

SONDERREGEL MEHRFACHROLLE:
Wenn Debitoren UND Kreditoren in mindestens 2 Positionen ODER über mindestens 2 Jahre gemeinsam ausgeübt:
ZWEI Titel: Finanzbuchhalter/in + Debitorenbuchhalter/in

5. Lohnbuchhalter/in (Payroll Accountant)

Wenn Tätigkeiten enthalten: Lohn- und Gehaltsabrechnung, Entgeltabrechnung, Payroll, Sozialversicherungsmeldungen
IMMER Lohnbuchhalter/in

6. Steuerfachangestellte/r

Wenn Ausbildung oder Qualifikation enthält: Steuerfachangestellte/r, Ausbildung in einer Steuerkanzlei

IMMER ZWEI Titel vergeben: Finanzbuchhalter/in + Steuerfachangestellte/r

AUSNAHME: Wenn Bilanzbuchhalter-Qualifikation vorhanden (Bedingung B von Rolle 1):
Bilanzbuchhalter/in + Steuerfachangestellte/r

GEWICHTUNG

- Aktuelle Position bestimmt die PRIMARY_ROLE
- Gesamter Werdegang bestimmt ALLE roles (auch vergangene Schwerpunkte)

FALLBACK

Wenn work_history leer oder nicht vorhanden:
roles = [], reasoning = "Kein Werdegang vorhanden"

Wenn keine der 6 Rollen zutrifft (z.B. Controller, Wirtschaftsprüfer):
roles = [], primary_role = null

SCHLUSSSATZ

Entscheidungen dürfen nur auf explizit genannten Tätigkeiten und Qualifikationen basieren.
Wenn Informationen fehlen oder unklar sind, ist die konservativere Einstufung zu wählen.

AUSGABEFORMAT (strikt JSON)

{
  "is_leadership": true/false,
  "roles": ["Finanzbuchhalter/in", "Kreditorenbuchhalter/in"],
  "primary_role": "Finanzbuchhalter/in",
  "reasoning": "Kurze Begründung (max 2-3 Sätze)"
}

ERLAUBTE WERTE für roles:
- "Bilanzbuchhalter/in"
- "Finanzbuchhalter/in"
- "Kreditorenbuchhalter/in"
- "Debitorenbuchhalter/in"
- "Lohnbuchhalter/in"
- "Steuerfachangestellte/r"

Wenn is_leadership = true:
roles = [], primary_role = null, reasoning = "Leitende Position: [Titel/Tätigkeit]"
"""

# ═══════════════════════════════════════════════════════════════
# JOB CLASSIFIER PROMPT — für Stellenbeschreibungen
# ═══════════════════════════════════════════════════════════════

FINANCE_JOB_CLASSIFIER_PROMPT = """Du bist ein sehr erfahrener Recruiter im Finance-Bereich (Deutschland).

Analysiere die folgende Stellenbeschreibung und ordne sie einer oder mehreren Finance-Rollen zu.

WICHTIG: Der Jobtitel der Stelle kann irreführend sein! Eine Stelle die als "Bilanzbuchhalter" ausgeschrieben ist,
sucht möglicherweise nur einen Finanzbuchhalter, wenn in den Tätigkeiten steht "Unterstützung bei der Erstellung
der Jahresabschlüsse" statt "Erstellung der Jahresabschlüsse".

Gleiche Rollendefinitionen wie bei Kandidaten:
- Bilanzbuchhalter/in: Erstellung von Abschlüssen + Bilanzbuchhalter-Qualifikation gefordert
- Finanzbuchhalter/in: Kreditoren+Debitoren, laufende Buchhaltung, vorbereitende Abschlüsse
- Kreditorenbuchhalter/in: Nur/überwiegend Kreditoren
- Debitorenbuchhalter/in: Nur/überwiegend Debitoren
- Lohnbuchhalter/in: Lohn- und Gehaltsabrechnung
- Steuerfachangestellte/r: Steuerliche Tätigkeiten

AUSGABEFORMAT (strikt JSON):
{
  "roles": ["Finanzbuchhalter/in"],
  "primary_role": "Finanzbuchhalter/in",
  "reasoning": "Kurze Begründung"
}

ERLAUBTE WERTE: "Bilanzbuchhalter/in", "Finanzbuchhalter/in", "Kreditorenbuchhalter/in",
"Debitorenbuchhalter/in", "Lohnbuchhalter/in", "Steuerfachangestellte/r"
"""


# ═══════════════════════════════════════════════════════════════
# DATACLASSES
# ═══════════════════════════════════════════════════════════════

@dataclass
class ClassificationResult:
    """Ergebnis einer Finance-Rollen-Klassifizierung."""

    is_leadership: bool = False
    roles: list[str] = field(default_factory=list)
    primary_role: str | None = None
    reasoning: str = ""
    success: bool = True
    error: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def cost_usd(self) -> float:
        input_cost = (self.input_tokens / 1_000_000) * PRICE_INPUT_PER_1M
        output_cost = (self.output_tokens / 1_000_000) * PRICE_OUTPUT_PER_1M
        return round(input_cost + output_cost, 6)


@dataclass
class BatchClassificationResult:
    """Ergebnis einer Batch-Klassifizierung."""

    total: int = 0
    classified: int = 0
    skipped_leadership: int = 0
    skipped_no_cv: int = 0
    skipped_no_role: int = 0
    skipped_error: int = 0
    multi_title_count: int = 0
    roles_distribution: dict[str, int] = field(default_factory=dict)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    duration_seconds: float = 0.0
    # Listen für Analyse — ALLE Kandidaten nach Kategorie
    classified_candidates: list[dict] = field(default_factory=list)
    unclassified_candidates: list[dict] = field(default_factory=list)
    leadership_candidates: list[dict] = field(default_factory=list)
    error_candidates: list[dict] = field(default_factory=list)

    @property
    def cost_usd(self) -> float:
        input_cost = (self.total_input_tokens / 1_000_000) * PRICE_INPUT_PER_1M
        output_cost = (self.total_output_tokens / 1_000_000) * PRICE_OUTPUT_PER_1M
        return round(input_cost + output_cost, 4)


# ═══════════════════════════════════════════════════════════════
# SERVICE
# ═══════════════════════════════════════════════════════════════

class FinanceClassifierService:
    """Klassifiziert FINANCE-Kandidaten/Jobs via OpenAI anhand des Werdegangs."""

    MODEL = "gpt-4o-mini"

    def __init__(self, db: AsyncSession, api_key: str | None = None):
        self.db = db
        self.api_key = api_key or settings.openai_api_key
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
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
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    # ──────────────────────────────────────────────────
    # OpenAI API Call
    # ──────────────────────────────────────────────────

    async def _call_openai(
        self,
        system_prompt: str,
        user_prompt: str,
        retry_count: int = 2,
    ) -> dict[str, Any] | None:
        """Sendet einen Prompt an OpenAI und gibt die JSON-Antwort zurück."""
        if not self.api_key:
            logger.warning("OpenAI API-Key nicht konfiguriert")
            return None

        for attempt in range(retry_count + 1):
            try:
                client = await self._get_client()
                response = await client.post(
                    "/chat/completions",
                    json={
                        "model": self.MODEL,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                        "temperature": 0.1,
                        "max_tokens": 500,
                        "response_format": {"type": "json_object"},
                    },
                )
                response.raise_for_status()
                result = response.json()

                # Usage extrahieren
                usage = result.get("usage", {})
                content = result["choices"][0]["message"]["content"]
                parsed = json.loads(content)
                parsed["_usage"] = {
                    "input_tokens": usage.get("prompt_tokens", 0),
                    "output_tokens": usage.get("completion_tokens", 0),
                }
                return parsed

            except httpx.TimeoutException:
                if attempt < retry_count:
                    logger.warning(
                        f"Finance-Classifier Timeout, Versuch {attempt + 2}/{retry_count + 1}"
                    )
                    import asyncio
                    await asyncio.sleep(5 * (attempt + 1))
                    continue
                logger.error("Finance-Classifier Timeout nach allen Versuchen")
                return None

            except (httpx.HTTPStatusError, json.JSONDecodeError, KeyError) as e:
                logger.error(f"Finance-Classifier Fehler: {e}")
                return None

            except Exception as e:
                logger.error(f"Finance-Classifier unerwarteter Fehler: {e}")
                return None

        return None

    # ──────────────────────────────────────────────────
    # Kandidat klassifizieren
    # ──────────────────────────────────────────────────

    def _build_candidate_prompt(self, candidate: Candidate) -> str:
        """Baut den User-Prompt für einen Kandidaten."""
        parts = []

        parts.append(f"AKTUELLE POSITION: {candidate.current_position or 'Unbekannt'}")

        # Work History
        if candidate.work_history:
            parts.append("\nWERDEGANG:")
            entries = candidate.work_history if isinstance(candidate.work_history, list) else []
            for i, entry in enumerate(entries, 1):
                if isinstance(entry, dict):
                    pos = entry.get("position", "Unbekannt")
                    company = entry.get("company", "Unbekannt")
                    start = entry.get("start_date", "?")
                    end = entry.get("end_date", "?")
                    desc = entry.get("description", "")
                    parts.append(f"\n{i}. {pos} bei {company} ({start} - {end})")
                    if desc:
                        parts.append(f"   Tätigkeiten: {desc}")

        # Education
        if candidate.education:
            parts.append("\nAUSBILDUNG:")
            entries = candidate.education if isinstance(candidate.education, list) else []
            for entry in entries:
                if isinstance(entry, dict):
                    degree = entry.get("degree", "")
                    institution = entry.get("institution", "")
                    field = entry.get("field_of_study", "")
                    parts.append(f"- {degree} ({field}) — {institution}")

        # Further Education (Bilanzbuchhalter IHK etc.)
        if candidate.further_education:
            parts.append("\nWEITERBILDUNGEN / ZERTIFIKATE:")
            entries = candidate.further_education if isinstance(candidate.further_education, list) else []
            for entry in entries:
                if isinstance(entry, dict):
                    degree = entry.get("degree", "")
                    institution = entry.get("institution", "")
                    parts.append(f"- {degree} — {institution}")

        # IT Skills
        if candidate.it_skills:
            parts.append(f"\nIT-KENNTNISSE: {', '.join(candidate.it_skills)}")

        return "\n".join(parts)

    async def classify_candidate(self, candidate: Candidate) -> ClassificationResult:
        """Klassifiziert einen einzelnen FINANCE-Kandidaten via OpenAI."""

        # Kein Werdegang → überspringen
        if not candidate.work_history and not candidate.current_position:
            return ClassificationResult(
                success=False,
                error="Kein Werdegang vorhanden",
                reasoning="Kein Werdegang vorhanden",
            )

        user_prompt = self._build_candidate_prompt(candidate)
        result = await self._call_openai(FINANCE_CLASSIFIER_SYSTEM_PROMPT, user_prompt)

        if result is None:
            return ClassificationResult(
                success=False,
                error="OpenAI-Aufruf fehlgeschlagen",
            )

        # Usage extrahieren
        usage = result.pop("_usage", {})
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)

        # Ergebnis parsen
        is_leadership = result.get("is_leadership", False)
        roles = result.get("roles", [])
        primary_role = result.get("primary_role")
        reasoning = result.get("reasoning", "")

        # Rollen validieren — nur erlaubte Werte
        roles = [r for r in roles if r in ALLOWED_ROLES]
        if primary_role and primary_role not in ALLOWED_ROLES:
            primary_role = roles[0] if roles else None

        return ClassificationResult(
            is_leadership=is_leadership,
            roles=roles,
            primary_role=primary_role,
            reasoning=reasoning,
            success=True,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    # ──────────────────────────────────────────────────
    # Job klassifizieren
    # ──────────────────────────────────────────────────

    def _build_job_prompt(self, job: Job) -> str:
        """Baut den User-Prompt für einen Job."""
        parts = []
        parts.append(f"STELLENTITEL: {job.position or 'Unbekannt'}")
        if job.company_name:
            parts.append(f"UNTERNEHMEN: {job.company_name}")
        if job.job_text:
            parts.append(f"\nSTELLENBESCHREIBUNG:\n{job.job_text[:8000]}")
        return "\n".join(parts)

    async def classify_job(self, job: Job) -> ClassificationResult:
        """Klassifiziert einen einzelnen FINANCE-Job via OpenAI."""
        if not job.job_text and not job.position:
            return ClassificationResult(
                success=False,
                error="Keine Stellenbeschreibung vorhanden",
            )

        user_prompt = self._build_job_prompt(job)
        result = await self._call_openai(FINANCE_JOB_CLASSIFIER_PROMPT, user_prompt)

        if result is None:
            return ClassificationResult(success=False, error="OpenAI-Aufruf fehlgeschlagen")

        usage = result.pop("_usage", {})
        roles = [r for r in result.get("roles", []) if r in ALLOWED_ROLES]
        primary_role = result.get("primary_role")
        if primary_role and primary_role not in ALLOWED_ROLES:
            primary_role = roles[0] if roles else None

        return ClassificationResult(
            roles=roles,
            primary_role=primary_role,
            reasoning=result.get("reasoning", ""),
            success=True,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
        )

    # ──────────────────────────────────────────────────
    # Ergebnis auf Kandidat/Job anwenden
    # ──────────────────────────────────────────────────

    def apply_to_candidate(self, candidate: Candidate, result: ClassificationResult) -> None:
        """Setzt die Klassifizierungsergebnisse auf dem Kandidaten-Model."""
        if result.roles:
            candidate.hotlist_job_title = result.primary_role or result.roles[0]
            candidate.hotlist_job_titles = result.roles
        # Trainingsdaten speichern
        candidate.classification_data = {
            "source": "openai",
            "is_leadership": result.is_leadership,
            "roles": result.roles,
            "primary_role": result.primary_role,
            "reasoning": result.reasoning,
            "classified_at": datetime.now(timezone.utc).isoformat(),
        }

    def apply_to_job(self, job: Job, result: ClassificationResult) -> None:
        """Setzt die Klassifizierungsergebnisse auf dem Job-Model."""
        if result.roles:
            job.hotlist_job_title = result.primary_role or result.roles[0]
            job.hotlist_job_titles = result.roles

    # ──────────────────────────────────────────────────
    # Batch-Klassifizierung: Alle FINANCE-Kandidaten
    # ──────────────────────────────────────────────────

    async def classify_all_finance_candidates(
        self, force: bool = False, progress_callback=None,
    ) -> BatchClassificationResult:
        """Klassifiziert alle FINANCE-Kandidaten via OpenAI."""
        import asyncio
        start_time = datetime.now(timezone.utc)

        # Alle FINANCE-Kandidaten laden
        query = (
            select(Candidate)
            .where(
                and_(
                    Candidate.hotlist_category == "FINANCE",
                    Candidate.deleted_at.is_(None),
                )
            )
        )
        if not force:
            # Nur Kandidaten ohne classification_data
            query = query.where(Candidate.classification_data.is_(None))

        result = await self.db.execute(query)
        candidates = list(result.scalars().all())

        batch_result = BatchClassificationResult(total=len(candidates))
        logger.info(f"Finance-Klassifizierung: {len(candidates)} Kandidaten zu verarbeiten")

        for i, candidate in enumerate(candidates):
            try:
                classification = await self.classify_candidate(candidate)

                batch_result.total_input_tokens += classification.input_tokens
                batch_result.total_output_tokens += classification.output_tokens

                if not classification.success:
                    if classification.error == "Kein Werdegang vorhanden":
                        batch_result.skipped_no_cv += 1
                    else:
                        batch_result.skipped_error += 1
                        batch_result.error_candidates.append({
                            "id": str(candidate.id),
                            "name": candidate.full_name,
                            "position": candidate.current_position,
                            "error": classification.error,
                        })
                    continue

                if classification.is_leadership:
                    batch_result.skipped_leadership += 1
                    batch_result.leadership_candidates.append({
                        "id": str(candidate.id),
                        "name": candidate.full_name,
                        "position": candidate.current_position,
                        "reasoning": classification.reasoning,
                    })
                    # Trotzdem Trainingsdaten speichern
                    self.apply_to_candidate(candidate, classification)
                    continue

                if not classification.roles:
                    batch_result.skipped_no_role += 1
                    batch_result.unclassified_candidates.append({
                        "id": str(candidate.id),
                        "name": candidate.full_name,
                        "position": candidate.current_position,
                        "reasoning": classification.reasoning,
                    })
                    self.apply_to_candidate(candidate, classification)
                    continue

                # Ergebnis anwenden
                self.apply_to_candidate(candidate, classification)
                batch_result.classified += 1
                batch_result.classified_candidates.append({
                    "id": str(candidate.id),
                    "name": candidate.full_name,
                    "position": candidate.current_position,
                    "roles": classification.roles,
                    "primary_role": classification.primary_role,
                    "reasoning": classification.reasoning,
                })

                if len(classification.roles) > 1:
                    batch_result.multi_title_count += 1

                for role in classification.roles:
                    batch_result.roles_distribution[role] = (
                        batch_result.roles_distribution.get(role, 0) + 1
                    )

                # Fortschritt loggen + Callback
                if (i + 1) % 10 == 0:
                    if progress_callback:
                        progress_callback(i + 1, len(candidates), batch_result)
                    if (i + 1) % 50 == 0:
                        logger.info(
                            f"Finance-Klassifizierung: {i + 1}/{len(candidates)} "
                            f"({batch_result.classified} klassifiziert, "
                            f"${batch_result.cost_usd:.2f})"
                        )

                # Rate-Limiting: kurze Pause alle 10 Requests
                if (i + 1) % 10 == 0:
                    await asyncio.sleep(0.5)

                # Zwischenspeichern alle 50 Kandidaten
                if (i + 1) % 50 == 0:
                    await self.db.commit()

            except Exception as e:
                logger.error(f"Fehler bei Kandidat {candidate.id}: {e}")
                batch_result.skipped_error += 1

        # Finale Änderungen committen
        await self.db.commit()
        await self.close()

        duration = (datetime.now(timezone.utc) - start_time).total_seconds()
        batch_result.duration_seconds = round(duration, 1)

        logger.info(
            f"Finance-Klassifizierung abgeschlossen: "
            f"{batch_result.classified}/{batch_result.total} klassifiziert, "
            f"{batch_result.skipped_leadership} Führungskräfte, "
            f"{batch_result.multi_title_count} Multi-Titel, "
            f"${batch_result.cost_usd:.2f} in {batch_result.duration_seconds}s"
        )

        return batch_result

    # ──────────────────────────────────────────────────
    # Batch-Klassifizierung: Alle FINANCE-Jobs
    # ──────────────────────────────────────────────────

    async def classify_all_finance_jobs(
        self, force: bool = False
    ) -> BatchClassificationResult:
        """Klassifiziert alle FINANCE-Jobs via OpenAI."""
        import asyncio
        start_time = datetime.now(timezone.utc)

        query = (
            select(Job)
            .where(
                and_(
                    Job.hotlist_category == "FINANCE",
                    Job.deleted_at.is_(None),
                )
            )
        )
        if not force:
            query = query.where(Job.hotlist_job_titles.is_(None))

        result = await self.db.execute(query)
        jobs = list(result.scalars().all())

        batch_result = BatchClassificationResult(total=len(jobs))
        logger.info(f"Finance-Job-Klassifizierung: {len(jobs)} Jobs zu verarbeiten")

        for i, job in enumerate(jobs):
            try:
                classification = await self.classify_job(job)

                batch_result.total_input_tokens += classification.input_tokens
                batch_result.total_output_tokens += classification.output_tokens

                if not classification.success:
                    batch_result.skipped_error += 1
                    continue

                if not classification.roles:
                    batch_result.skipped_no_role += 1
                    continue

                self.apply_to_job(job, classification)
                batch_result.classified += 1

                if len(classification.roles) > 1:
                    batch_result.multi_title_count += 1

                for role in classification.roles:
                    batch_result.roles_distribution[role] = (
                        batch_result.roles_distribution.get(role, 0) + 1
                    )

                if (i + 1) % 50 == 0:
                    logger.info(f"Finance-Job-Klassifizierung: {i + 1}/{len(jobs)}")

                if (i + 1) % 10 == 0:
                    await asyncio.sleep(0.5)

            except Exception as e:
                logger.error(f"Fehler bei Job {job.id}: {e}")
                batch_result.skipped_error += 1

        await self.db.commit()
        await self.close()

        duration = (datetime.now(timezone.utc) - start_time).total_seconds()
        batch_result.duration_seconds = round(duration, 1)

        logger.info(
            f"Finance-Job-Klassifizierung abgeschlossen: "
            f"{batch_result.classified}/{batch_result.total}, "
            f"${batch_result.cost_usd:.2f} in {batch_result.duration_seconds}s"
        )

        return batch_result
