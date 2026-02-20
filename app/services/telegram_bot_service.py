"""Telegram Bot Service — Kernlogik fuer den PulsePoint Telegram Bot.

Verarbeitet eingehende Telegram Updates (Text, Voice, Callback Queries).
Routet Nachrichten an die passenden Handler.
Sendet Antworten zurueck an Telegram.

Sicherheit: Nur Milads Chat-ID (7103040196) wird akzeptiert.
"""

import logging
from datetime import date, datetime, timedelta, timezone
from uuid import UUID

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# Telegram Bot API Base URL
TELEGRAM_API = "https://api.telegram.org/bot{token}"


# ── Telegram API Helpers ─────────────────────────────────────────

async def send_message(
    text: str,
    chat_id: str | None = None,
    reply_markup: dict | None = None,
    parse_mode: str = "HTML",
) -> dict | None:
    """Sendet eine Nachricht an einen Telegram Chat."""
    if not settings.telegram_bot_token:
        logger.warning("Telegram Bot Token nicht konfiguriert")
        return None

    target_chat = chat_id or settings.telegram_chat_id
    if not target_chat:
        logger.warning("Keine Telegram Chat-ID konfiguriert")
        return None

    url = f"{TELEGRAM_API.format(token=settings.telegram_bot_token)}/sendMessage"
    payload = {
        "chat_id": target_chat,
        "text": text,
        "parse_mode": parse_mode,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error(f"Telegram sendMessage fehlgeschlagen: {e}")
        return None


async def send_document(
    document: bytes,
    filename: str,
    chat_id: str | None = None,
    caption: str | None = None,
) -> dict | None:
    """Sendet ein Dokument (PDF etc.) an einen Telegram Chat."""
    if not settings.telegram_bot_token:
        return None

    target_chat = chat_id or settings.telegram_chat_id
    url = f"{TELEGRAM_API.format(token=settings.telegram_bot_token)}/sendDocument"

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            data = {"chat_id": target_chat}
            if caption:
                data["caption"] = caption
                data["parse_mode"] = "HTML"
            resp = await client.post(
                url,
                data=data,
                files={"document": (filename, document, "application/pdf")},
            )
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error(f"Telegram sendDocument fehlgeschlagen: {e}")
        return None


async def answer_callback_query(callback_query_id: str, text: str = "") -> None:
    """Beantwortet eine Callback Query (Button-Klick Feedback)."""
    if not settings.telegram_bot_token:
        return
    url = f"{TELEGRAM_API.format(token=settings.telegram_bot_token)}/answerCallbackQuery"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(url, json={
                "callback_query_id": callback_query_id,
                "text": text,
            })
    except Exception:
        pass


async def _download_file(file_id: str) -> bytes | None:
    """Laedt eine Datei von Telegram herunter (z.B. Voice-Nachricht)."""
    if not settings.telegram_bot_token:
        return None

    base = TELEGRAM_API.format(token=settings.telegram_bot_token)

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Schritt 1: File-Info holen
            info_resp = await client.get(f"{base}/getFile", params={"file_id": file_id})
            info_resp.raise_for_status()
            file_path = info_resp.json()["result"]["file_path"]

            # Schritt 2: Datei herunterladen
            download_url = f"https://api.telegram.org/file/bot{settings.telegram_bot_token}/{file_path}"
            file_resp = await client.get(download_url)
            file_resp.raise_for_status()
            return file_resp.content
    except Exception as e:
        logger.error(f"Telegram Datei-Download fehlgeschlagen: {e}")
        return None


# ── Update Handler ───────────────────────────────────────────────

async def handle_update(update: dict) -> None:
    """Haupteinstiegspunkt: Verarbeitet ein Telegram Update."""
    try:
        # Callback Query (Button-Klick)
        if "callback_query" in update:
            await _handle_callback_query(update["callback_query"])
            return

        message = update.get("message")
        if not message:
            return

        chat_id = str(message.get("chat", {}).get("id", ""))

        # Sicherheitscheck: Nur Milads Chat-ID akzeptieren
        if settings.telegram_chat_id and chat_id != settings.telegram_chat_id:
            logger.warning(f"Unbekannte Chat-ID: {chat_id} — ignoriert")
            return

        # Voice-Nachricht
        if "voice" in message:
            await _handle_voice_message(chat_id, message)
            return

        # Text-Nachricht
        text = message.get("text", "").strip()
        if not text:
            return

        # Kommando (startet mit /)
        if text.startswith("/"):
            await _handle_command(chat_id, text)
        else:
            await _handle_free_text(chat_id, text)

    except Exception as e:
        logger.error(f"Fehler beim Verarbeiten des Telegram Updates: {e}", exc_info=True)
        try:
            await send_message(f"Fehler bei der Verarbeitung: {str(e)[:200]}")
        except Exception:
            pass


# ── Command Handler ──────────────────────────────────────────────

async def _handle_command(chat_id: str, text: str) -> None:
    """Verarbeitet Slash-Kommandos."""
    parts = text.split(maxsplit=1)
    command = parts[0].lower().split("@")[0]  # /tasks@Sincirusbot -> /tasks
    args = parts[1].strip() if len(parts) > 1 else ""

    if command == "/start":
        await send_message(
            "Willkommen beim PulsePoint Bot!\n\n"
            "Verfuegbare Kommandos:\n"
            "/tasks — Heutige Aufgaben\n"
            "/search &lt;Name&gt; — Kandidat suchen\n"
            "/briefing — Tages-Ueberblick\n"
            "/stats — Statistiken\n"
            "/help — Hilfe\n\n"
            "Du kannst auch einfach Freitext oder Sprachnachrichten senden!",
            chat_id=chat_id,
        )

    elif command == "/tasks":
        await _handle_task_list(chat_id)

    elif command == "/done":
        if args:
            await _handle_task_complete(chat_id, args)
        else:
            await send_message("Bitte gib die Aufgaben-ID an: /done &lt;id&gt;", chat_id=chat_id)

    elif command == "/search":
        if args:
            await _handle_candidate_search(chat_id, args)
        else:
            await send_message("Bitte gib einen Namen an: /search &lt;Name&gt;", chat_id=chat_id)

    elif command == "/briefing":
        await _handle_briefing(chat_id)

    elif command == "/stats":
        await _handle_stats(chat_id)

    elif command == "/stellenpdf":
        if args:
            await _handle_stelle_pdf(chat_id, args)
        else:
            await send_message("Bitte gib die Stellen-ID an: /stellenpdf &lt;id&gt;", chat_id=chat_id)

    elif command == "/help":
        await send_message(
            "<b>PulsePoint Bot — Hilfe</b>\n\n"
            "<b>Kommandos:</b>\n"
            "/tasks — Heutige Aufgaben + Ueberfaellige\n"
            "/done &lt;id&gt; — Aufgabe als erledigt markieren\n"
            "/search &lt;Name&gt; — Kandidat suchen\n"
            "/briefing — Was gibt es Neues?\n"
            "/stats — Aktuelle Statistiken\n"
            "/stellenpdf &lt;id&gt; — Stelle als PDF\n\n"
            "<b>Freitext:</b>\n"
            "\"Erstelle Aufgabe: Firma Mueller anrufen morgen 14 Uhr\"\n"
            "\"Finde Anna Schmidt\"\n"
            "\"Was steht heute an?\"\n"
            "\"Gerade mit Firma X telefoniert...\"\n\n"
            "<b>Sprachnachrichten:</b>\n"
            "Einfach eine Sprachnachricht senden — wird automatisch transkribiert und verarbeitet.",
            chat_id=chat_id,
        )

    else:
        await send_message(f"Unbekanntes Kommando: {command}\nTippe /help fuer Hilfe.", chat_id=chat_id)


# ── Feature Handler ──────────────────────────────────────────────

async def _handle_task_list(chat_id: str) -> None:
    """Zeigt heutige Aufgaben + ueberfaellige."""
    try:
        from app.database import async_session_maker
        from app.services.ats_todo_service import ATSTodoService

        async with async_session_maker() as db:
            service = ATSTodoService(db)
            today_todos = await service.get_today_todos()
            overdue_todos = await service.get_overdue_todos()

        lines = ["<b>Aufgaben</b>\n"]

        if overdue_todos:
            lines.append(f"<b>Ueberfaellig ({len(overdue_todos)}):</b>")
            for t in overdue_todos[:10]:
                prio = _priority_emoji(t.priority.value)
                due = t.due_date.strftime("%d.%m.") if t.due_date else ""
                time_str = f" {t.due_time}" if t.due_time else ""
                lines.append(f"  {prio} {t.title} ({due}{time_str})")
            lines.append("")

        if today_todos:
            lines.append(f"<b>Heute ({len(today_todos)}):</b>")
            for t in today_todos[:15]:
                prio = _priority_emoji(t.priority.value)
                time_str = f" {t.due_time}" if t.due_time else ""
                status = " [done]" if t.status.value == "erledigt" else ""
                lines.append(f"  {prio} {t.title}{time_str}{status}")
        else:
            lines.append("Keine Aufgaben fuer heute.")

        # Inline Keyboard mit Optionen
        reply_markup = {
            "inline_keyboard": [
                [
                    {"text": "Alle anzeigen", "callback_data": "tasks_all"},
                    {"text": "Naechste 7 Tage", "callback_data": "tasks_upcoming"},
                ],
            ],
        }

        await send_message("\n".join(lines), chat_id=chat_id, reply_markup=reply_markup)

    except Exception as e:
        logger.error(f"Task-Liste fehlgeschlagen: {e}", exc_info=True)
        await send_message(f"Fehler beim Laden der Aufgaben: {str(e)[:200]}", chat_id=chat_id)


async def _handle_task_complete(chat_id: str, task_id_str: str) -> None:
    """Markiert eine Aufgabe als erledigt."""
    try:
        from app.database import async_session_maker
        from app.services.ats_todo_service import ATSTodoService

        task_id = UUID(task_id_str.strip())

        async with async_session_maker() as db:
            service = ATSTodoService(db)
            todo = await service.complete_todo(task_id)
            if not todo:
                await send_message("Aufgabe nicht gefunden.", chat_id=chat_id)
                return
            title = todo.title
            await db.commit()

        await send_message(f"Aufgabe erledigt: <b>{title}</b>", chat_id=chat_id)

    except ValueError:
        await send_message("Ungueltige Aufgaben-ID. Bitte die UUID angeben.", chat_id=chat_id)
    except Exception as e:
        logger.error(f"Task-Complete fehlgeschlagen: {e}", exc_info=True)
        await send_message(f"Fehler: {str(e)[:200]}", chat_id=chat_id)


async def _handle_task_create(chat_id: str, entities: dict) -> None:
    """Erstellt eine neue Aufgabe basierend auf Intent-Entitaeten.

    Versucht automatisch, die Aufgabe mit einem Kandidaten, Unternehmen
    oder Kontakt zu verknuepfen, wenn ein Name in den Entities steht.
    """
    try:
        from app.database import async_session_maker
        from app.models.candidate import Candidate
        from app.models.company import Company
        from app.models.company_contact import CompanyContact
        from app.services.ats_todo_service import ATSTodoService

        title = entities.get("title", "Neue Aufgabe")
        due_date_str = entities.get("date")
        due_time = entities.get("time")
        priority = entities.get("priority", "wichtig")
        name = entities.get("name")

        # Prioritaet mappen
        prio_map = {
            "dringend": "sehr_dringend",
            "sehr dringend": "sehr_dringend",
            "wichtig": "wichtig",
            "normal": "mittelmaessig",
            "niedrig": "unwichtig",
        }
        mapped_priority = prio_map.get(priority.lower(), "wichtig") if priority else "wichtig"

        # Datum parsen
        due_date = None
        if due_date_str:
            try:
                due_date = date.fromisoformat(due_date_str)
            except ValueError:
                pass
        if not due_date:
            # Default: morgen
            due_date = date.today() + timedelta(days=1)

        # ── Entity-Verknuepfung: Kandidat/Firma/Kontakt suchen ──
        candidate_id = None
        company_id = None
        contact_id = None
        linked_name = None

        if name:
            async with async_session_maker() as db:
                # 1. Kandidat suchen
                result = await db.execute(
                    select(Candidate)
                    .where(
                        (Candidate.first_name + " " + Candidate.last_name).ilike(f"%{name}%")
                    )
                    .limit(1)
                )
                candidate = result.scalar_one_or_none()
                if candidate:
                    candidate_id = candidate.id
                    linked_name = f"{candidate.first_name} {candidate.last_name}"
                else:
                    # 2. Kontakt suchen
                    result = await db.execute(
                        select(CompanyContact)
                        .where(CompanyContact.name.ilike(f"%{name}%"))
                        .limit(1)
                    )
                    contact = result.scalar_one_or_none()
                    if contact:
                        contact_id = contact.id
                        company_id = contact.company_id
                        linked_name = contact.name
                    else:
                        # 3. Firma suchen
                        result = await db.execute(
                            select(Company)
                            .where(Company.name.ilike(f"%{name}%"))
                            .limit(1)
                        )
                        company = result.scalar_one_or_none()
                        if company:
                            company_id = company.id
                            linked_name = company.name

        # ── Todo erstellen ──
        async with async_session_maker() as db:
            service = ATSTodoService(db)
            todo = await service.create_todo(
                title=title,
                priority=mapped_priority,
                due_date=due_date,
                due_time=due_time,
                candidate_id=candidate_id,
                company_id=company_id,
                contact_id=contact_id,
            )
            await db.commit()

        time_str = f" um {due_time}" if due_time else ""
        link_str = f"\nVerknuepft mit: {linked_name}" if linked_name else ""
        await send_message(
            f"Aufgabe erstellt:\n"
            f"<b>{title}</b>\n"
            f"Faellig: {due_date.strftime('%d.%m.%Y')}{time_str}\n"
            f"Prioritaet: {mapped_priority}{link_str}",
            chat_id=chat_id,
        )

    except Exception as e:
        logger.error(f"Task-Create fehlgeschlagen: {e}", exc_info=True)
        await send_message(f"Fehler beim Erstellen der Aufgabe: {str(e)[:200]}", chat_id=chat_id)


async def _handle_candidate_search(chat_id: str, query: str) -> None:
    """Sucht Kandidaten nach Name."""
    try:
        from app.database import async_session_maker
        from app.models.candidate import Candidate
        from sqlalchemy import select, or_

        async with async_session_maker() as db:
            search_term = f"%{query.strip()}%"
            stmt = (
                select(Candidate)
                .where(
                    or_(
                        Candidate.first_name.ilike(search_term),
                        Candidate.last_name.ilike(search_term),
                        (Candidate.first_name + " " + Candidate.last_name).ilike(search_term),
                    )
                )
                .limit(10)
            )
            result = await db.execute(stmt)
            candidates = result.scalars().all()

        if not candidates:
            await send_message(f"Keine Kandidaten gefunden fuer: <b>{query}</b>", chat_id=chat_id)
            return

        lines = [f"<b>Suchergebnisse fuer \"{query}\" ({len(candidates)}):</b>\n"]
        for c in candidates:
            name = f"{c.first_name or ''} {c.last_name or ''}".strip()
            city = c.city or "—"
            position = c.current_position or c.desired_positions or "—"
            lines.append(f"  <b>{name}</b> — {city}")
            lines.append(f"  Position: {position}")
            if c.email:
                lines.append(f"  Email: {c.email}")
            lines.append("")

        await send_message("\n".join(lines), chat_id=chat_id)

    except Exception as e:
        logger.error(f"Kandidatensuche fehlgeschlagen: {e}", exc_info=True)
        await send_message(f"Fehler bei der Suche: {str(e)[:200]}", chat_id=chat_id)


async def _handle_briefing(chat_id: str) -> None:
    """Sendet ein Morgen-Briefing mit den wichtigsten Zahlen."""
    try:
        from app.database import async_session_maker
        from app.models.candidate import Candidate
        from app.models.job import Job
        from app.models.match import Match
        from sqlalchemy import select, func

        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

        async with async_session_maker() as db:
            # Neue Jobs
            new_jobs = (await db.execute(
                select(func.count()).select_from(Job).where(
                    Job.created_at >= cutoff, Job.deleted_at.is_(None)
                )
            )).scalar() or 0

            # Neue Matches
            new_matches = (await db.execute(
                select(func.count()).select_from(Match).where(Match.created_at >= cutoff)
            )).scalar() or 0

            # Top Matches (Score > 75)
            top_matches = (await db.execute(
                select(func.count()).select_from(Match).where(
                    Match.created_at >= cutoff, Match.v2_score >= 75
                )
            )).scalar() or 0

            # Neue Kandidaten
            new_candidates = (await db.execute(
                select(func.count()).select_from(Candidate).where(
                    Candidate.created_at >= cutoff
                )
            )).scalar() or 0

            # Outreach-Antworten
            outreach_responses = (await db.execute(
                select(func.count()).select_from(Match).where(
                    Match.outreach_responded_at >= cutoff
                )
            )).scalar() or 0

            # Heutige Todos
            from app.services.ats_todo_service import ATSTodoService
            todo_service = ATSTodoService(db)
            today_todos = await todo_service.get_today_todos()
            overdue_todos = await todo_service.get_overdue_todos()

        lines = [
            "<b>PulsePoint Briefing</b>",
            f"Stand: {datetime.now().strftime('%d.%m.%Y %H:%M')}\n",
            "<b>Letzte 24 Stunden:</b>",
            f"  Neue Jobs: {new_jobs}",
            f"  Neue Kandidaten: {new_candidates}",
            f"  Neue Matches: {new_matches}",
            f"  Top-Matches (>75): {top_matches}",
            f"  Outreach-Antworten: {outreach_responses}",
            "",
            "<b>Aufgaben:</b>",
            f"  Heute: {len(today_todos)}",
            f"  Ueberfaellig: {len(overdue_todos)}",
        ]

        if overdue_todos:
            lines.append("\n<b>Ueberfaellige Aufgaben:</b>")
            for t in overdue_todos[:5]:
                prio = _priority_emoji(t.priority.value)
                lines.append(f"  {prio} {t.title}")

        reply_markup = {
            "inline_keyboard": [
                [
                    {"text": "Aufgaben", "callback_data": "tasks_today"},
                    {"text": "Top Matches", "callback_data": "briefing_top_matches"},
                ],
            ],
        }

        await send_message("\n".join(lines), chat_id=chat_id, reply_markup=reply_markup)

    except Exception as e:
        logger.error(f"Briefing fehlgeschlagen: {e}", exc_info=True)
        await send_message(f"Fehler beim Erstellen des Briefings: {str(e)[:200]}", chat_id=chat_id)


async def _handle_stats(chat_id: str) -> None:
    """Zeigt aktuelle Statistiken."""
    try:
        from app.database import async_session_maker
        from app.models.candidate import Candidate
        from app.models.job import Job
        from app.models.match import Match
        from sqlalchemy import select, func

        async with async_session_maker() as db:
            jobs_count = (await db.execute(
                select(func.count()).select_from(Job).where(Job.deleted_at.is_(None))
            )).scalar() or 0

            candidates_count = (await db.execute(
                select(func.count()).select_from(Candidate)
            )).scalar() or 0

            matches_count = (await db.execute(
                select(func.count()).select_from(Match)
            )).scalar() or 0

            top_matches = (await db.execute(
                select(func.count()).select_from(Match).where(Match.v2_score >= 75)
            )).scalar() or 0

        await send_message(
            "<b>PulsePoint Statistiken</b>\n\n"
            f"Jobs (aktiv): {jobs_count}\n"
            f"Kandidaten: {candidates_count}\n"
            f"Matches gesamt: {matches_count}\n"
            f"Top-Matches (>75): {top_matches}",
            chat_id=chat_id,
        )

    except Exception as e:
        logger.error(f"Stats fehlgeschlagen: {e}", exc_info=True)
        await send_message(f"Fehler: {str(e)[:200]}", chat_id=chat_id)


# ── Free-Text Handler ────────────────────────────────────────────

async def _handle_free_text(chat_id: str, text: str) -> None:
    """Verarbeitet Freitext-Nachrichten via GPT Intent-Klassifikation."""
    try:
        from app.services.telegram_intent_service import classify_intent

        result = await classify_intent(text)
        intent = result.get("intent", "unknown")
        entities = result.get("entities", {})

        if intent == "task_list":
            await _handle_task_list(chat_id)
        elif intent == "task_create":
            await _handle_task_create(chat_id, entities)
        elif intent == "task_complete":
            name = entities.get("name", entities.get("title", ""))
            if name:
                await send_message(
                    f"Welche Aufgabe meinst du?\n"
                    f"Bitte verwende: /done &lt;Aufgaben-ID&gt;",
                    chat_id=chat_id,
                )
            else:
                await send_message("Bitte gib die Aufgaben-ID an: /done &lt;id&gt;", chat_id=chat_id)
        elif intent == "candidate_search":
            name = entities.get("name", "")
            if name:
                await _handle_candidate_search(chat_id, name)
            else:
                await send_message("Wen suchst du? Bitte gib einen Namen an.", chat_id=chat_id)
        elif intent == "briefing":
            await _handle_briefing(chat_id)
        elif intent == "call_log":
            from app.services.telegram_call_handler import handle_call_log
            await handle_call_log(chat_id, text)
        elif intent == "email_send":
            from app.services.telegram_email_handler import handle_email_send
            await handle_email_send(chat_id, text, entities)
        else:
            await send_message(
                "Das habe ich nicht verstanden. Tippe /help fuer verfuegbare Kommandos.",
                chat_id=chat_id,
            )

    except Exception as e:
        logger.error(f"Freitext-Handler fehlgeschlagen: {e}", exc_info=True)
        await send_message(f"Fehler: {str(e)[:200]}", chat_id=chat_id)


# ── Voice Handler ────────────────────────────────────────────────

async def _handle_voice_message(chat_id: str, message: dict) -> None:
    """Verarbeitet Voice-Nachrichten: Whisper -> Intent -> Handler."""
    try:
        from app.services.telegram_intent_service import transcribe_voice

        voice = message["voice"]
        file_id = voice["file_id"]

        # Feedback: Verarbeitung laeuft
        await send_message("Sprachnachricht wird verarbeitet...", chat_id=chat_id)

        # Audio herunterladen
        audio_bytes = await _download_file(file_id)
        if not audio_bytes:
            await send_message("Konnte die Sprachnachricht nicht herunterladen.", chat_id=chat_id)
            return

        # Whisper-Transkription
        text = await transcribe_voice(audio_bytes)
        if not text:
            await send_message("Konnte die Sprachnachricht nicht transkribieren.", chat_id=chat_id)
            return

        # Transkription anzeigen
        await send_message(f"<i>Transkription:</i>\n{text[:500]}", chat_id=chat_id)

        # Als Freitext weiterverarbeiten
        await _handle_free_text(chat_id, text)

    except Exception as e:
        logger.error(f"Voice-Handler fehlgeschlagen: {e}", exc_info=True)
        await send_message(f"Fehler bei der Sprachverarbeitung: {str(e)[:200]}", chat_id=chat_id)


# ── Callback Query Handler ───────────────────────────────────────

async def _handle_callback_query(callback_query: dict) -> None:
    """Verarbeitet Inline-Keyboard Button-Klicks."""
    try:
        callback_data = callback_query.get("data", "")
        chat_id = str(callback_query.get("message", {}).get("chat", {}).get("id", ""))
        callback_id = callback_query.get("id")

        # Sicherheitscheck
        if settings.telegram_chat_id and chat_id != settings.telegram_chat_id:
            return

        if callback_data == "tasks_today" or callback_data == "tasks_all":
            await answer_callback_query(callback_id, "Lade Aufgaben...")
            await _handle_task_list(chat_id)

        elif callback_data == "tasks_upcoming":
            await answer_callback_query(callback_id, "Lade naechste 7 Tage...")
            await _handle_upcoming_tasks(chat_id)

        elif callback_data == "briefing_top_matches":
            await answer_callback_query(callback_id, "Lade Top-Matches...")
            await _handle_top_matches(chat_id)

        elif callback_data.startswith("complete_"):
            task_id = callback_data.replace("complete_", "")
            await answer_callback_query(callback_id, "Wird erledigt...")
            await _handle_task_complete(chat_id, task_id)

        elif callback_data.startswith("email_send_"):
            from app.services.telegram_email_handler import handle_email_callback
            await handle_email_callback(chat_id, callback_data, callback_id)

        else:
            await answer_callback_query(callback_id, "Unbekannte Aktion")

    except Exception as e:
        logger.error(f"Callback Query fehlgeschlagen: {e}", exc_info=True)


async def _handle_upcoming_tasks(chat_id: str) -> None:
    """Zeigt Aufgaben der naechsten 7 Tage."""
    try:
        from app.database import async_session_maker
        from app.services.ats_todo_service import ATSTodoService

        async with async_session_maker() as db:
            service = ATSTodoService(db)
            todos = await service.get_upcoming_todos(days=7)

        if not todos:
            await send_message("Keine Aufgaben in den naechsten 7 Tagen.", chat_id=chat_id)
            return

        lines = [f"<b>Naechste 7 Tage ({len(todos)} Aufgaben):</b>\n"]
        for t in todos[:20]:
            prio = _priority_emoji(t.priority.value)
            due = t.due_date.strftime("%d.%m.") if t.due_date else ""
            time_str = f" {t.due_time}" if t.due_time else ""
            lines.append(f"  {prio} <b>{due}{time_str}</b> — {t.title}")

        await send_message("\n".join(lines), chat_id=chat_id)

    except Exception as e:
        logger.error(f"Upcoming Tasks fehlgeschlagen: {e}", exc_info=True)
        await send_message(f"Fehler: {str(e)[:200]}", chat_id=chat_id)


async def _handle_top_matches(chat_id: str) -> None:
    """Zeigt die besten unbearbeiteten Matches."""
    try:
        from app.database import async_session_maker
        from app.models.match import Match
        from app.models.job import Job
        from app.models.candidate import Candidate
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        async with async_session_maker() as db:
            stmt = (
                select(Match)
                .options(selectinload(Match.job), selectinload(Match.candidate))
                .where(
                    Match.v2_score >= 75,
                    Match.outreach_status.is_(None),
                )
                .order_by(Match.v2_score.desc())
                .limit(10)
            )
            result = await db.execute(stmt)
            matches = result.scalars().all()

        if not matches:
            await send_message("Keine unbearbeiteten Top-Matches.", chat_id=chat_id)
            return

        lines = [f"<b>Top Matches ({len(matches)}):</b>\n"]
        for m in matches:
            score = f"{m.v2_score:.0f}" if m.v2_score else "—"
            cand_name = f"{m.candidate.first_name or ''} {m.candidate.last_name or ''}".strip() if m.candidate else "—"
            job_title = m.job.position if m.job else "—"
            company = m.job.company_name if m.job else "—"
            drive = ""
            if m.drive_time_car_min:
                drive = f" | {m.drive_time_car_min} min"
            lines.append(f"  <b>{score}%</b> — {cand_name}")
            lines.append(f"  {job_title} @ {company}{drive}")
            lines.append("")

        await send_message("\n".join(lines), chat_id=chat_id)

    except Exception as e:
        logger.error(f"Top-Matches fehlgeschlagen: {e}", exc_info=True)
        await send_message(f"Fehler: {str(e)[:200]}", chat_id=chat_id)


async def _handle_stelle_pdf(chat_id: str, job_id_str: str) -> None:
    """Generiert und sendet ein Stelle-PDF via Telegram."""
    try:
        from app.database import async_session_maker
        from app.services.ats_job_pdf_service import ATSJobPdfService

        job_id = UUID(job_id_str.strip())

        async with async_session_maker() as db:
            service = ATSJobPdfService(db)
            pdf_bytes = await service.generate_stelle_pdf(job_id)

        await send_document(pdf_bytes, filename=f"Stelle_{job_id_str[:8]}.pdf", caption="Stellenbeschreibung PDF")

    except ValueError:
        await send_message("Ungueltige Stellen-ID. Bitte die UUID angeben.", chat_id=chat_id)
    except Exception as e:
        logger.error(f"Stelle-PDF Generierung fehlgeschlagen: {e}", exc_info=True)
        await send_message(f"Fehler bei der PDF-Generierung: {str(e)[:200]}", chat_id=chat_id)


# ── Helpers ──────────────────────────────────────────────────────

def _priority_emoji(priority: str) -> str:
    """Gibt ein Emoji fuer die Prioritaet zurueck."""
    return {
        "sehr_dringend": "[!!!]",
        "dringend": "[!!]",
        "wichtig": "[!]",
        "mittelmaessig": "[-]",
        "unwichtig": "[.]",
    }.get(priority, "[-]")
