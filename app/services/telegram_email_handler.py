"""Telegram Email Handler — Email-Versand via Telegram Bot.

Ermoeglicht das Senden beliebiger Emails per Telegram-Nachricht.
GPT-4o-mini schreibt die Email basierend auf der Anweisung.
Versand ueber Microsoft Graph (hamdard@sincirus.com).
"""

import json
import logging
from datetime import datetime

import httpx
from sqlalchemy import or_, select

from app.config import settings

logger = logging.getLogger(__name__)

# ── GPT System-Prompt fuer Email-Erstellung ──
EMAIL_WRITER_SYSTEM_PROMPT = """Du bist der persoenliche Email-Assistent von Milad Hamdard, Geschaeftsfuehrer der Sincirus GmbH (Personalberatung im Finance-Bereich).

Du schreibst professionelle, freundliche Emails auf Deutsch in Milads Namen. Der Ton ist:
- Professionell aber persoenlich und warmherzig
- IMMER Siezen ("Sie/Ihnen/Ihr") — ausser Milad sagt explizit "schreib in Du-Form"
- Direkt und klar, keine Floskeln
- Kurz und praegnant — keine unnoetig langen Emails

REGELN:
1. Schreibe die Email so, als wuerde Milad sie selbst schreiben
2. IMMER "Sie" verwenden — egal ob Kandidat oder Firmenkontakt
3. Verwende KEINE Emojis in der Email
4. Beende JEDE Email mit:
   Mit freundlichen Gruessen
   Milad Hamdard
   Sincirus GmbH
   Tel: +49 173 3665239
5. Wenn der User eine bestimmte Information erwaehnt (Termin, Ort, Zeit), baue sie EXAKT ein
6. Wenn der User keinen Betreff nennt, erstelle einen passenden kurzen Betreff

Antworte IMMER als JSON:
{
  "subject": "Betreff der Email",
  "body_html": "<p>HTML-formatierter Email-Text</p>",
  "tone": "formal" | "informal"
}

WICHTIG: body_html muss valides HTML sein mit <p>, <br>, <strong> Tags.
Verwende <br><br> fuer Absaetze. Kein <style> oder CSS noetig."""


async def handle_email_send(chat_id: str, text: str, entities: dict) -> None:
    """Verarbeitet eine Email-Sende-Anfrage aus Telegram.

    Flow:
    1. Empfaenger per Name im System suchen (Kandidat oder Kontakt)
    2. Email-Adresse pruefen
    3. GPT schreibt die Email basierend auf der Anweisung
    4. Vorschau an Telegram senden (mit Senden/Abbrechen Buttons)
    5. Bei Bestaetigung: Versand via Microsoft Graph
    """
    try:
        from app.services.telegram_bot_service import send_message

        name = entities.get("name", "")
        if not name:
            await send_message(
                "Ich brauche den Empfaenger-Namen in der Nachricht.\n\n"
                "Beispiel:\n"
                "<i>Schreib eine Email an Max Mueller und sag ihm dass der Termin am Freitag ist</i>\n\n"
                "Der Name muss immer dabei stehen — ich suche dann automatisch die Email-Adresse im System.",
                chat_id=chat_id,
            )
            return

        # ── Schritt 1: Empfaenger suchen ──
        recipient = await _find_recipient(name)
        if not recipient:
            await send_message(
                f"Konnte <b>{name}</b> nicht im System finden.\n"
                "Bitte den vollen Namen verwenden.",
                chat_id=chat_id,
            )
            return

        if not recipient.get("email"):
            await send_message(
                f"<b>{recipient['name']}</b> hat keine Email-Adresse hinterlegt.\n"
                f"Typ: {recipient['type']}",
                chat_id=chat_id,
            )
            return

        await send_message(
            f"Schreibe Email an <b>{recipient['name']}</b> ({recipient['email']})...",
            chat_id=chat_id,
        )

        # ── Schritt 2: GPT schreibt die Email ──
        email_data = await _generate_email(text, recipient)
        if not email_data:
            await send_message(
                "Konnte die Email nicht erstellen. Bitte erneut versuchen.",
                chat_id=chat_id,
            )
            return

        subject = email_data.get("subject", "Nachricht von Sincirus GmbH")
        body_html = email_data.get("body_html", "")

        # Plaintext-Vorschau fuer Telegram (HTML-Tags entfernen)
        import re
        preview_text = re.sub(r"<[^>]+>", "", body_html)
        preview_text = preview_text.replace("&nbsp;", " ").strip()

        # ── Schritt 3: Direkt senden (kein Confirm-Dialog noetig, Milad ist einziger User) ──
        from app.services.email_service import MicrosoftGraphClient

        result = await MicrosoftGraphClient.send_email(
            to_email=recipient["email"],
            subject=subject,
            body_html=body_html,
        )

        if result.get("success"):
            await send_message(
                f"<b>Email gesendet!</b>\n\n"
                f"An: {recipient['name']} ({recipient['email']})\n"
                f"Betreff: <b>{subject}</b>\n\n"
                f"<i>{preview_text[:500]}</i>",
                chat_id=chat_id,
            )
            logger.info(f"Email gesendet: {recipient['email']} — {subject}")
        else:
            error = result.get("error", "Unbekannter Fehler")
            await send_message(
                f"Email-Versand fehlgeschlagen:\n{error[:300]}",
                chat_id=chat_id,
            )

    except Exception as e:
        logger.error(f"Email-Handler fehlgeschlagen: {e}", exc_info=True)
        from app.services.telegram_bot_service import send_message
        await send_message(
            f"Fehler beim Email-Versand: {str(e)[:200]}",
            chat_id=chat_id,
        )


