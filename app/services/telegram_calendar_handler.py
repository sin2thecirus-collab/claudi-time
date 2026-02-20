"""Telegram Calendar Handler — Kalendereintraege + Erinnerungen via Telegram Bot.

Erstellt Termine im Outlook-Kalender von hamdard@sincirus.com
via Microsoft Graph Calendar API.

GPT-4o schreibt Betreff + Einladungstext dynamisch basierend auf der Anweisung.
Erinnerungs-Service: Sendet am Vorabend freundliche Erinnerungs-Emails an Teilnehmer.

Flow: Anweisung -> GPT generiert Betreff + Text -> Vorschau -> Bestaetigung -> Erstellung + Activity-Log
"""

import json
import logging
import re
from datetime import datetime, timedelta

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# ── Zwischenspeicher (In-Memory, Single-User) ──
_pending_calendar: dict[str, dict] = {}
_pending_calendar_pick: dict[str, dict] = {}

# ── GPT System-Prompt fuer Kalendereinladungs-Text ──
CALENDAR_INVITE_SYSTEM_PROMPT = """Du bist der persoenliche Kalender-Assistent von Milad Hamdard, Geschaeftsfuehrer der Sincirus GmbH (Personalberatung im Finance-Bereich).

Du schreibst professionelle Einladungstexte fuer Kalendertermine auf Deutsch in Milads Namen. Der Ton ist:
- Professionell aber persoenlich und warmherzig
- IMMER Siezen ("Sie/Ihnen/Ihr") — ausser Milad sagt explizit "schreib in Du-Form"
- Kurz und praegnant — kein Roman, aber alle relevanten Infos

REGELN:
1. Schreibe den Text so, als wuerde Milad ihn selbst schreiben
2. IMMER "Sie" verwenden
3. Verwende die ANREDE aus den Empfaengerdaten: Wenn "Herr" -> "Hallo Herr [Nachname]", wenn "Frau" -> "Hallo Frau [Nachname]". Wenn keine Anrede vorhanden: "Hallo [Vorname] [Nachname]"
4. Verwende KEINE Emojis
5. Der Text soll den ZWECK des Termins erwaehnen (worauf sich der Einladungstext bezieht)
6. Wenn Milad bestimmte Details erwaehnt (Thema, Stelle, Position), baue sie ein
7. Beende den Text mit "Mit freundlichen Gruessen" — KEINE Signatur (Name, Firma, Tel etc.), die wird automatisch angehaengt!
8. Der Text soll kurz sein: 3-6 Saetze maximal

BETREFF-REGELN — WICHTIG:
- Erstelle einen passenden Betreff basierend auf dem Kontext der Anweisung
- "Telefonischer Austausch | [Name] & Milad Hamdard" NUR bei Erstgespraechen (wenn sich die Personen noch nicht kennen)
- Bei Follow-ups, Stellenbesprechungen, Feedback-Terminen etc.: einen passenden Betreff waehlen
- Beispiele:
  - Erstgespraech: "Telefonischer Austausch | Max Mueller & Milad Hamdard"
  - Stellenbesprechung: "Stellenbesprechung: FiBu-Position Hamburg"
  - Follow-up: "Ruecksprache zu Ihrer Bewerbung"
  - Kundentermin: "Abstimmung offene Positionen | Sincirus & [Firma]"
  - Wenn unklar: einen kurzen, passenden Betreff waehlen

Antworte IMMER als JSON:
{
  "subject": "Passender Betreff",
  "body_html": "<p>HTML-formatierter Einladungstext</p>"
}

WICHTIG: body_html muss valides HTML sein mit <p>, <br> Tags.
WICHTIG: body_html endet mit "Mit freundlichen Gruessen" — KEIN Name, KEINE Firma, KEINE Telefonnummer danach!"""


