"""Telegram Call Handler — Call-Logging via Telegram Voice/Text.

Verarbeitet Anrufzusammenfassungen die per Telegram gesendet werden.
Erkennt automatisch ob es ein Kandidaten- oder Kundengespräch war.
Nutzt die bestehende CallTranscriptionService Pipeline.
"""

import logging
from datetime import date, datetime, timezone
from uuid import UUID

from app.config import settings

logger = logging.getLogger(__name__)

# Felder die vom Job-Quali Ergebnis auf ATSJob gemappt werden
QUALIFICATION_FIELDS = [
    "team_size", "erp_system", "home_office_days", "flextime", "core_hours",
    "vacation_days", "overtime_handling", "open_office", "english_requirements",
    "hiring_process_steps", "feedback_timeline", "digitalization_level",
    "older_candidates_ok", "desired_start_date", "interviews_started",
    "ideal_candidate_description", "candidate_tasks", "multiple_entities",
    "task_distribution", "salary_min", "salary_max", "employment_type",
    "description", "requirements",
]


async def handle_call_log(
    chat_id: str,
    text: str,
    is_voice_transcript: bool = False,
) -> None:
    """Verarbeitet eine Call-Log Nachricht aus Telegram.

    Flow:
    1. GPT: Ist es ein Kandidaten- oder Kundengespräch?
    2. Entity Resolution: Firma/Kandidat im System suchen
    3. Daten extrahieren (CallTranscriptionService)
    4. ATSCallNote erstellen
    5. Bei Job-Quali: ATSJob-Felder updaten
    6. Todos aus Action Items erstellen
    7. Zusammenfassung an Telegram senden
    """
    try:
        from app.services.telegram_bot_service import send_message

        # Schritt 1: Gesprächstyp klassifizieren
        call_type, classification = await _classify_call_type(text)

        if call_type == "candidate":
            await _handle_candidate_call(chat_id, text, classification)
        elif call_type == "customer":
            await _handle_customer_call(chat_id, text, classification)
        else:
            await send_message(
                "<b>Anruf protokolliert</b>\n\n"
                f"<i>Typ: Sonstiges</i>\n\n"
                f"{text[:300]}...\n\n"
                "Konnte keinen Kandidaten oder Kunden zuordnen. "
                "Bitte manuell im System erfassen.",
                chat_id=chat_id,
            )

    except Exception as e:
        logger.error(f"Call-Log Verarbeitung fehlgeschlagen: {e}", exc_info=True)
        from app.services.telegram_bot_service import send_message
        await send_message(
            f"Fehler bei der Anruf-Verarbeitung: {str(e)[:200]}",
            chat_id=chat_id,
        )


async def _classify_call_type(text: str) -> tuple[str, dict]:
    """Klassifiziert ob es ein Kandidaten- oder Kundengespräch ist.

    Returns:
        ("candidate" | "customer" | "unknown", classification_dict)
    """
    import json
    import httpx

    prompt = """Analysiere diese Gesprächszusammenfassung und bestimme:
1. Ist es ein Gespräch mit einem KANDIDATEN (Bewerber, Jobsuchender) oder einem KUNDEN (Firma, Ansprechpartner)?
2. Extrahiere den Namen der Person/Firma.

Antworte NUR als JSON:
{
  "type": "candidate" | "customer" | "unknown",
  "person_name": "Name der Person" | null,
  "company_name": "Name der Firma" | null,
  "confidence": 0.0-1.0,
  "reasoning": "Kurze Begruendung"
}"""

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
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": text},
                    ],
                    "temperature": 0.0,
                    "max_tokens": 200,
                },
            )
            response.raise_for_status()
            data = response.json()

        content = data["choices"][0]["message"]["content"].strip()
        result = json.loads(content)
        return result.get("type", "unknown"), result

    except Exception as e:
        logger.error(f"Call-Typ Klassifizierung fehlgeschlagen: {e}")
        return "unknown", {}


