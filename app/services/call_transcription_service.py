"""Call Transcription Service — Whisper + GPT-4o-mini Pipeline.

Zwei-Stufen-Klassifizierung:
1. Whisper API: Audio → Transkript (deutsch)
2. GPT-4o-mini Stufe 1: Gesprächstyp erkennen (qualifizierung/kurz/kunde/sonstig)
3. GPT-4o-mini Stufe 2: Strukturierte Felder extrahieren je nach Typ

Kosten:
- Whisper: $0.006/Min (~27 Cent für 45-Min-Gespräch)
- GPT-4o-mini: ~$0.001-0.003 pro Analyse
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

# Preise
WHISPER_PRICE_PER_MIN = 0.006
GPT_INPUT_PER_1M = 0.15
GPT_OUTPUT_PER_1M = 0.60


# ── Stufe 1: Gesprächstyp erkennen ──
CLASSIFY_SYSTEM_PROMPT = """Du bist ein Recruiting-Assistent. Du analysierst Transkriptionen von Telefonaten und bestimmst den Gesprächstyp.

GESPRÄCHSTYPEN:
- "qualifizierung": Ausführliches Qualifizierungsgespräch mit einem Kandidaten. Erkennbar an: Fragen zu gewünschten Positionen, Home-Office, Pendelbereitschaft, Gehalt, Kündigungsfrist, ERP-Kenntnisse, Branchen, Großraumbüro, WhatsApp-Kontakt, andere Recruiter, Exklusivität. Dauert typisch 15-45 Minuten.
- "kurz": Kurzer Anruf mit einem Kandidaten (z.B. Terminvereinbarung, kurze Rückfrage, Status-Update). Unter 10 Minuten, keine ausführliche Qualifizierung.
- "kunde": Gespräch mit einem Kunden/Kontakt/Unternehmen (nicht mit einem Kandidaten). Erkennbar an: Stellenbeschreibung besprechen, Kandidaten vorstellen, Konditionen klären.
- "sonstig": Alles was nicht in die anderen Kategorien passt.

Antworte NUR als JSON:
{
  "call_type": "qualifizierung" | "kurz" | "kunde" | "sonstig",
  "confidence": 0.0-1.0,
  "reasoning": "Kurze Begründung (1 Satz)"
}"""


# ── Stufe 2: Qualifizierungsgespräch — Felder extrahieren ──
QUALIFY_SYSTEM_PROMPT = """Du bist ein Recruiting-Assistent. Du extrahierst strukturierte Daten aus einem Qualifizierungsgespräch mit einem Kandidaten.

REGELN:
- Extrahiere NUR was explizit im Gespräch gesagt wird. Erfinde NICHTS.
- Wenn eine Information nicht vorkommt, setze den Wert auf null.
- Gehalt immer als String (z.B. "55.000 €", "60.000-70.000 €")
- Kündigungsfrist als Text (z.B. "3 Monate", "6 Wochen zum Quartalsende")
- Home-Office Tage als Text (z.B. "2 bis 3 Tage", "kein Home-Office")
- Pendelbereitschaft als Text (z.B. "30 min", "40 km")
- ERP-Steckenpferd: Das ERP-System das der Kandidat am besten kennt (z.B. "DATEV", "SAP")
- ERP-Kenntnisse: ALLE genannten ERP-Systeme als Liste
- Branchen: Freitext, genau wie der Kandidat es beschrieben hat
- "Bereits beworben bei": Freitext, alle genannten Unternehmen