# ── GPT System-Prompt fuer Termin-Erinnerungen ──
CALENDAR_REMINDER_SYSTEM_PROMPT = """Du bist der persoenliche Assistent von Milad Hamdard, Geschaeftsfuehrer der Sincirus GmbH (Personalberatung im Finance-Bereich).

Du schreibst kurze, freundliche Erinnerungs-Emails fuer morgen anstehende Termine. Der Ton ist:
- Freundlich und persoenlich
- IMMER Siezen ("Sie/Ihnen/Ihr")
- Sehr kurz — maximal 3-4 Saetze

REGELN:
1. Beginne mit "Hallo Herr [Nachname]" oder "Hallo Frau [Nachname]" (basierend auf Anrede)
2. Erinnere freundlich an den Termin morgen (Datum + Uhrzeit)
3. Erwaehne kurz das Thema (aus dem Betreff ableitbar)
4. Beende mit "Mit freundlichen Gruessen" — KEINE Signatur danach!
5. Verwende KEINE Emojis
6. Wenn CRM-Infos verfuegbar sind (Position, Firma etc.), kannst du optional darauf Bezug nehmen

Antworte IMMER als JSON:
{
  "subject": "Erinnerung: [Betreff des Termins]",
  "body_html": "<p>HTML-formatierter Erinnerungstext</p>"
}

WICHTIG: body_html muss valides HTML sein mit <p>, <br> Tags."""


async def handle_calendar_create(chat_id: str, text: str, entities: dict) -> None:
    """Erstellt einen Kalender-Termin basierend auf Intent-Entities.

    Flow:
    1. Empfaenger per Name suchen (optional)
    2. Termin-Details aus Entities extrahieren
    3. GPT generiert Einladungstext
    4. Vorschau zeigen mit Buttons (Erstellen / Abbrechen)
    5. Bei Bestaetigung: Termin via Microsoft Graph erstellen + Activity loggen
    """
    try:
        from app.services.telegram_bot_service import send_message

        name = entities.get("name", "")
        date_str = entities.get("date", "")
        time_str = entities.get("time", "")
        duration = entities.get("duration", 30)  # Default 30 Minuten

        if not date_str or not time_str:
            await send_message(
                "Ich brauche mindestens ein Datum und eine Uhrzeit.\n\n"
                "Beispiel:\n"
                "<i>Erstelle einen Termin mit Max Mueller am Freitag um 14:00</i>",
                chat_id=chat_id,
            )
            return

        # Datum + Zeit parsen
        try:
            start_dt = datetime.fromisoformat(f"{date_str}T{time_str}:00")
        except ValueError:
            await send_message(
                f"Konnte Datum/Uhrzeit nicht parsen: {date_str} {time_str}\n"
                "Bitte im Format angeben: Datum + Uhrzeit",
                chat_id=chat_id,
            )
            return

        # ── Empfaenger suchen (optional) ──
        recipient = None
        if name:
            from app.services.telegram_person_search import (
                build_disambiguation_buttons,
                build_disambiguation_text,
                search_persons,
            )

            matches = await search_persons(name)
            # Fuer Kalender: Kandidaten + Kontakte (keine Unternehmen)
            matches = [m for m in matches if m["type"] in ("candidate", "contact")]

            if len(matches) > 1:
                _pending_calendar_pick[chat_id] = {
                    "matches": matches,
                    "date_str": date_str,
                    "time_str": time_str,
                    "duration": duration,
                    "original_text": text,
                }
                await send_message(
                    build_disambiguation_text(matches, name),
                    chat_id=chat_id,
                    reply_markup={"inline_keyboard": build_disambiguation_buttons(matches, "cal_pick_")},
                )
                return

            if len(matches) == 1:
                recipient = matches[0]

        # GPT-Einladungstext generieren + Vorschau anzeigen
        await _generate_and_preview(chat_id, start_dt, duration, recipient, text)

    except Exception as e:
        logger.error(f"Calendar-Handler fehlgeschlagen: {e}", exc_info=True)
        from app.services.telegram_bot_service import send_message
        await send_message(
            f"Fehler beim Termin-Erstellen: {str(e)[:200]}",
            chat_id=chat_id,
        )