async def _handle_candidate_call(chat_id: str, text: str, classification: dict) -> None:
    """Verarbeitet ein Kandidatengespräch."""
    from app.database import async_session_maker
    from app.services.telegram_bot_service import send_message

    person_name = classification.get("person_name", "")

    # Kandidat suchen
    candidate_id = None
    candidate_name = person_name

    if person_name:
        async with async_session_maker() as db:
            from app.models.candidate import Candidate
            from sqlalchemy import select, or_

            search_term = f"%{person_name}%"
            stmt = (
                select(Candidate)
                .where(
                    or_(
                        Candidate.first_name.ilike(search_term),
                        Candidate.last_name.ilike(search_term),
                        (Candidate.first_name + " " + Candidate.last_name).ilike(search_term),
                    )
                )
                .limit(3)
            )
            result = await db.execute(stmt)
            candidates = result.scalars().all()

            if len(candidates) == 1:
                candidate_id = candidates[0].id
                candidate_name = f"{candidates[0].first_name or ''} {candidates[0].last_name or ''}".strip()

    # Daten extrahieren via CallTranscriptionService
    extracted_data = {}
    summary = text[:500]

    if candidate_id:
        try:
            async with async_session_maker() as db:
                from app.services.call_transcription_service import CallTranscriptionService
                service = CallTranscriptionService(db)
                result = await service.process_call(
                    candidate_id=candidate_id,
                    transcript_text=text,
                )
                await db.commit()

                if result.get("success"):
                    extracted_data = result.get("extracted_data", {})
                    summary = extracted_data.get("summary", text[:500])
        except Exception as e:
            logger.error(f"CallTranscriptionService fehlgeschlagen: {e}")

    # CallNote erstellen
    try:
        async with async_session_maker() as db:
            from app.models.ats_call_note import ATSCallNote, CallType, CallDirection

            call_note = ATSCallNote(
                candidate_id=candidate_id,
                call_type=CallType.CANDIDATE_CALL,
                direction=CallDirection.OUTBOUND,
                summary=summary,
                raw_notes=text,
                action_items=extracted_data.get("action_items") if extracted_data else None,
                called_at=datetime.now(timezone.utc),
            )
            db.add(call_note)
            await db.commit()

            # Todos aus Action Items erstellen
            if extracted_data and extracted_data.get("action_items"):
                from app.services.ats_todo_service import ATSTodoService
                todo_service = ATSTodoService(db)
                for action in extracted_data["action_items"][:5]:
                    if isinstance(action, str) and action.strip():
                        await todo_service.create_todo(
                            title=action.strip()[:200],
                            priority="wichtig",
                            due_date=date.today(),
                            candidate_id=candidate_id,
                        )
                await db.commit()
    except Exception as e:
        logger.error(f"CallNote Erstellung fehlgeschlagen: {e}")

    # Telegram-Antwort
    msg_lines = [
        "<b>Kandidaten-Anruf protokolliert</b>\n",
        f"Kandidat: <b>{candidate_name}</b>",
    ]

    if extracted_data:
        if extracted_data.get("desired_positions"):
            msg_lines.append(f"Wunschposition: {extracted_data['desired_positions']}")
        if extracted_data.get("salary"):
            msg_lines.append(f"Gehalt: {extracted_data['salary']}")
        if extracted_data.get("notice_period"):
            msg_lines.append(f"Kuendigungsfrist: {extracted_data['notice_period']}")
        if extracted_data.get("home_office_days"):
            msg_lines.append(f"Home-Office: {extracted_data['home_office_days']}")

    msg_lines.append(f"\n<i>{summary[:300]}</i>")

    if extracted_data and extracted_data.get("action_items"):
        msg_lines.append("\n<b>Action Items:</b>")
        for a in extracted_data["action_items"][:5]:
            if isinstance(a, str):
                msg_lines.append(f"  - {a}")

    await send_message("\n".join(msg_lines), chat_id=chat_id)


