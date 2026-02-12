"""Interaction Analyzer Service — KI-gesteuerte Analyse von Kandidaten-Interaktionen.

Analysiert Transkriptionen (Telefonate) und E-Mail-Texte mit GPT-4o-mini
und extrahiert automatisch relevante Felder fuer den Kandidaten-Account:
- Wechselbereitschaft (ja/nein/unbekannt)
- Gehaltswunsch
- Kuendigungsfrist
- ERP-Kenntnisse
- Verfuegbarkeit
- Zusammenfassung / Notizen

Kosten: ~$0.0005-0.002 pro Analyse (GPT-4o-mini)
"""

import json
import logging
from datetime import datetime, timezone
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import limits, settings
from app.models.candidate import Candidate

logger = logging.getLogger(__name__)

# GPT-4o-mini Preise (Stand Jan 2026)
PRICE_INPUT_PER_1M = 0.15
PRICE_OUTPUT_PER_1M = 0.60

SYSTEM_PROMPT = """Du bist ein Recruiting-Assistent fuer ein Matching-Tool. Du analysierst Texte aus Kandidaten-Interaktionen (Telefonate, E-Mails) und extrahierst strukturierte Daten.

KONTEXT: Du erhaeltst den Text einer Interaktion mit einem Kandidaten (Telefonat-Transkription oder E-Mail). Extrahiere ALLE relevanten Informationen die sich auf den Kandidaten beziehen.

WICHTIG:
- Extrahiere NUR was explizit im Text steht. Erfinde NICHTS.
- Wenn eine Information nicht im Text vorkommt, setze den Wert auf null.
- Gehalt immer als String formatieren (z.B. "65.000 €", "55.000-60.000 €")
- Kuendigungsfrist als verstaendlicher Text (z.B. "3 Monate", "6 Wochen zum Quartalsende")
- ERP-Kenntnisse als Liste (z.B. ["SAP", "DATEV"])
- Wechselbereitschaft: "ja" wenn der Kandidat offen/interessiert/wechselwillig ist, "nein" wenn er explizit ablehnt, null wenn unklar

Antworte IMMER als JSON mit genau diesem Schema:

{
  "willingness_to_change": "ja" | "nein" | null,
  "salary": "Gehaltswunsch als String" | null,
  "notice_period": "Kuendigungsfrist als String" | null,
  "erp": ["ERP1", "ERP2"] | null,
  "availability": "Verfuegbarkeit als String" | null,
  "summary": "1-3 Saetze Zusammenfassung der Interaktion",
  "action_items": ["Aufgabe 1", "Aufgabe 2"] | null,
  "sentiment": "positiv" | "neutral" | "negativ",
  "key_facts": ["Fakt 1", "Fakt 2"]
}"""