async def _generate_and_preview(
    chat_id: str,
    start_dt: datetime,
    duration: int,
    recipient: dict | None,
    original_text: str,
) -> None:
    """Generiert Einladungstext via GPT und zeigt Vorschau mit Buttons."""
    from app.services.telegram_bot_service import send_message

    # Ende berechnen
    end_dt = start_dt + timedelta(minutes=duration)

    # Attendee Email (falls vorhanden)
    attendee_email = recipient.get("email") if recipient else None

    # GPT-Einladungstext + Betreff generieren
    await send_message(
        f"Erstelle Kalendereinladung{' fuer ' + recipient['name'] if recipient else ''}...",
        chat_id=chat_id,
    )

    invite_data = await _generate_invite(original_text, recipient, start_dt, duration)

    if invite_data:
        subject = invite_data.get("subject", "")
        body_html = invite_data.get("body_html", "")
    else:
        subject = ""
        body_html = ""

    # Fallbacks
    if not subject:
        if recipient:
            subject = f"Telefonischer Austausch | {recipient['name']} & Milad Hamdard"
        else:
            subject = "Termin — Milad Hamdard"
    if not body_html:
        body_html = _fallback_invite_body(recipient, start_dt)

    # Vorschau-Text (HTML -> Plaintext fuer Telegram)
    preview_text = re.sub(r"<[^>]+>", "", body_html)
    preview_text = preview_text.replace("&nbsp;", " ").strip()

    # Zwischenspeichern
    _pending_calendar[chat_id] = {
        "subject": subject,
        "start": start_dt.isoformat(),
        "end": end_dt.isoformat(),
        "duration": duration,
        "body_html": body_html,
        "attendee_email": attendee_email,
        "attendee_name": recipient["name"] if recipient else None,
        "recipient": recipient,
        "original_text": original_text,
        "created_at": datetime.now().isoformat(),
    }

    # Vorschau-Nachricht
    date_display = start_dt.strftime("%d.%m.%Y")
    time_display = f"{start_dt.strftime('%H:%M')} — {end_dt.strftime('%H:%M')}"
    attendee_display = f"\nTeilnehmer: {recipient['name']}" if recipient else ""
    if attendee_email:
        attendee_display += f" ({attendee_email})"

    await send_message(
        f"<b>Termin-Vorschau</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"Betreff: <b>{subject}</b>\n"
        f"Datum: {date_display}\n"
        f"Zeit: {time_display}\n"
        f"Dauer: {duration} Minuten"
        f"{attendee_display}\n"
        f"━━━━━━━━━━━━━━━\n\n"
        f"<i>{preview_text[:600]}</i>",
        chat_id=chat_id,
        reply_markup={
            "inline_keyboard": [
                [
                    {"text": "Termin erstellen", "callback_data": "cal_confirm"},
                    {"text": "Neu schreiben", "callback_data": "cal_rewrite"},
                    {"text": "Abbrechen", "callback_data": "cal_cancel"},
                ],
            ],
        },
    )