async def _handle_customer_call(chat_id: str, text: str, classification: dict) -> None:
    """Verarbeitet ein Kundengespräch (inkl. Job-Qualifizierung)."""
    from app.database import async_session_maker
    from app.services.telegram_bot_service import send_message

    company_name = classification.get("company_name", "Unbekannt")
    person_name = classification.get("person_name", "")

    # Firma + Kontakt suchen
    company_id = None
    contact_id = None

    if company_name and company_name != "Unbekannt":
        try:
            async with async_session_maker() as db:
                from app.models.company import Company
                from sqlalchemy import select

                search_term = f"%{company_name}%"
                stmt = select(Company).where(Company.name.ilike(search_term)).limit(3)
                result = await db.execute(stmt)
                companies = result.scalars().all()

                if len(companies) == 1:
                    company_id = companies[0].id
                    company_name = companies[0].name
        except Exception as e:
            logger.error(f"Firmensuche fehlgeschlagen: {e}")

    # Subtyp klassifizieren + Daten extrahieren via CallTranscriptionService
    subtype_result = {}
    job_data = {}

    try:
        async with async_session_maker() as db:
            from app.services.call_transcription_service import CallTranscriptionService
            service = CallTranscriptionService(db)
            subtype_result = await service.process_contact_call(
                transcript=text,
                contact_name=person_name or "Unbekannt",
                company_name=company_name,
            )

            if subtype_result.get("success") and subtype_result.get("subtype") == "job_quali":
                job_data = subtype_result.get("job_data", {})
    except Exception as e:
        logger.error(f"Kontakt-Call Verarbeitung fehlgeschlagen: {e}")

    subtype = subtype_result.get("subtype", "sonstiges")
    summary = subtype_result.get("summary", text[:500])

    # CallNote erstellen
    ats_job_id = None
    try:
        async with async_session_maker() as db:
            from app.models.ats_call_note import ATSCallNote, CallType, CallDirection

            call_note = ATSCallNote(
                company_id=company_id,
                call_type=CallType.ACQUISITION if subtype == "job_quali" else CallType.FOLLOWUP,
                direction=CallDirection.OUTBOUND,
                summary=summary,
                raw_notes=text,
                action_items=subtype_result.get("action_items") if subtype_result else None,
                called_at=datetime.now(timezone.utc),
            )
            db.add(call_note)
            await db.flush()

            # Bei Job-Quali: ATSJob erstellen/updaten
            if subtype == "job_quali" and job_data:
                ats_job_id = await _apply_job_qualification(db, company_id, contact_id, call_note.id, job_data)

            await db.commit()

            # Todos erstellen
            action_items = subtype_result.get("action_items", [])
            if not action_items and subtype == "follow_up" and subtype_result.get("follow_up_reason"):
                action_items = [f"Follow-up: {subtype_result['follow_up_reason']}"]

            if action_items:
                from app.services.ats_todo_service import ATSTodoService
                todo_service = ATSTodoService(db)
                for action in (action_items[:5] if isinstance(action_items, list) else []):
                    if isinstance(action, str) and action.strip():
                        await todo_service.create_todo(
                            title=action.strip()[:200],
                            priority="wichtig",
                            due_date=date.today(),
                            company_id=company_id,
                        )
                await db.commit()
    except Exception as e:
        logger.error(f"CallNote/Job-Quali Erstellung fehlgeschlagen: {e}")

    # Telegram-Antwort
    subtype_labels = {
        "kein_bedarf": "Kein Bedarf",
        "follow_up": "Follow-up noetig",
        "job_quali": "Job-Qualifizierung",
        "sonstiges": "Sonstiges",
    }

    msg_lines = [
        "<b>Kunden-Anruf protokolliert</b>\n",
        f"Firma: <b>{company_name}</b>",
        f"Ergebnis: <b>{subtype_labels.get(subtype, subtype)}</b>",
    ]

    if subtype == "job_quali" and job_data:
        msg_lines.append("")
        msg_lines.append("<b>Qualifizierte Stelle:</b>")
        if job_data.get("title"):
            msg_lines.append(f"  Position: {job_data['title']}")
        if job_data.get("salary_min") or job_data.get("salary_max"):
            sal_min = job_data.get("salary_min", "?")
            sal_max = job_data.get("salary_max", "?")
            msg_lines.append(f"  Gehalt: {sal_min} - {sal_max} EUR")
        if job_data.get("team_size"):
            msg_lines.append(f"  Team: {job_data['team_size']}")
        if job_data.get("erp_system"):
            msg_lines.append(f"  ERP: {job_data['erp_system']}")
        if job_data.get("home_office_days"):
            msg_lines.append(f"  Home-Office: {job_data['home_office_days']}")
        if ats_job_id:
            msg_lines.append(f"\n  Stelle im System gespeichert.")

    elif subtype == "follow_up":
        if subtype_result.get("follow_up_date"):
            msg_lines.append(f"Follow-up am: {subtype_result['follow_up_date']}")
        if subtype_result.get("follow_up_reason"):
            msg_lines.append(f"Grund: {subtype_result['follow_up_reason']}")

    msg_lines.append(f"\n<i>{summary[:300]}</i>")

    await send_message("\n".join(msg_lines), chat_id=chat_id)