Antworte IMMER als JSON:
{
  "desired_positions": "Gewünschte Positionen (Freitext)" | null,
  "key_activities": "Tätigkeiten die voll umfänglich beherrscht werden" | null,
  "home_office_days": "Home-Office Tage" | null,
  "commute_max": "Pendelbereitschaft" | null,
  "commute_transport": "Auto" | "ÖPNV" | "Beides" | null,
  "erp_main": "ERP-Steckenpferd" | null,
  "erp_skills": ["ERP1", "ERP2"] | null,
  "employment_type": "Vollzeit" | "Teilzeit" | "Beides" | null,
  "part_time_hours": "Teilzeit-Stunden" | null,
  "salary": "Gehaltswunsch" | null,
  "notice_period": "Kündigungsfrist" | null,
  "preferred_industries": "Bevorzugte Branchen" | null,
  "avoided_industries": "Branchen vermeiden" | null,
  "open_office_ok": "ja" | "nein" | "egal" | null,
  "whatsapp_ok": true | false | null,
  "other_recruiters": "Details zu anderen Recruitern" | null,
  "exclusivity_agreed": true | false | null,
  "applied_at_companies_text": "Wo bereits beworben" | null,
  "willingness_to_change": "ja" | "nein" | null,
  "summary": "Zusammenfassung des Gesprächs (3-5 Sätze)",
  "key_facts": ["Fakt 1", "Fakt 2"]
}"""


# ── Stufe 2: Kurzer Call — nur Zusammenfassung ──
SHORT_CALL_SYSTEM_PROMPT = """Du bist ein Recruiting-Assistent. Du fasst einen kurzen Telefonanruf mit einem Kandidaten zusammen.

Extrahiere NUR was explizit gesagt wird. Erfinde NICHTS.

Antworte als JSON:
{
  "summary": "Zusammenfassung (2-3 Sätze)",
  "action_items": ["Aufgabe 1", "Aufgabe 2"] | null,
  "willingness_to_change": "ja" | "nein" | null,
  "salary": "Gehaltswunsch" | null,
  "notice_period": "Kündigungsfrist" | null
}"""


# ── Stufe 2: Kundengespräch — Zusammenfassung ──
CUSTOMER_CALL_SYSTEM_PROMPT = """Du bist ein Recruiting-Assistent. Du fasst ein Telefonat mit einem Kunden/Unternehmen zusammen.

Extrahiere NUR was explizit besprochen wird. Erfinde NICHTS.

Antworte als JSON:
{
  "summary": "Zusammenfassung (3-5 Sätze)",
  "company_name": "Name des Unternehmens" | null,
  "positions_discussed": ["Position 1", "Position 2"] | null,
  "action_items": ["Aufgabe 1"] | null,
  "key_facts": ["Fakt 1", "Fakt 2"]
}"""


# ── Kontakt-Call: Subtyp-Klassifizierung ──
CUSTOMER_SUBTYPE_SYSTEM_PROMPT = """Du bist ein Recruiting-Assistent. Du analysierst ein Akquise-/Vertriebsgespräch mit einem Kunden-Kontakt und bestimmst das Ergebnis des Gesprächs.

ERGEBNIS-TYPEN:
- "kein_bedarf": Der Kontakt hat aktuell keinen Personalbedarf. Kein Follow-up nötig.
- "follow_up": Der Kontakt hat potenziell Bedarf, aber es soll zu einem späteren Zeitpunkt nochmal telefoniert werden. Erkennbar an: "rufen Sie nächsten Monat an", "ab Q2 wird es interessant", "melden Sie sich im März".
- "job_quali": Der Kontakt beschreibt eine konkrete offene Stelle oder einen Personalbedarf. Erkennbar an: Stellenbeschreibung, Gehalt, Anforderungen, Team-Größe, ERP-System, Home-Office, Gleitzeit — typisch für ein ausführliches Qualifizierungsgespräch über eine offene Position.
- "sonstiges": Alles andere (Smalltalk, Rückfrage zu laufender Besetzung, Absage eines Kandidaten etc.)

Antworte NUR als JSON:
{
  "call_subtype": "kein_bedarf" | "follow_up" | "job_quali" | "sonstiges",
  "summary": "Zusammenfassung des Gesprächs (2-4 Sätze)",
  "confidence": 0.0-1.0,
  "follow_up_date": "YYYY-MM-DD" | null,
  "follow_up_reason": "Grund für Follow-up" | null
}