async def handle_calendar_callback(chat_id: str, action: str, callback_id: str) -> None:
    """Verarbeitet Button-Klicks fuer Kalender (Vorschau + Empfaenger-Auswahl).

    Actions: cal_confirm, cal_rewrite, cal_cancel, cal_pick_0..4, cal_pick_cancel
    """
    from app.services.telegram_bot_service import answer_callback_query, send_message

    # ── Empfaenger-Auswahl (cal_pick_*) ──
    if action.startswith("cal_pick_"):
        choice_data = _pending_calendar_pick.pop(chat_id, None)
        if not choice_data:
            await answer_callback_query(callback_id, "Auswahl abgelaufen.")
            return

        if action == "cal_pick_cancel":
            await answer_callback_query(callback_id, "Abgebrochen.")
            await send_message("Termin-Erstellung abgebrochen.", chat_id=chat_id)
            return

        try:
            idx = int(action.replace("cal_pick_", ""))
            recipient = choice_data["matches"][idx]
        except (ValueError, IndexError):
            await answer_callback_query(callback_id, "Ungueltige Auswahl.")
            return

        await answer_callback_query(callback_id, f"{recipient['name']} ausgewaehlt")

        # Termin-Vorschau mit ausgewaehltem Empfaenger
        try:
            start_dt = datetime.fromisoformat(f"{choice_data['date_str']}T{choice_data['time_str']}:00")
        except ValueError:
            await send_message("Fehler beim Parsen des Datums.", chat_id=chat_id)
            return

        await _generate_and_preview(
            chat_id, start_dt, choice_data["duration"], recipient, choice_data["original_text"],
        )
        return

    # ── Termin-Vorschau Aktionen (cal_confirm / cal_rewrite / cal_cancel) ──
    pending = _pending_calendar.get(chat_id)

    if not pending:
        await answer_callback_query(callback_id, "Keine Termin-Vorschau vorhanden.")
        return

    if action == "cal_confirm":
        await answer_callback_query(callback_id, "Termin wird erstellt...")

        try:
            result = await _create_calendar_event(
                subject=pending["subject"],
                start_iso=pending["start"],
                end_iso=pending["end"],
                body_html=pending["body_html"],
                attendee_email=pending.get("attendee_email"),
                attendee_name=pending.get("attendee_name"),
            )

            if result.get("success"):
                # ── Activity im CRM loggen ──
                await _log_calendar_activity(pending)

                attendee_info = ""
                if pending.get("attendee_name"):
                    attendee_info = f"\nTeilnehmer: {pending['attendee_name']}"
                    if pending.get("attendee_email"):
                        attendee_info += f" (Einladung gesendet)"

                start_dt = datetime.fromisoformat(pending["start"])
                await send_message(
                    f"<b>Termin erstellt!</b>\n\n"
                    f"Betreff: <b>{pending['subject']}</b>\n"
                    f"Datum: {start_dt.strftime('%d.%m.%Y')}\n"
                    f"Zeit: {start_dt.strftime('%H:%M')}"
                    f"{attendee_info}",
                    chat_id=chat_id,
                )
                logger.info(f"Termin erstellt: {pending['subject']} am {pending['start']}")
            else:
                error = result.get("error", "Unbekannter Fehler")
                await send_message(
                    f"Termin-Erstellung fehlgeschlagen:\n{error[:300]}",
                    chat_id=chat_id,
                )
        except Exception as e:
            logger.error(f"Termin-Erstellung fehlgeschlagen: {e}", exc_info=True)
            await send_message(
                f"Fehler: {str(e)[:200]}",
                chat_id=chat_id,
            )
        finally:
            _pending_calendar.pop(chat_id, None)

    elif action == "cal_rewrite":
        # ── Einladungstext neu generieren ──
        await answer_callback_query(callback_id, "Schreibe Einladung neu...")
        recipient = pending.get("recipient")
        original_text = pending.get("original_text", "")
        start_dt = datetime.fromisoformat(pending["start"])
        duration = pending.get("duration", 30)
        _pending_calendar.pop(chat_id, None)

        await _generate_and_preview(chat_id, start_dt, duration, recipient, original_text)

    elif action == "cal_cancel":
        _pending_calendar.pop(chat_id, None)
        await answer_callback_query(callback_id, "Termin verworfen.")
        await send_message("Termin wurde verworfen.", chat_id=chat_id)


# ── GPT Einladungstext-Generierung ──────────────────────────────

async def _generate_invite(
    user_instruction: str,
    recipient: dict | None,
    start_dt: datetime,
    duration: int,
) -> dict | None:
    """Generiert Betreff + Einladungstext via GPT-4o.

    Returns: {"subject": str, "body_html": str} oder None
    """
    if not settings.openai_api_key:
        logger.warning("OpenAI API Key nicht konfiguriert")
        return None

    today = datetime.now().strftime("%d.%m.%Y")
    termin_date = start_dt.strftime("%d.%m.%Y")
    termin_time = start_dt.strftime("%H:%M")

    # Empfaenger-Kontext
    if recipient:
        salutation = recipient.get("salutation", "")
        name_parts = recipient["name"].split()
        last_name = name_parts[-1] if name_parts else recipient["name"]
        recipient_context = (
            f"Empfaenger: {recipient['name']} ({recipient['type']})\n"
            f"Anrede: {salutation or 'keine Anrede hinterlegt'}\n"
            f"Nachname: {last_name}\n"
            f"Vorname: {recipient.get('first_name', '')}\n"
            f"Email: {recipient.get('email', 'nicht vorhanden')}"
        )
    else:
        recipient_context = "Kein bestimmter Empfaenger — allgemeiner Termin"

    user_prompt = (
        f"{recipient_context}\n\n"
        f"Heutiges Datum: {today}\n"
        f"Termin-Datum: {termin_date}\n"
        f"Termin-Uhrzeit: {termin_time} Uhr\n"
        f"Dauer: {duration} Minuten\n\n"
        f"Anweisung von Milad:\n{user_instruction}"
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
                        {"role": "system", "content": CALENDAR_INVITE_SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                },
            )
            response.raise_for_status()
            data = response.json()

        content = data["choices"][0]["message"]["content"]
        result = json.loads(content) if isinstance(content, str) else content
        logger.info(f"Kalender-Einladung generiert: Betreff='{result.get('subject', '')[:60]}'")
        return result

    except Exception as e:
        logger.error(f"Einladungs-Generierung fehlgeschlagen: {e}")
        return None