async def _apply_job_qualification(
    db,
    company_id: UUID | None,
    contact_id: UUID | None,
    call_note_id: UUID,
    job_data: dict,
) -> UUID | None:
    """Speichert Job-Quali Daten auf einem ATSJob.

    Sucht zuerst nach einer bestehenden offenen Stelle fuer die Firma.
    Wenn nicht vorhanden, erstellt eine neue.
    Updatet nur NULL-Felder (ueberschreibt keine manuellen Eintraege).

    Returns:
        ATSJob ID oder None bei Fehler
    """
    try:
        from app.models.ats_job import ATSJob, ATSJobStatus
        from sqlalchemy import select

        ats_job = None

        # Bestehende offene Stelle suchen
        if company_id:
            stmt = (
                select(ATSJob)
                .where(
                    ATSJob.company_id == company_id,
                    ATSJob.status == ATSJobStatus.OPEN,
                    ATSJob.deleted_at.is_(None),
                )
                .order_by(ATSJob.created_at.desc())
                .limit(1)
            )
            result = await db.execute(stmt)
            ats_job = result.scalar_one_or_none()

        if ats_job:
            # Bestehende Stelle updaten (nur NULL-Felder)
            for field in QUALIFICATION_FIELDS:
                value = job_data.get(field)
                if value is not None and getattr(ats_job, field, None) is None:
                    setattr(ats_job, field, value)

            # Title updaten wenn noch leer
            if job_data.get("title") and not ats_job.title:
                ats_job.title = job_data["title"]

            if not ats_job.source_call_note_id:
                ats_job.source_call_note_id = call_note_id

        else:
            # Neue Stelle erstellen
            ats_job = ATSJob(
                company_id=company_id,
                contact_id=contact_id,
                source_call_note_id=call_note_id,
                title=job_data.get("title", "Neue Stelle"),
                source="Telegram Call-Log",
                status=ATSJobStatus.OPEN,
            )

            # Alle Quali-Felder setzen
            for field in QUALIFICATION_FIELDS:
                value = job_data.get(field)
                if value is not None:
                    setattr(ats_job, field, value)

            # Location
            if job_data.get("location"):
                ats_job.location_city = job_data["location"]

            db.add(ats_job)

        await db.flush()
        logger.info(f"ATSJob {'aktualisiert' if ats_job.id else 'erstellt'}: {ats_job.title}")
        return ats_job.id

    except Exception as e:
        logger.error(f"Job-Quali Speicherung fehlgeschlagen: {e}", exc_info=True)
        return None
