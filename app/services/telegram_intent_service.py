"""Telegram Intent-Klassifikation und Whisper-Transkription.

Verwendet GPT-4o-mini fuer Intent-Erkennung aus Freitext/Sprachnachrichten.
Verwendet OpenAI Whisper fuer Sprachnachricht -> Text.
"""

import json
import logging
from datetime import datetime, timedelta

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# ── Intent-Klassifikation Prompt ──────────────────────────────────
INTENT_SYSTEM_PROMPT_TEMPLATE = """Du bist ein Intent-Klassifikator fuer einen Recruiting-Assistenten (Telegram Bot).

HEUTE ist {today} ({weekday}). Verwende dieses Datum als Referenz fuer relative Angaben wie "morgen", "Samstag", "naechste Woche" etc.

Verfuegbare Intents:
- task_list: User will seine Aufgaben sehen ("Was steht an?", "Aufgaben heute", "Todos")
- task_create: User will eine neue Aufgabe erstellen ("Erstelle Aufgabe...", "Erinnerung: ...", "Morgen um 14 Uhr...")
- task_complete: User hat eine Aufgabe erledigt ("Erledigt: ...", "Done: ...")
- candidate_search: User sucht einen Kandidaten ("Finde Anna Schmidt", "Kandidat Mueller", "Suche...")
- briefing: User will ein Update/Briefing ("Was gibt es Neues?", "Ueberblick", "Status")
- call_log: User berichtet von einem Telefonat ("Gerade mit Firma X telefoniert...", "Call mit...")
- email_send: User will eine Email senden ("Schreibe Email an...", "Mail an...") — NUR wenn KEIN Termin/Kalender erwaehnt wird
- calendar_create: User will einen Termin erstellen ODER eine Terminbestaetigung senden ("Erstelle Termin mit...", "Terminiere...", "Meeting am...", "Terminbestaetigung senden", "Termin in Kalender eintragen", "sende Terminbestaetigung an...")
- unknown: Keiner der obigen Intents passt

Extrahiere fuer jeden Intent relevante Entitaeten:
- name: Name einer Person/Firma. PFLICHT bei email_send, calendar_create, task_create.
- date: Datum im Format YYYY-MM-DD (berechne aus heutigem Datum)
- time: Uhrzeit im Format HH:MM
- title: Aufgaben-Titel (NICHT der Personenname!)
- priority: dringend/wichtig/normal
- instruction: Bei email_send — Anweisung/Inhalt der Email
- duration: Bei calendar_create — Dauer in Minuten (Default: 30)

MULTI-INTENT: Wenn die Nachricht mehrere Aufgaben enthaelt, erkenne bis zu 2 Intents.
Gib den ERSTEN als "intent"+"entities" zurueck, weitere in "secondary" als Array.

BEISPIEL Multi-Intent:
Nachricht: "Schreibe Email an Mueller und erstelle Aufgabe fuer den 10. März: Feedback einholen"
Antwort:
{{"intent": "email_send", "entities": {{"name": "Mueller", "instruction": "..."}},
  "secondary": [{{"intent": "task_create", "entities": {{"name": "Mueller", "date": "2026-03-10", "title": "Feedback einholen"}}}}],
  "confidence": 0.95}}

BEISPIEL Single-Intent Email:
{{"intent": "email_send", "entities": {{"name": "Sandra Kuhse", "instruction": "..."}}, "secondary": [], "confidence": 0.95}}

BEISPIEL Terminbestaetigung (= calendar_create, NICHT email_send!):
Nachricht: "Sende eine Terminbestaetigung an Antje Lindner fuer Mittwoch um 17:30 und trage den Termin in meinen Kalender ein"
Antwort: {{"intent": "calendar_create", "entities": {{"name": "Antje Lindner", "date": "2026-03-04", "time": "17:30", "duration": 60}}, "secondary": [], "confidence": 0.95}}

WICHTIG: "secondary" ist immer ein Array (leer wenn nur ein Intent). Max. 2 Intents gesamt.
WICHTIG: Bei task_create ist der Name der Person NICHT der Titel. "Mueller anrufen" -> name="Mueller", title="anrufen".

Antworte IMMER als JSON-Objekt mit den Feldern: intent, entities, secondary, confidence."""