def _fallback_invite_body(recipient: dict | None, start_dt: datetime) -> str:
    """Fallback-Einladungstext wenn GPT fehlschlaegt."""
    if recipient:
        salutation = recipient.get("salutation", "")
        name_parts = recipient["name"].split()
        last_name = name_parts[-1] if name_parts else recipient["name"]
        if salutation == "Herr":
            greeting = f"Sehr geehrter Herr {last_name}"
        elif salutation == "Frau":
            greeting = f"Sehr geehrte Frau {last_name}"
        else:
            greeting = f"Guten Tag {recipient['name']}"
    else:
        greeting = "Guten Tag"

    return (
        f"<p>{greeting},</p>"
        f"<p>hiermit lade ich Sie herzlich zu einem telefonischen Austausch "
        f"am {start_dt.strftime('%d.%m.%Y')} um {start_dt.strftime('%H:%M')} Uhr ein.</p>"
        f"<p>Ich freue mich auf unser Gespraech.</p>"
        f"<p>Mit freundlichen Gruessen</p>"
    )


# ── Microsoft Graph Calendar API ────────────────────────────────

async def _create_calendar_event(
    subject: str,
    start_iso: str,
    end_iso: str,
    body_html: str,
    attendee_email: str | None = None,
    attendee_name: str | None = None,
) -> dict:
    """Erstellt einen Kalender-Termin via Microsoft Graph Calendar API.

    Returns: {"success": True, "event_id": "..."} oder {"success": False, "error": "..."}
    """
    from app.services.email_service import EMAIL_SIGNATURE, MicrosoftGraphClient

    sender = settings.microsoft_sender_email
    if not sender:
        return {"success": False, "error": "Kein Absender konfiguriert (MICROSOFT_SENDER_EMAIL)"}

    try:
        token = await MicrosoftGraphClient._get_access_token()
    except Exception as e:
        logger.error(f"Graph Token-Fehler: {e}")
        return {"success": False, "error": f"Token-Fehler: {e}"}

    graph_url = f"https://graph.microsoft.com/v1.0/users/{sender}/calendar/events"

    # Vollstaendigen Body mit Signatur bauen
    full_body_html = f"""<div style="font-family: Arial, Helvetica, sans-serif;">
    {body_html}
    <br>
    {EMAIL_SIGNATURE}
</div>"""

    event_payload = {
        "subject": subject,
        "body": {
            "contentType": "HTML",
            "content": full_body_html,
        },
        "start": {
            "dateTime": start_iso,
            "timeZone": "Europe/Berlin",
        },
        "end": {
            "dateTime": end_iso,
            "timeZone": "Europe/Berlin",
        },
        "isOnlineMeeting": False,
        "reminderMinutesBeforeStart": 15,
    }

    # Teilnehmer hinzufuegen (falls vorhanden)
    if attendee_email:
        event_payload["attendees"] = [
            {
                "emailAddress": {
                    "address": attendee_email,
                    "name": attendee_name or attendee_email,
                },
                "type": "required",
            }
        ]

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                graph_url,
                json=event_payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )

        if resp.status_code == 201:
            data = resp.json()
            event_id = data.get("id", "")
            logger.info(f"Kalender-Termin erstellt: {subject} (ID: {event_id[:20]})")
            return {"success": True, "event_id": event_id}
        else:
            error_text = resp.text[:500]
            logger.error(f"Graph Calendar-Fehler {resp.status_code}: {error_text}")
            return {"success": False, "error": f"HTTP {resp.status_code}: {error_text}"}

    except Exception as e:
        logger.error(f"Graph Calendar-Exception: {e}")
        return {"success": False, "error": str(e)}


# ── Activity-Tracking ───────────────────────────────────────────