REGELN:
- follow_up_date nur bei "follow_up" setzen. Wenn kein konkretes Datum genannt wird, schätze basierend auf Kontext (z.B. "nächsten Monat" → erster Werktag nächsten Monats).
- Bei "job_quali" ist KEIN follow_up_date nötig (wird separat behandelt).
- Extrahiere NUR was explizit gesagt wird. Erfinde NICHTS."""


# ── Kontakt-Call: Job-Quali Felder extrahieren ──
JOB_QUALI_SYSTEM_PROMPT = """Du bist ein Recruiting-Assistent. Du extrahierst strukturierte Daten über eine offene Stelle aus einem Kundengespräch.

Der Personalberater hat mit einem Kunden telefoniert und eine offene Position qualifiziert. Extrahiere ALLE Details die im Gespräch genannt werden.

REGELN:
- Extrahiere NUR was explizit im Gespräch gesagt wird. Erfinde NICHTS.
- Wenn eine Information nicht vorkommt, setze den Wert auf null.
- Gehalt: salary_min und salary_max als Zahlen (Jahresbrutto). Bei "60.000 bis 70.000" → salary_min=60000, salary_max=70000.
- Wenn nur ein Gehalt genannt wird: salary_min und salary_max gleich setzen.
- title: Die offizielle Stellenbezeichnung, z.B. "Bilanzbuchhalter (m/w/d)". Immer mit (m/w/d) ergänzen.