async def _find_recipient(name: str) -> dict | None:
    """Sucht einen Empfaenger per Name in Kandidaten und Kontakten.

    Returns: {"name": str, "email": str, "type": "candidate"|"contact", "id": UUID}
    """
    try:
        from app.database import async_session_maker
        from app.models.candidate import Candidate
        from app.models.company_contact import CompanyContact

        search = f"%{name}%"

        # 1. Kandidat suchen
        async with async_session_maker() as db:
            result = await db.execute(
                select(Candidate)
                .where(
                    or_(
                        Candidate.last_name.ilike(search),
                        (Candidate.first_name + " " + Candidate.last_name).ilike(search),
                        Candidate.first_name.ilike(search),
                    )
                )
                .limit(3)
            )
            candidates = result.scalars().all()

            if len(candidates) == 1:
                c = candidates[0]
                full_name = f"{c.first_name or ''} {c.last_name or ''}".strip()
                return {
                    "name": full_name,
                    "email": c.email,
                    "type": "candidate",
                    "id": str(c.id),
                    "first_name": c.first_name or "",
                }

        # 2. Kontakt suchen
        async with async_session_maker() as db:
            result = await db.execute(
                select(CompanyContact)
                .where(CompanyContact.name.ilike(search))
                .limit(3)
            )
            contacts = result.scalars().all()

            if len(contacts) == 1:
                ct = contacts[0]
                return {
                    "name": ct.name,
                    "email": ct.email,
                    "type": "contact",
                    "id": str(ct.id),
                    "first_name": ct.name.split()[0] if ct.name else "",
                }

        return None

    except Exception as e:
        logger.error(f"Empfaengersuche fehlgeschlagen: {e}")
        return None


async def _generate_email(user_instruction: str, recipient: dict) -> dict | None:
    """Generiert eine Email via GPT-4o-mini basierend auf der Anweisung.

    Returns: {"subject": str, "body_html": str}
    """
    if not settings.openai_api_key:
        logger.warning("OpenAI API Key nicht konfiguriert")
        return None

    today = datetime.now().strftime("%d.%m.%Y")
    recipient_context = (
        f"Empfaenger: {recipient['name']} ({recipient['type']})\n"
        f"Vorname: {recipient.get('first_name', '')}\n"
        f"Email: {recipient['email']}\n"
        f"Heutiges Datum: {today}"
    )

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.openai_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "gpt-4o",
                    "temperature": 0.3,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": EMAIL_WRITER_SYSTEM_PROMPT},
                        {
                            "role": "user",
                            "content": (
                                f"{recipient_context}\n\n"
                                f"Anweisung von Milad:\n{user_instruction}"
                            ),
                        },
                    ],
                },
            )
            response.raise_for_status()
            data = response.json()

        content = data["choices"][0]["message"]["content"]
        result = json.loads(content) if isinstance(content, str) else content
        logger.info(f"Email generiert: Betreff='{result.get('subject', '')[:60]}'")
        return result

    except Exception as e:
        logger.error(f"Email-Generierung fehlgeschlagen: {e}")
        return None