async def _log_calendar_activity(pending: dict) -> None:
    """Loggt den erstellten Termin als ATSActivity im CRM."""
    try:
        from app.database import async_session_maker
        from app.models.ats_activity import ATSActivity, ActivityType

        recipient = pending.get("recipient")
        if not recipient:
            return

        candidate_id = None
        company_id = None

        if recipient["type"] == "candidate":
            candidate_id = recipient["id"]
        elif recipient["type"] == "contact":
            # Kontakt → company_id falls vorhanden
            company_id = recipient.get("company_id")

        start_dt = datetime.fromisoformat(pending["start"])

        async with async_session_maker() as db:
            activity = ATSActivity(
                activity_type=ActivityType.NOTE_ADDED,
                description=f"Termin erstellt: {pending['subject']} am {start_dt.strftime('%d.%m.%Y %H:%M')}",
                candidate_id=candidate_id,
                company_id=company_id,
                metadata_json={
                    "source": "telegram_bot",
                    "action": "calendar_created",
                    "subject": pending["subject"],
                    "start": pending["start"],
                    "end": pending["end"],
                    "attendee_name": pending.get("attendee_name"),
                    "attendee_email": pending.get("attendee_email"),
                },
            )
            db.add(activity)
            await db.commit()

        logger.info(f"Calendar-Activity geloggt fuer {recipient['name']}")

    except Exception as e:
        logger.error(f"Calendar-Activity Logging fehlgeschlagen: {e}")


# ── Erinnerungs-Service (Vorabend-Reminder) ─────────────────────

async def send_tomorrow_reminders() -> dict:
    """Sucht morgige Kalender-Termine und sendet Erinnerungs-Emails an Teilnehmer.

    Wird taeglich um 18:00 via n8n/API-Endpoint aufgerufen.

    Flow:
    1. Microsoft Graph: Alle Termine von morgen laden
    2. Fuer jeden Termin mit externem Teilnehmer:
       a. Email in CRM suchen (Kandidat/Kontakt)
       b. GPT generiert Erinnerungs-Email (Hallo Herr/Frau...)
       c. Email senden via Microsoft Graph
       d. Activity loggen
    3. Ergebnis zurueckgeben

    Returns: {"reminders_sent": int, "events_total": int, "errors": list}
    """
    result = {"reminders_sent": 0, "events_total": 0, "errors": [], "details": []}

    try:
        from app.services.email_service import EMAIL_SIGNATURE, MicrosoftGraphClient

        sender = settings.microsoft_sender_email
        if not sender:
            result["errors"].append("Kein Absender konfiguriert")
            return result

        token = await MicrosoftGraphClient._get_access_token()

        # ── Morgen-Zeitraum berechnen ──
        tomorrow = datetime.now() + timedelta(days=1)
        start_of_day = tomorrow.replace(hour=0, minute=0, second=0, microsecond=0)
        end_of_day = tomorrow.replace(hour=23, minute=59, second=59, microsecond=0)

        # ── Microsoft Graph: Termine von morgen laden ──
        graph_url = (
            f"https://graph.microsoft.com/v1.0/users/{sender}/calendarView"
            f"?startDateTime={start_of_day.isoformat()}"
            f"&endDateTime={end_of_day.isoformat()}"
            f"&$select=subject,start,end,attendees"
            f"&$top=50"
        )

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                graph_url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Prefer": 'outlook.timezone="Europe/Berlin"',
                },
            )

        if resp.status_code != 200:
            result["errors"].append(f"Graph Calendar-Fehler: {resp.status_code}")
            return result

        events = resp.json().get("value", [])
        result["events_total"] = len(events)

        if not events:
            return result

        # ── Fuer jeden Termin mit Teilnehmern: Erinnerung senden ──
        for event in events:
            attendees = event.get("attendees", [])
            subject = event.get("subject", "Termin")
            start_info = event.get("start", {})
            start_time = start_info.get("dateTime", "")

            # Nur externe Teilnehmer (nicht Milad selbst)
            external_attendees = [
                a for a in attendees
                if a.get("emailAddress", {}).get("address", "").lower() != sender.lower()
            ]

            if not external_attendees:
                continue

            for attendee in external_attendees:
                attendee_email = attendee.get("emailAddress", {}).get("address", "")
                attendee_name = attendee.get("emailAddress", {}).get("name", "")

                if not attendee_email:
                    continue

                try:
                    # ── CRM-Daten laden (Kandidat/Kontakt) ──
                    crm_data = await _find_person_by_email(attendee_email)

                    # ── Erinnerungs-Email generieren ──
                    reminder = await _generate_reminder_email(
                        attendee_name=attendee_name,
                        attendee_email=attendee_email,
                        subject=subject,
                        start_time=start_time,
                        crm_data=crm_data,
                    )

                    if not reminder:
                        reminder = _fallback_reminder(attendee_name, crm_data, subject, start_time)

                    reminder_subject = reminder.get("subject", f"Erinnerung: {subject}")
                    reminder_body = reminder.get("body_html", "")

                    # Signatur anhaengen
                    full_html = f"""<div style="font-family: Arial, Helvetica, sans-serif;">
    {reminder_body}
    <br>
    {EMAIL_SIGNATURE}
</div>"""

                    # ── Email senden ──
                    send_result = await MicrosoftGraphClient.send_email(
                        to_email=attendee_email,
                        subject=reminder_subject,
                        body_html=full_html,
                    )

                    if send_result.get("success"):
                        result["reminders_sent"] += 1
                        result["details"].append({
                            "email": attendee_email,
                            "name": attendee_name,
                            "event_subject": subject,
                            "status": "sent",
                        })

                        # ── Activity loggen ──
                        await _log_reminder_activity(attendee_email, crm_data, subject, start_time)

                        logger.info(f"Erinnerung gesendet: {attendee_email} fuer '{subject}'")
                    else:
                        error = send_result.get("error", "Unbekannt")
                        result["errors"].append(f"{attendee_email}: {error[:100]}")
                        result["details"].append({
                            "email": attendee_email,
                            "name": attendee_name,
                            "event_subject": subject,
                            "status": "failed",
                            "error": error[:100],
                        })

                except Exception as e:
                    logger.error(f"Erinnerung fehlgeschlagen fuer {attendee_email}: {e}")
                    result["errors"].append(f"{attendee_email}: {str(e)[:100]}")

    except Exception as e:
        logger.error(f"Reminder-Service fehlgeschlagen: {e}", exc_info=True)
        result["errors"].append(str(e)[:200])

    return result