WEEKDAY_NAMES = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]


def _build_intent_prompt() -> str:
    """Baut den Intent-Prompt mit aktuellem Datum."""
    now = datetime.now()
    return INTENT_SYSTEM_PROMPT_TEMPLATE.format(
        today=now.strftime("%Y-%m-%d"),
        weekday=WEEKDAY_NAMES[now.weekday()],
    )


async def classify_intent(text: str) -> dict:
    """Klassifiziert eine Textnachricht in einen Intent mit Entitaeten.

    Returns:
        {"intent": str, "entities": dict, "confidence": float}
    """
    if not settings.openai_api_key:
        logger.warning("OpenAI API Key nicht konfiguriert - Intent-Klassifikation nicht moeglich")
        return {"intent": "unknown", "entities": {}, "confidence": 0.0, "_error": "no_api_key"}

    content = ""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.openai_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "gpt-4o-mini",
                    "messages": [
                        {"role": "system", "content": _build_intent_prompt()},
                        {"role": "user", "content": text},
                    ],
                    "temperature": 0.0,
                    "max_tokens": 500,
                    "response_format": {"type": "json_object"},
                },
            )
            if response.status_code == 429:
                logger.error("OpenAI Rate-Limit oder Quota erschoepft (429)")
                return {"intent": "unknown", "entities": {}, "confidence": 0.0, "_error": "openai_429"}
            if response.status_code == 401:
                logger.error("OpenAI API Key ungueltig (401)")
                return {"intent": "unknown", "entities": {}, "confidence": 0.0, "_error": "openai_401"}
            if response.status_code == 400:
                body = response.text[:300]
                logger.error(f"OpenAI Bad Request (400): {body}")
                return {"intent": "unknown", "entities": {}, "confidence": 0.0, "_error": f"openai_400: {body[:80]}"}
            response.raise_for_status()
            data = response.json()

        content = data["choices"][0]["message"]["content"].strip()
        result = json.loads(content)
        logger.info(f"Intent klassifiziert: {result.get('intent')} (confidence: {result.get('confidence')})")
        return result

    except json.JSONDecodeError:
        logger.warning(f"GPT hat kein valides JSON zurueckgegeben: {content[:200]}")
        return {"intent": "unknown", "entities": {}, "confidence": 0.0, "_error": "json_error"}
    except Exception as e:
        logger.error(f"Intent-Klassifikation fehlgeschlagen: {e}")
        return {"intent": "unknown", "entities": {}, "confidence": 0.0, "_error": str(e)[:100]}


async def transcribe_voice(audio_bytes: bytes, filename: str = "voice.ogg") -> str:
    """Transkribiert Audio via OpenAI Whisper API.

    Args:
        audio_bytes: Audio-Daten (OGG/MP3/WAV)
        filename: Dateiname mit Extension fuer MIME-Type Erkennung

    Returns:
        Transkribierter Text
    """
    if not settings.openai_api_key:
        logger.warning("OpenAI API Key nicht konfiguriert - Whisper nicht verfuegbar")
        return ""

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={
                    "Authorization": f"Bearer {settings.openai_api_key}",
                },
                data={
                    "model": "whisper-1",
                    "language": "de",
                },
                files={
                    "file": (filename, audio_bytes, "audio/ogg"),
                },
            )
            response.raise_for_status()
            data = response.json()

        text = data.get("text", "").strip()
        logger.info(f"Whisper-Transkription erfolgreich: {len(text)} Zeichen")
        return text

    except Exception as e:
        logger.error(f"Whisper-Transkription fehlgeschlagen: {e}")
        return ""
