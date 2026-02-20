"""Telegram Intent-Klassifikation und Whisper-Transkription.

Verwendet GPT-4o-mini fuer Intent-Erkennung aus Freitext/Sprachnachrichten.
Verwendet OpenAI Whisper fuer Sprachnachricht -> Text.
"""

import json
import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# ── Intent-Klassifikation Prompt ──────────────────────────────────
INTENT_SYSTEM_PROMPT = """Du bist ein Intent-Klassifikator fuer einen Recruiting-Assistenten (Telegram Bot).

Klassifiziere die Nachricht des Users in GENAU EINEN der folgenden Intents:

- task_list: User will seine Aufgaben sehen ("Was steht an?", "Aufgaben heute", "Todos")
- task_create: User will eine neue Aufgabe erstellen ("Erstelle Aufgabe...", "Erinnerung: ...", "Morgen um 14 Uhr...")
- task_complete: User hat eine Aufgabe erledigt ("Erledigt: ...", "Done: ...")
- candidate_search: User sucht einen Kandidaten ("Finde Anna Schmidt", "Kandidat Mueller", "Suche...")
- briefing: User will ein Update/Briefing ("Was gibt es Neues?", "Ueberblick", "Status")
- call_log: User berichtet von einem Telefonat ("Gerade mit Firma X telefoniert...", "Call mit...")
- email_send: User will eine Email senden ("Schreibe Email an...", "Mail an...")
- unknown: Keiner der obigen Intents passt

Extrahiere zusaetzlich relevante Entitaeten:
- name: Name einer Person/Firma (falls erwaehnt)
- date: Datum (falls erwaehnt, Format: YYYY-MM-DD)
- time: Uhrzeit (falls erwaehnt, Format: HH:MM)
- title: Titel/Beschreibung einer Aufgabe (falls erwaehnt)
- priority: Prioritaet (dringend/wichtig/normal, falls erwaehnt)

Antworte NUR mit JSON:
{"intent": "...", "entities": {...}, "confidence": 0.0-1.0}"""


async def classify_intent(text: str) -> dict:
    """Klassifiziert eine Textnachricht in einen Intent mit Entitaeten.

    Returns:
        {"intent": str, "entities": dict, "confidence": float}
    """
    if not settings.openai_api_key:
        logger.warning("OpenAI API Key nicht konfiguriert - Intent-Klassifikation nicht moeglich")
        return {"intent": "unknown", "entities": {}, "confidence": 0.0}

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
                        {"role": "system", "content": INTENT_SYSTEM_PROMPT},
                        {"role": "user", "content": text},
                    ],
                    "temperature": 0.0,
                    "max_tokens": 200,
                },
            )
            response.raise_for_status()
            data = response.json()

        content = data["choices"][0]["message"]["content"].strip()
        # Parse JSON response
        result = json.loads(content)
        logger.info(f"Intent klassifiziert: {result.get('intent')} (confidence: {result.get('confidence')})")
        return result

    except json.JSONDecodeError:
        logger.warning(f"GPT hat kein valides JSON zurueckgegeben: {content[:200]}")
        return {"intent": "unknown", "entities": {}, "confidence": 0.0}
    except Exception as e:
        logger.error(f"Intent-Klassifikation fehlgeschlagen: {e}")
        return {"intent": "unknown", "entities": {}, "confidence": 0.0}


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