async def _find_person_by_email(email: str) -> dict | None:
    """Sucht Kandidat oder Kontakt per Email-Adresse im CRM."""
    try:
        from app.database import async_session_maker
        from app.models.candidate import Candidate
        from app.models.company_contact import CompanyContact
        from sqlalchemy import select

        # Kandidat suchen
        async with async_session_maker() as db:
            result = await db.execute(
                select(Candidate).where(Candidate.email == email).limit(1)
            )
            candidate = result.scalar_one_or_none()

            if candidate:
                return {
                    "type": "candidate",
                    "id": str(candidate.id),
                    "name": f"{candidate.first_name or ''} {candidate.last_name or ''}".strip(),
                    "first_name": candidate.first_name or "",
                    "last_name": candidate.last_name or "",
                    "salutation": candidate.gender or "",
                    "position": candidate.current_position,
                    "company": candidate.current_company,
                    "city": candidate.city,
                }

        # Kontakt suchen
        async with async_session_maker() as db:
            result = await db.execute(
                select(CompanyContact).where(CompanyContact.email == email).limit(1)
            )
            contact = result.scalar_one_or_none()

            if contact:
                company_name = None
                try:
                    if contact.company:
                        company_name = contact.company.name
                except Exception:
                    pass

                return {
                    "type": "contact",
                    "id": str(contact.id),
                    "name": f"{contact.first_name or ''} {contact.last_name or ''}".strip(),
                    "first_name": contact.first_name or "",
                    "last_name": contact.last_name or "",
                    "salutation": contact.salutation or "",
                    "position": contact.position,
                    "company": company_name,
                    "city": contact.city,
                }

        return None

    except Exception as e:
        logger.error(f"CRM-Suche per Email fehlgeschlagen: {e}")
        return None