class InteractionAnalyzerService:
    """Analysiert Kandidaten-Interaktionen mit GPT-4o-mini."""

    MODEL = "gpt-4o-mini"

    def __init__(self, db: AsyncSession):
        self.db = db
        self.api_key = settings.openai_api_key
        self._client: httpx.AsyncClient | None = None

        if not self.api_key:
            logger.warning("OpenAI API-Key nicht konfiguriert — Interaction Analyzer deaktiviert")

    async def _get_client(self) -> httpx.AsyncClient:
        """HTTP-Client fuer OpenAI API (Singleton)."""
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

    async def analyze_interaction(
        self,
        candidate_id: UUID,
        text: str,
        interaction_type: str = "call",
    ) -> dict:
        """Analysiert eine Interaktion und aktualisiert den Kandidaten.

        Args:
            candidate_id: UUID des Kandidaten
            text: Transkription oder E-Mail-Text
            interaction_type: "call", "email_received", "email_sent"

        Returns:
            Dict mit extracted_data, fields_updated, summary
        """
        if not self.api_key:
            return {"success": False, "error": "OpenAI API-Key nicht konfiguriert"}

        # Kandidat laden
        candidate = await self.db.get(Candidate, candidate_id)
        if not candidate:
            return {"success": False, "error": "Kandidat nicht gefunden"}

        # Kontext-Info fuer bessere Analyse
        context = self._build_context(candidate, interaction_type)

        # GPT-4o-mini analysieren lassen
        extracted = await self._call_openai(context, text)
        if not extracted:
            return {"success": False, "error": "KI-Analyse fehlgeschlagen"}

        # Felder aktualisieren
        fields_updated = await self._apply_extracted_data(candidate, extracted, text, interaction_type)

        # last_contact immer updaten
        candidate.last_contact = datetime.now(timezone.utc)
        candidate.updated_at = datetime.now(timezone.utc)
        await self.db.flush()

        return {
            "success": True,
            "candidate_id": str(candidate_id),
            "extracted_data": extracted,
            "fields_updated": fields_updated,
            "summary": extracted.get("summary", ""),
            "action_items": extracted.get("action_items"),
            "sentiment": extracted.get("sentiment", "neutral"),
        }

    def _build_context(self, candidate: Candidate, interaction_type: str) -> str:
        """Baut Kontext-Info fuer den Prompt."""
        type_label = {
            "call": "Telefonat",
            "email_received": "Eingehende E-Mail",
            "email_sent": "Gesendete E-Mail",
        }.get(interaction_type, "Interaktion")

        parts = [f"Interaktionstyp: {type_label}"]
        parts.append(f"Kandidat: {candidate.full_name}")

        if candidate.current_position:
            parts.append(f"Aktuelle Position: {candidate.current_position}")
        if candidate.current_company:
            parts.append(f"Aktuelles Unternehmen: {candidate.current_company}")
        if candidate.salary:
            parts.append(f"Bisheriger Gehaltswunsch: {candidate.salary}")
        if candidate.notice_period:
            parts.append(f"Bisherige Kuendigungsfrist: {candidate.notice_period}")
        if candidate.willingness_to_change:
            parts.append(f"Bisherige Wechselbereitschaft: {candidate.willingness_to_change}")
        if candidate.erp:
            parts.append(f"Bekannte ERP-Kenntnisse: {', '.join(candidate.erp)}")

        return "\n".join(parts)

    async def _call_openai(self, context: str, text: str) -> dict | None:
        """Ruft GPT-4o-mini auf und parst die Antwort."""
        user_message = f"KONTEXT:\n{context}\n\nTEXT DER INTERAKTION:\n{text}"

        for attempt in range(3):
            try:
                client = await self._get_client()
                response = await client.post(
                    "/chat/completions",
                    json={
                        "model": self.MODEL,
                        "messages": [
                            {"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": user_message},
                        ],
                        "temperature": 0.1,
                        "max_tokens": 600,
                        "response_format": {"type": "json_object"},
                    },
                )
                response.raise_for_status()
                result = response.json()

                content = result["choices"][0]["message"]["content"]
                parsed = json.loads(content)

                # Usage loggen
                usage = result.get("usage", {})
                input_tokens = usage.get("prompt_tokens", 0)
                output_tokens = usage.get("completion_tokens", 0)
                cost = (input_tokens / 1_000_000) * PRICE_INPUT_PER_1M + (output_tokens / 1_000_000) * PRICE_OUTPUT_PER_1M
                logger.info(f"Interaction Analyzer: {input_tokens}+{output_tokens} tokens, ${cost:.4f}")

                return parsed

            except httpx.TimeoutException:
                logger.warning(f"Interaction Analyzer Timeout (Versuch {attempt + 1}/3)")
                if attempt == 2:
                    return None
            except json.JSONDecodeError as e:
                logger.error(f"Interaction Analyzer JSON-Fehler: {e}")
                return None
            except httpx.HTTPStatusError as e:
                logger.error(f"Interaction Analyzer HTTP-Fehler: {e.response.status_code}")
                return None
            except Exception as e:
                logger.exception(f"Interaction Analyzer Fehler: {e}")
                return None

        return None

    async def _apply_extracted_data(
        self,
        candidate: Candidate,
        extracted: dict,
        raw_text: str,
        interaction_type: str,
    ) -> list[str]:
        """Wendet extrahierte Daten auf den Kandidaten an. Gibt Liste der geaenderten Felder zurueck."""
        updated = []

        # Wechselbereitschaft
        willingness = extracted.get("willingness_to_change")
        if willingness in ("ja", "nein"):
            candidate.willingness_to_change = willingness
            updated.append("willingness_to_change")

        # Gehalt (nur setzen wenn nicht leer)
        salary = extracted.get("salary")
        if salary and isinstance(salary, str) and salary.strip():
            candidate.salary = salary.strip()
            updated.append("salary")

        # Kuendigungsfrist
        notice = extracted.get("notice_period")
        if notice and isinstance(notice, str) and notice.strip():
            candidate.notice_period = notice.strip()
            updated.append("notice_period")

        # ERP-Kenntnisse (merge mit bestehenden)
        erp_new = extracted.get("erp")
        if erp_new and isinstance(erp_new, list) and len(erp_new) > 0:
            existing = list(candidate.erp or [])
            merged = existing.copy()
            for item in erp_new:
                if isinstance(item, str) and item.strip():
                    clean = item.strip()
                    if clean not in merged:
                        merged.append(clean)
            if merged != existing:
                candidate.erp = merged
                updated.append("erp")

        # Zusammenfassung als Notiz anhaengen
        summary = extracted.get("summary")
        if summary:
            type_label = {"call": "Telefonat", "email_received": "E-Mail (eingehend)", "email_sent": "E-Mail (ausgehend)"}.get(interaction_type, "Interaktion")
            timestamp = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M")
            note_entry = f"--- {timestamp} | {type_label} ---\n{summary}"

            if extracted.get("key_facts"):
                note_entry += "\nFakten: " + " | ".join(extracted["key_facts"])

            if candidate.candidate_notes:
                candidate.candidate_notes = candidate.candidate_notes + "\n\n" + note_entry
            else:
                candidate.candidate_notes = note_entry
            updated.append("candidate_notes")

        return updated
