"""Telegram Calendar Handler — Kalendereintraege via Telegram Bot.

Erstellt Termine im Outlook-Kalender von hamdard@sincirus.com
via Microsoft Graph Calendar API.

Betreff-Format: "Telefonischer Austausch | [Vorname Nachname] & Milad Hamdard"
"""

import logging
from datetime import datetime

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# ── Zwischenspeicher (In-Memory, Single-User) ──
_pending_calendar: dict[str, dict] = {}
_pending_calendar_pick: dict[str, dict] = {}


async def handle_calendar_create(chat_id: str, text: str, entities: dict) -> None:
    """Erstellt einen Kalender-Termin basierend auf Intent-Entities.

    Flow:
    1. Empfaenger per Name suchen (optional)
    2. Termin-Details aus Entities extrahieren
    3. Vorschau zeigen mit Buttons (Erstellen / Abbrechen)
    4. Bei Bestaetigung: Termin via Microsoft Graph erstellen
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

        # Vorschau generieren und anzeigen
        await _preview_calendar_event(chat_id, start_dt, duration, recipient)

    except Exception as e:
        logger.error(f"Calendar-Handler fehlgeschlagen: {e}", exc_info=True)
        from app.services.telegram_bot_service import send_message
        await send_message(
            f"Fehler beim Termin-Erstellen: {str(e)[:200]}",
            chat_id=chat_id,
        )


async def _preview_calendar_event(
    chat_id: str,
    start_dt: datetime,
    duration: int,
    recipient: dict | None,
) -> None:
    """Zeigt eine Termin-Vorschau mit Bestaetigung-Buttons."""
    from app.services.telegram_bot_service import send_message

    # Betreff erstellen
    if recipient:
        subject = f"Telefonischer Austausch | {recipient['name']} & Milad Hamdard"
    else:
        subject = "Telefonischer Austausch | Milad Hamdard"

    # Ende berechnen
    from datetime import timedelta
    end_dt = start_dt + timedelta(minutes=duration)

    # Attendee Email (falls vorhanden)
    attendee_email = recipient.get("email") if recipient else None

    # Zwischenspeichern
    _pending_calendar[chat_id] = {
        "subject": subject,
        "start": start_dt.isoformat(),
        "end": end_dt.isoformat(),
        "attendee_email": attendee_email,
        "attendee_name": recipient["name"] if recipient else None,
        "created_at": datetime.now().isoformat(),
    }

    # Vorschau-Text
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
        f"━━━━━━━━━━━━━━━",
        chat_id=chat_id,
        reply_markup={
            "inline_keyboard": [
                [
                    {"text": "Termin erstellen", "callback_data": "cal_confirm"},
                    {"text": "Abbrechen", "callback_data": "cal_cancel"},
                ],
            ],
        },
    )


async def handle_calendar_callback(chat_id: str, action: str, callback_id: str) -> None:
    """Verarbeitet Button-Klicks fuer Kalender (Vorschau + Empfaenger-Auswahl).

    Actions: cal_confirm, cal_cancel, cal_pick_0..4, cal_pick_cancel
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

        await _preview_calendar_event(chat_id, start_dt, choice_data["duration"], recipient)
        return

    # ── Termin-Vorschau Aktionen (cal_confirm / cal_cancel) ──
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
                attendee_email=pending.get("attendee_email"),
                attendee_name=pending.get("attendee_name"),
            )

            if result.get("success"):
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

    elif action == "cal_cancel":
        _pending_calendar.pop(chat_id, None)
        await answer_callback_query(callback_id, "Termin verworfen.")
        await send_message("Termin wurde verworfen.", chat_id=chat_id)


async def _create_calendar_event(
    subject: str,
    start_iso: str,
    end_iso: str,
    attendee_email: str | None = None,
    attendee_name: str | None = None,
) -> dict:
    """Erstellt einen Kalender-Termin via Microsoft Graph Calendar API.

    Returns: {"success": True, "event_id": "..."} oder {"success": False, "error": "..."}
    """
    from app.services.email_service import MicrosoftGraphClient

    sender = settings.microsoft_sender_email
    if not sender:
        return {"success": False, "error": "Kein Absender konfiguriert (MICROSOFT_SENDER_EMAIL)"}

    try:
        token = await MicrosoftGraphClient._get_access_token()
    except Exception as e:
        logger.error(f"Graph Token-Fehler: {e}")
        return {"success": False, "error": f"Token-Fehler: {e}"}

    graph_url = f"https://graph.microsoft.com/v1.0/users/{sender}/calendar/events"

    event_body = {
        "subject": subject,
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
        event_body["attendees"] = [
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
                json=event_body,
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