async def _generate_reminder_email(
    attendee_name: str,
    attendee_email: str,
    subject: str,
    start_time: str,
    crm_data: dict | None,
) -> dict | None:
    """Generiert Erinnerungs-Email via GPT-4o.

    Returns: {"subject": str, "body_html": str} oder None
    """
    if not settings.openai_api_key:
        return None

    # Termin-Zeit parsen
    try:
        dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
        time_display = dt.strftime("%H:%M")
        date_display = dt.strftime("%d.%m.%Y")
    except Exception:
        time_display = start_time
        date_display = "morgen"

    # CRM-Kontext
    crm_context = "Keine CRM-Daten verfuegbar."
    if crm_data:
        parts = [f"Typ: {crm_data['type']}", f"Name: {crm_data['name']}"]
        if crm_data.get("salutation"):
            parts.append(f"Anrede: {crm_data['salutation']}")
        if crm_data.get("position"):
            parts.append(f"Position: {crm_data['position']}")
        if crm_data.get("company"):
            parts.append(f"Firma: {crm_data['company']}")
        if crm_data.get("city"):
            parts.append(f"Stadt: {crm_data['city']}")
        crm_context = "\n".join(parts)

    user_prompt = (
        f"CRM-Daten zum Teilnehmer:\n{crm_context}\n\n"
        f"Teilnehmer-Name: {attendee_name}\n"
        f"Teilnehmer-Email: {attendee_email}\n"
        f"Termin-Betreff: {subject}\n"
        f"Termin-Datum: {date_display}\n"
        f"Termin-Uhrzeit: {time_display} Uhr\n\n"
        f"Schreibe eine kurze Erinnerungs-Email fuer morgen."
    )

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
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
                        {"role": "system", "content": CALENDAR_REMINDER_SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                },
            )
            response.raise_for_status()
            data = response.json()

        content = data["choices"][0]["message"]["content"]
        result = json.loads(content) if isinstance(content, str) else content
        logger.info(f"Erinnerungs-Email generiert fuer {attendee_email}")
        return result

    except Exception as e:
        logger.error(f"Erinnerungs-Generierung fehlgeschlagen: {e}")
        return None


def _fallback_reminder(
    attendee_name: str,
    crm_data: dict | None,
    subject: str,
    start_time: str,
) -> dict:
    """Fallback-Erinnerungs-Email wenn GPT fehlschlaegt."""
    # Anrede bestimmen
    if crm_data and crm_data.get("salutation"):
        last_name = crm_data.get("last_name", attendee_name)
        salutation = crm_data["salutation"]
        greeting = f"Hallo {salutation} {last_name}"
    else:
        greeting = f"Hallo {attendee_name}"

    try:
        dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
        time_display = dt.strftime("%H:%M")
        date_display = dt.strftime("%d.%m.%Y")
    except Exception:
        time_display = ""
        date_display = "morgen"

    return {
        "subject": f"Erinnerung: {subject}",
        "body_html": (
            f"<p>{greeting},</p>"
            f"<p>ich moechte Sie kurz an unseren Termin morgen ({date_display}) "
            f"um {time_display} Uhr erinnern.</p>"
            f"<p>Ich freue mich auf unser Gespraech.</p>"
            f"<p>Mit freundlichen Gruessen</p>"
        ),
    }


async def _log_reminder_activity(
    attendee_email: str,
    crm_data: dict | None,
    event_subject: str,
    start_time: str,
) -> None:
    """Loggt die gesendete Erinnerungs-Email als ATSActivity."""
    try:
        from app.database import async_session_maker
        from app.models.ats_activity import ATSActivity, ActivityType

        candidate_id = None
        company_id = None

        if crm_data:
            if crm_data["type"] == "candidate":
                candidate_id = crm_data["id"]

        async with async_session_maker() as db:
            activity = ATSActivity(
                activity_type=ActivityType.EMAIL_SENT,
                description=f"Termin-Erinnerung: {event_subject[:80]}",
                candidate_id=candidate_id,
                company_id=company_id,
                metadata_json={
                    "source": "telegram_bot",
                    "action": "calendar_reminder",
                    "to_email": attendee_email,
                    "event_subject": event_subject,
                    "event_start": start_time,
                },
            )
            db.add(activity)
            await db.commit()

    except Exception as e:
        logger.error(f"Reminder-Activity Logging fehlgeschlagen: {e}")