Antworte IMMER als JSON:
{
  "title": "Stellenbezeichnung (m/w/d)" | null,
  "salary_min": 55000 | null,
  "salary_max": 65000 | null,
  "employment_type": "Vollzeit" | "Teilzeit" | "Vollzeit oder Teilzeit" | null,
  "location": "Stadt" | null,
  "requirements": "Anforderungen an den Kandidaten (Freitext)" | null,
  "description": "Stellenbeschreibung / Kontext (Freitext)" | null,
  "team_size": "Größe des Teams" | null,
  "erp_system": "Verwendetes ERP-System" | null,
  "home_office_days": "Home-Office Regelung" | null,
  "flextime": true | false | null,
  "core_hours": "Kernarbeitszeit" | null,
  "vacation_days": 30 | null,
  "overtime_handling": "Umgang mit Überstunden" | null,
  "open_office": "Einzelbüro" | "Großraum" | "Mix" | null,
  "english_requirements": "Englisch-Anforderungen" | null,
  "hiring_process_steps": "Bewerbungsprozess / Interview-Stufen" | null,
  "feedback_timeline": "Feedback-Zeitraum nach Vorstellung" | null,
  "digitalization_level": "Digitalisierungsgrad" | null,
  "older_candidates_ok": true | false | null,
  "desired_start_date": "Gewünschter Starttermin" | null,
  "interviews_started": true | false | null,
  "ideal_candidate_description": "Beschreibung des idealen Kandidaten" | null,
  "candidate_tasks": "Konkrete Aufgaben / Tätigkeiten" | null,
  "multiple_entities": true | false | null,
  "task_distribution": "Aufgabenverteilung (z.B. 70% HGB, 30% Controlling)" | null
}"""


class CallTranscriptionService:
    """Transkribiert Audio-Dateien und extrahiert strukturierte Daten."""

    MODEL = "gpt-4o"

    def __init__(self, db: AsyncSession):
        self.db = db
        self.api_key = settings.openai_api_key
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """HTTP-Client für OpenAI API."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url="https://api.openai.com/v1",
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=httpx.Timeout(300.0),  # 5 Min Timeout für Whisper (große Dateien)
            )
        return self._client

    async def close(self) -> None:
        """Schließt den HTTP-Client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def process_call(
        self,
        candidate_id: UUID,
        audio_url: str | None = None,
        audio_bytes: bytes | None = None,
        audio_filename: str = "recording.mp3",
        transcript_text: str | None = None,
    ) -> dict:
        """Verarbeitet einen Anruf: Transkription → Klassifizierung → Extraktion → DB-Update.

        Entweder audio_url ODER audio_bytes ODER transcript_text übergeben.
        - audio_url: URL zur Audio-Datei (wird heruntergeladen)
        - audio_bytes: Audio als Bytes
        - transcript_text: Bereits transkribierter Text (überspringt Whisper)

        Returns:
            Dict mit transcript, call_type, extracted_data, fields_updated, cost
        """
        if not self.api_key:
            return {"success": False, "error": "OpenAI API-Key nicht konfiguriert"}

        # Kandidat laden
        candidate = await self.db.get(Candidate, candidate_id)
        if not candidate:
            return {"success": False, "error": "Kandidat nicht gefunden"}

        total_cost = 0.0

        # ── Schritt 1: Transkription (Whisper) ──
        if transcript_text:
            transcript = transcript_text
            logger.info(f"Transkript direkt übergeben ({len(transcript)} Zeichen)")
        else:
            # Audio beschaffen
            audio_data = audio_bytes
            if audio_url and not audio_data:
                audio_data = await self._download_audio(audio_url)
                if not audio_data:
                    return {"success": False, "error": f"Audio-Download fehlgeschlagen: {audio_url}"}

            if not audio_data:
                return {"success": False, "error": "Weder audio_url, audio_bytes noch transcript_text übergeben"}

            transcript, whisper_cost = await self._transcribe_audio(audio_data, audio_filename)
            total_cost += whisper_cost
            if not transcript:
                return {"success": False, "error": "Whisper-Transkription fehlgeschlagen"}

        logger.info(f"Transkript: {len(transcript)} Zeichen für Kandidat {candidate.full_name}")

        # ── Schritt 2: Gesprächstyp klassifizieren ──
        call_type, classify_cost = await self._classify_call(transcript)
        total_cost += classify_cost
        logger.info(f"Gesprächstyp: {call_type} für Kandidat {candidate.full_name}")

        # ── Schritt 3: Felder extrahieren (je nach Typ) ──
        extracted, extract_cost = await self._extract_fields(transcript, call_type, candidate)
        total_cost += extract_cost

        # ── Schritt 4: DB-Update ──
        fields_updated = await self._apply_to_candidate(candidate, transcript, call_type, extracted)

        await self.db.flush()

        logger.info(
            f"Call verarbeitet: Typ={call_type}, Felder={len(fields_updated)}, "
            f"Kosten=${total_cost:.4f}, Kandidat={candidate.full_name}"
        )

        return {
            "success": True,
            "candidate_id": str(candidate_id),
            "candidate_name": candidate.full_name,
            "call_type": call_type,
            "transcript_length": len(transcript),
            "extracted_data": extracted,
            "fields_updated": fields_updated,
            "summary": extracted.get("summary", ""),
            "cost_usd": round(total_cost, 4),
        }

    # ────────────────────────────────────────────────────
    # Audio-Download
    # ────────────────────────────────────────────────────

    async def _download_audio(self, url: str) -> bytes | None:
        """Lädt Audio-Datei von URL herunter."""
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
                response = await client.get(url)
                response.raise_for_status()
                logger.info(f"Audio heruntergeladen: {len(response.content)} Bytes von {url}")
                return response.content
        except Exception as e:
            logger.error(f"Audio-Download fehlgeschlagen: {e}")
            return None

    # ────────────────────────────────────────────────────
    # Whisper Transkription
    # ────────────────────────────────────────────────────

    async def _transcribe_audio(self, audio_data: bytes, filename: str) -> tuple[str | None, float]:
        """Transkribiert Audio mit Whisper API. Returns (transcript, cost)."""
        try:
            client = await self._get_client()

            # Whisper braucht multipart/form-data
            files = {"file": (filename, audio_data, "audio/mpeg")}
            data = {
                "model": "whisper-1",
                "language": "de",
                "response_format": "text",
            }

            response = await client.post(
                "/audio/transcriptions",
                files=files,
                data=data,
                headers={"Authorization": f"Bearer {self.api_key}"},  # Override Content-Type
            )
            response.raise_for_status()

            transcript = response.text.strip()
            # Kosten schätzen: ~$0.006/Min, ca. 150 Wörter/Min
            word_count = len(transcript.split())
            estimated_minutes = max(1, word_count / 150)
            cost = estimated_minutes * WHISPER_PRICE_PER_MIN

            logger.info(f"Whisper: {word_count} Wörter, ~{estimated_minutes:.1f} Min, ${cost:.4f}")
            return transcript, cost

        except httpx.HTTPStatusError as e:
            logger.error(f"Whisper HTTP-Fehler: {e.response.status_code} - {e.response.text[:500]}")
            return None, 0.0
        except Exception as e:
            logger.exception(f"Whisper-Fehler: {e}")
            return None, 0.0

    # ────────────────────────────────────────────────────
    # GPT-4o-mini: Gesprächstyp klassifizieren
    # ────────────────────────────────────────────────────

    async def _classify_call(self, transcript: str) -> tuple[str, float]:
        """Klassifiziert den Gesprächstyp. Returns (call_type, cost)."""
        # Nur die ersten 3000 Zeichen für Klassifizierung (spart Tokens)
        snippet = transcript[:3000]
        if len(transcript) > 3000:
            snippet += "\n\n[... Transkript gekürzt für Klassifizierung ...]"

        result = await self._call_gpt(CLASSIFY_SYSTEM_PROMPT, snippet, max_tokens=150)
        if not result:
            return "sonstig", 0.0

        parsed, cost = result
        call_type = parsed.get("call_type", "sonstig")
        if call_type not in ("qualifizierung", "kurz", "kunde", "sonstig"):
            call_type = "sonstig"

        return call_type, cost

    # ────────────────────────────────────────────────────
    # GPT-4o-mini: Felder extrahieren
    # ────────────────────────────────────────────────────

    async def _extract_fields(self, transcript: str, call_type: str, candidate: Candidate) -> tuple[dict, float]:
        """Extrahiert Felder je nach Gesprächstyp. Returns (extracted_data, cost)."""
        if call_type == "qualifizierung":
            system_prompt = QUALIFY_SYSTEM_PROMPT
            max_tokens = 1200
        elif call_type == "kurz":
            system_prompt = SHORT_CALL_SYSTEM_PROMPT
            max_tokens = 400
        elif call_type == "kunde":
            system_prompt = CUSTOMER_CALL_SYSTEM_PROMPT
            max_tokens = 600
        else:
            # Sonstiges: nur Zusammenfassung
            system_prompt = SHORT_CALL_SYSTEM_PROMPT
            max_tokens = 400

        # Kontext-Info hinzufügen
        context = f"Kandidat: {candidate.full_name}"
        if candidate.current_position:
            context += f"\nAktuelle Position: {candidate.current_position}"
        if candidate.current_company:
            context += f"\nAktuelles Unternehmen: {candidate.current_company}"

        user_message = f"KONTEXT:\n{context}\n\nTRANSKRIPTION DES GESPRÄCHS:\n{transcript}"

        result = await self._call_gpt(system_prompt, user_message, max_tokens=max_tokens)
        if not result:
            return {"summary": "KI-Analyse fehlgeschlagen"}, 0.0

        parsed, cost = result
        return parsed, cost

    # ────────────────────────────────────────────────────
    # GPT API Call (shared)
    # ────────────────────────────────────────────────────

    async def _call_gpt(self, system_prompt: str, user_message: str, max_tokens: int = 800) -> tuple[dict, float] | None:
        """GPT-4o-mini aufrufen und JSON parsen. Returns (parsed_json, cost) oder None."""
        for attempt in range(3):
            try:
                client = await self._get_client()
                response = await client.post(
                    "/chat/completions",
                    json={
                        "model": self.MODEL,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_message},
                        ],
                        "temperature": 0.1,
                        "max_tokens": max_tokens,
                        "response_format": {"type": "json_object"},
                    },
                    headers={"Content-Type": "application/json"},
                )
                response.raise_for_status()
                result = response.json()

                content = result["choices"][0]["message"]["content"]
                parsed = json.loads(content)

                # Kosten berechnen
                usage = result.get("usage", {})
                input_tokens = usage.get("prompt_tokens", 0)
                output_tokens = usage.get("completion_tokens", 0)
                cost = (input_tokens / 1_000_000) * GPT_INPUT_PER_1M + (output_tokens / 1_000_000) * GPT_OUTPUT_PER_1M

                return parsed, cost

            except httpx.TimeoutException:
                logger.warning(f"GPT Timeout (Versuch {attempt + 1}/3)")
                if attempt == 2:
                    return None
            except json.JSONDecodeError as e:
                logger.error(f"GPT JSON-Fehler: {e}")
                return None
            except httpx.HTTPStatusError as e:
                logger.error(f"GPT HTTP-Fehler: {e.response.status_code} - {e.response.text[:300]}")
                return None
            except Exception as e:
                logger.exception(f"GPT Fehler: {e}")
                return None

        return None

    # ────────────────────────────────────────────────────
    # DB-Update
    # ────────────────────────────────────────────────────

    async def _apply_to_candidate(
        self,
        candidate: Candidate,
        transcript: str,
        call_type: str,
        extracted: dict,
    ) -> list[str]:
        """Wendet extrahierte Daten auf Kandidaten an. Returns Liste der aktualisierten Felder."""
        updated = []

        # Immer: Transkript, Typ, Datum
        candidate.call_transcript = transcript
        updated.append("call_transcript")

        candidate.call_type = call_type
        updated.append("call_type")

        candidate.call_date = datetime.now(timezone.utc)
        updated.append("call_date")

        candidate.last_contact = datetime.now(timezone.utc)
        updated.append("last_contact")

        # Zusammenfassung
        summary = extracted.get("summary")
        if summary:
            candidate.call_summary = summary
            updated.append("call_summary")

        # Qualifizierungsgespräch: Alle Felder
        if call_type == "qualifizierung":
            updated.extend(self._apply_qualification_fields(candidate, extracted))

        # Kurzer Call / Sonstiges: Nur Basis-Felder
        elif call_type in ("kurz", "sonstig"):
            updated.extend(self._apply_basic_fields(candidate, extracted))

        # Kandidat-Notizen: Summary anhängen
        if summary:
            type_labels = {
                "qualifizierung": "Qualifizierungsgespräch",
                "kurz": "Kurzer Call",
                "kunde": "Kundengespräch",
                "sonstig": "Sonstiges Gespräch",
            }
            timestamp = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M")
            note_entry = f"--- {timestamp} | {type_labels.get(call_type, 'Gespräch')} (KI) ---\n{summary}"

            key_facts = extracted.get("key_facts")
            if key_facts and isinstance(key_facts, list):
                note_entry += "\nFakten: " + " | ".join(key_facts)

            if candidate.candidate_notes:
                candidate.candidate_notes = candidate.candidate_notes + "\n\n" + note_entry
            else:
                candidate.candidate_notes = note_entry
            updated.append("candidate_notes")

        candidate.updated_at = datetime.now(timezone.utc)

        return updated

    def _apply_qualification_fields(self, candidate: Candidate, data: dict) -> list[str]:
        """Wendet Qualifizierungsfelder an."""
        updated = []

        # String-Felder: nur setzen wenn Wert vorhanden
        str_fields = [
            "desired_positions", "key_activities", "home_office_days",
            "commute_max", "commute_transport", "erp_main",
            "employment_type", "part_time_hours",
            "preferred_industries", "avoided_industries",
            "open_office_ok", "other_recruiters",
            "applied_at_companies_text",
            "salary", "notice_period",
        ]
        for field in str_fields:
            val = data.get(field)
            if val and isinstance(val, str) and val.strip():
                setattr(candidate, field, val.strip())
                updated.append(field)

        # Boolean-Felder
        for field in ("whatsapp_ok", "exclusivity_agreed"):
            val = data.get(field)
            if val is not None and isinstance(val, bool):
                setattr(candidate, field, val)
                updated.append(field)

        # Wechselbereitschaft
        willingness = data.get("willingness_to_change")
        if willingness in ("ja", "nein"):
            candidate.willingness_to_change = willingness
            updated.append("willingness_to_change")

        # ERP-Kenntnisse (merge mit bestehenden)
        erp_new = data.get("erp_skills")
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

        return updated

    def _apply_basic_fields(self, candidate: Candidate, data: dict) -> list[str]:
        """Wendet Basis-Felder an (kurzer Call / sonstiges)."""
        updated = []

        willingness = data.get("willingness_to_change")
        if willingness in ("ja", "nein"):
            candidate.willingness_to_change = willingness
            updated.append("willingness_to_change")

        salary = data.get("salary")
        if salary and isinstance(salary, str) and salary.strip():
            candidate.salary = salary.strip()
            updated.append("salary")

        notice = data.get("notice_period")
        if notice and isinstance(notice, str) and notice.strip():
            candidate.notice_period = notice.strip()
            updated.append("notice_period")

        return updated

    # ────────────────────────────────────────────────────
    # Kontakt-Call Verarbeitung (Akquise/Vertrieb)
    # ────────────────────────────────────────────────────

    async def process_contact_call(
        self,
        transcript: str,
        contact_name: str,
        company_name: str,
    ) -> dict:
        """Verarbeitet einen Kontakt-/Kundencall: Subtyp klassifizieren + ggf. Job-Quali extrahieren.

        Returns:
            Dict mit: subtype, summary, confidence, follow_up_date, follow_up_reason,
                      job_data (nur bei job_quali), cost_usd
        """
        if not self.api_key:
            return {"success": False, "error": "OpenAI API-Key nicht konfiguriert"}

        total_cost = 0.0

        # ── Stufe 1: Subtyp klassifizieren ──
        context = f"Ansprechpartner: {contact_name}\nUnternehmen: {company_name}"
        user_message = f"KONTEXT:\n{context}\n\nTRANSKRIPTION DES GESPRÄCHS:\n{transcript}"

        result = await self._call_gpt(CUSTOMER_SUBTYPE_SYSTEM_PROMPT, user_message, max_tokens=400)
        if not result:
            return {
                "success": False,
                "error": "GPT-Klassifizierung fehlgeschlagen",
                "subtype": "sonstiges",
                "summary": "KI-Analyse fehlgeschlagen",
            }

        parsed, cost = result
        total_cost += cost

        subtype = parsed.get("call_subtype", "sonstiges")
        if subtype not in ("kein_bedarf", "follow_up", "job_quali", "sonstiges"):
            subtype = "sonstiges"

        response = {
            "success": True,
            "subtype": subtype,
            "summary": parsed.get("summary", ""),
            "confidence": parsed.get("confidence", 0.0),
            "follow_up_date": parsed.get("follow_up_date"),
            "follow_up_reason": parsed.get("follow_up_reason"),
            "job_data": None,
            "cost_usd": 0.0,
        }

        # ── Stufe 2: Bei job_quali → strukturierte Felder extrahieren ──
        if subtype == "job_quali":
            job_context = f"Ansprechpartner: {contact_name}\nUnternehmen: {company_name}"
            job_user_msg = f"KONTEXT:\n{job_context}\n\nTRANSKRIPTION DES GESPRÄCHS:\n{transcript}"

            job_result = await self._call_gpt(JOB_QUALI_SYSTEM_PROMPT, job_user_msg, max_tokens=1500)
            if job_result:
                job_parsed, job_cost = job_result
                total_cost += job_cost
                response["job_data"] = job_parsed

                logger.info(
                    f"Job-Quali extrahiert: '{job_parsed.get('title', '?')}' "
                    f"bei {company_name} ({contact_name})"
                )
            else:
                logger.warning(f"Job-Quali-Extraktion fehlgeschlagen fuer {company_name}")

        response["cost_usd"] = round(total_cost, 4)

        logger.info(
            f"Kontakt-Call verarbeitet: subtype={subtype}, "
            f"Kosten=${total_cost:.4f}, Kontakt={contact_name} ({company_name})"
        )

        return response
