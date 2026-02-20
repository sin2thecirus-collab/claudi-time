"""Telegram Call Handler — Call-Logging via Telegram Voice/Text.

Verwendet den gleichen GPT-Prompt wie der Webex n8n Workflow
und die gleiche store-or-assign Pipeline fuer einheitliche Verarbeitung.
"""

import json
import logging
from datetime import datetime, timezone

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# ── GPT System-Prompt: Identisch mit dem Webex n8n Workflow ──
CALL_EXTRACTION_SYSTEM_PROMPT = (
    "Du bist ein Recruiting-Assistent fuer eine Personalberatung (Sincirus GmbH). "
    "Du analysierst Transkripte von Telefongespraechen, klassifizierst sie und "
    "extrahierst strukturierte Daten. Antworte IMMER als valides JSON-Objekt. "
    "Wenn eine Information nicht im Gespraech erwaehnt wird, setze den Wert auf null.\n\n"
    "=== KLASSIFIZIERUNG (call_type) - HARTE REGELN ===\n\n"
    "Die 2 PFLICHT-Themen fuer ein Qualifizierungsgespraech sind:\n"
    "(1) Gehaltsvorstellung\n"
    "(2) Kuendigungsfrist\n\n"
    "Klassifizierung:\n\n"
    '1. "qualifizierung" - Sobald BEIDE Pflicht-Themen (Gehalt UND Kuendigungsfrist) '
    "im Gespraech genannt werden, ist es IMMER ein Qualifizierungsgespraech. "
    "Egal wie kurz das Gespraech ist. Egal ob andere Themen fehlen. "
    "Diese 2 Themen sind der einzige Massstab.\n\n"
    '2. "kurzer_call" - Kandidatengespraech bei dem NICHT beide Pflicht-Themen '
    "besprochen wurden. Z.B. Terminvereinbarung, Status-Update, Erstansprache, "
    "nur Gehalt ODER nur Kuendigungsfrist erwaehnt.\n\n"
    '3. "akquise" - Gespraech mit Unternehmen/Kunden (NICHT mit Kandidaten): '
    "Vorstellung, Auftragsannahme, Stellenbesprechung, Feedback\n\n"
    '4. "sonstiges" - Alles andere: Interne Gespraeche, Testanrufe, nicht erkennbar\n\n'
    "=== DATENEXTRAKTION ===\n"
    "Extrahiere IMMER alle Felder. Bei kurzen Gespraechen sind die meisten null - das ist OK.\n\n"
    "=== ZUSAMMENFASSUNG (call_summary) ===\n"
    'SCHREIBE NIEMALS Dinge wie "KI-transkribiertes Gespraech" oder "automatisch erfasstes Telefonat". '
    "Schreibe IMMER eine inhaltliche Zusammenfassung: Was wurde besprochen? "
    "Was sind die Ergebnisse? Was sind die naechsten Schritte? 2-4 Saetze.\n\n"
    "=== ACTION ITEMS / FOLLOW-UPS (WICHTIG!) ===\n"
    "Analysiere das Gespraech GRUENDLICH auf ALLE Aufgaben, Zusagen und Vereinbarungen.\n\n"
    "REGELN fuer Action Items:\n"
    "1. PERSPEKTIVE: Schreibe IMMER aus Sicht des Recruiters (Sincirus). "
    "Der Recruiter liest diese Aufgaben.\n"
    "2. KANDIDATEN-NAME: Nenne IMMER den Namen des Kandidaten/Kontakts im Titel, "
    "damit man nach 2 Wochen noch weiss worum es geht.\n"
    "3. KONTEXT: Schreibe KONKRET was zu tun ist, nicht vage Saetze.\n"
    "4. CONTEXT-FELD: Ergaenze zu jeder Aufgabe 2-3 Saetze Kontext: Was genau wurde "
    "besprochen? Warum ist diese Aufgabe wichtig? Welche Details aus dem Gespraech sind relevant?\n"
    '5. UHRZEIT: Wenn im Gespraech eine Uhrzeit genannt wird, extrahiere diese als due_time im Format "HH:MM".\n'
    '6. IMPLIZITE AUFGABEN ERFASSEN: Wenn der Kandidat sagt "Ich spreche Montag mit meinem Chef" '
    'oder "Ich klaere das naechste Woche", dann erstelle IMMER eine Follow-up-Aufgabe fuer den Recruiter.\n'
    '7. GUTE Beispiele: "Max Mueller: CV-Eingang pruefen", "Anna Schmidt: Rueckruf um 14:00", '
    '"Thomas Weber: Follow-up nach Chef-Gespraech"\n'
    '8. SCHLECHTE Beispiele: "Lebenslauf erhalten", "Nach dem Gespraech fragen"\n\n'
    "Jedes Action Item: {title, context (2-3 Saetze Kontext), due_date (ISO), "
    'due_time ("HH:MM" oder null), priority (hoch/mittel/niedrig)}\n\n'
    "=== ERLEDIGTE AUFGABEN (completed_tasks) ===\n"
    "Analysiere ob in diesem Gespraech BESTEHENDE Aufgaben erledigt wurden.\n"
    "Indikatoren: Versprochener Rueckruf durchgefuehrt, zugesagte Info geliefert, "
    "vereinbarter Termin eingehalten, Kandidat hat etwas erledigt das er zuvor angekuendigt hatte, "
    "Feedback gegeben das vorher ausstehend war.\n"
    "Fuer jede Erledigung: {reason, match_hint (Schlagwoerter komma-separiert), person_name}\n"
    "Wenn nichts erledigt: null oder leeres Array.\n\n"
    "=== EMAIL-AKTIONEN (email_actions) ===\n"
    "Wenn im Gespraech eine Email/WhatsApp versprochen wird:\n"
    '1. "kontaktdaten" - Recruiter schickt Kontaktdaten (IMMER bei Qualifizierung)\n'
    '2. "stellenausschreibung" - Konkrete Stelle per Email. Extrahiere job_keywords.\n'
    '3. "individuell" - Andere Email-Zusagen.\n'
    "Jede email_action: {type, description, job_keywords (nur bei stellenausschreibung), urgency}"
)


def _build_call_extraction_user_prompt(transcript: str) -> str:
    """Baut den User-Prompt fuer die Call-Extraktion (identisch mit n8n Workflow)."""
    today = datetime.now().strftime("%Y-%m-%d")
    return (
        f"Heutiges Datum: {today}\n\n"
        "Analysiere das folgende Transkript und extrahiere als JSON:\n\n"
        "Felder:\n"
        '- call_type (string): "qualifizierung" / "kurzer_call" / "akquise" / "sonstiges"\n'
        "- pflichtthemen_erkannt (object): {gehalt: true/false, kuendigungsfrist: true/false}\n"
        "- desired_positions (string[]): Gesuchte Positionen/Rollen\n"
        "- key_activities (string[]): Haupttaetigkeiten des Kandidaten\n"
        "- home_office_days (number|null): Home-Office Tage/Woche\n"
        "- commute_max (string|null): Max. Pendelzeit/-entfernung\n"
        "- commute_transport (string|null): Auto/OePNV/beides\n"
        "- erp_main (string|null): Hauptsaechliches ERP-System\n"
        "- employment_type (string|null): Vollzeit/Teilzeit\n"
        "- part_time_hours (number|null): Teilzeit-Stunden/Woche\n"
        "- preferred_industries (string[]): Bevorzugte Branchen\n"
        "- avoided_industries (string[]): Branchen vermeiden\n"
        "- salary (string|null): Gehaltsvorstellung\n"
        "- notice_period (string|null): Kuendigungsfrist\n"
        "- willingness_to_change (string): ja/nein/unklar\n"
        "- call_summary (string): Inhaltliche Zusammenfassung 2-4 Saetze\n"
        "- action_items (array|null): [{title, context, due_date, due_time, priority}] "
        "-- Kandidatenname in jeden Titel! context = 2-3 Saetze Kontext!\n"
        "- completed_tasks (array|null): [{reason, match_hint, person_name}] "
        "-- Aufgaben die in diesem Gespraech ERLEDIGT wurden.\n"
        "- email_actions (array|null): [{type, description, job_keywords, urgency}] "
        "-- Email/WhatsApp-Versprechen.\n\n"
        f"TRANSKRIPT:\n{transcript}"
    )


async def handle_call_log(
    chat_id: str,
    text: str,
    is_voice_transcript: bool = False,
) -> None:
    """Verarbeitet eine Call-Log Nachricht aus Telegram.

    Nutzt den gleichen GPT-Prompt wie der Webex n8n Workflow und
    schickt das Ergebnis an die store-or-assign Pipeline.
    """
    try:
        from app.services.telegram_bot_service import send_message

        await send_message("Analysiere Gespraech...", chat_id=chat_id)

        # ── Schritt 1: GPT-Extraktion (gleicher Prompt wie Webex n8n) ──
        extracted_data = await _extract_call_data(text)
        if not extracted_data:
            await send_message(
                "Konnte das Gespraech nicht analysieren. Bitte erneut versuchen.",
                chat_id=chat_id,
            )
            return

        call_type = extracted_data.get("call_type", "sonstiges")
        summary = extracted_data.get("call_summary", text[:500])

        # ── Schritt 2: An store-or-assign Pipeline senden ──
        mt_payload = {
            "call_desired_positions": extracted_data.get("desired_positions", []),
            "call_key_activities": extracted_data.get("key_activities", []),
            "call_home_office": extracted_data.get("home_office_days"),
            "call_commute_willingness": extracted_data.get("commute_max"),
            "call_transport": extracted_data.get("commute_transport"),
            "call_erp_specialty": extracted_data.get("erp_main"),
            "call_employment_type": extracted_data.get("employment_type"),
            "call_part_time_hours": extracted_data.get("part_time_hours"),
            "call_preferred_industries": extracted_data.get("preferred_industries", []),
            "call_avoided_industries": extracted_data.get("avoided_industries", []),
            "call_salary": extracted_data.get("salary"),
            "call_notice_period": extracted_data.get("notice_period"),
            "call_willingness_to_change": extracted_data.get("willingness_to_change"),
            "call_summary": summary,
            "last_call_date": datetime.now(timezone.utc).isoformat(),
            "completed_tasks": extracted_data.get("completed_tasks", []),
        }

        # Intern die store-or-assign Logik aufrufen (direkt via Service, nicht HTTP)
        assign_result = await _call_store_or_assign(
            call_type=call_type,
            transcript=text if call_type == "qualifizierung" else "",
            call_summary=summary,
            extracted_data=extracted_data,
            mt_payload=mt_payload,
        )

        # ── Schritt 3: Telegram-Antwort formatieren ──
        msg = _format_telegram_response(call_type, extracted_data, assign_result)
        await send_message(msg, chat_id=chat_id)

    except Exception as e:
        logger.error(f"Call-Log Verarbeitung fehlgeschlagen: {e}", exc_info=True)
        from app.services.telegram_bot_service import send_message
        await send_message(
            f"Fehler bei der Anruf-Verarbeitung: {str(e)[:200]}",
            chat_id=chat_id,
        )


async def _extract_call_data(transcript: str) -> dict | None:
    """Extrahiert strukturierte Daten aus einem Gespraechstranskript via GPT-4o-mini.

    Verwendet den identischen Prompt wie der Webex n8n Workflow.
    """
    if not settings.openai_api_key:
        logger.warning("OpenAI API Key nicht konfiguriert")
        return None

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.openai_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "gpt-4o-mini",
                    "temperature": 0.1,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": CALL_EXTRACTION_SYSTEM_PROMPT},
                        {"role": "user", "content": _build_call_extraction_user_prompt(transcript)},
                    ],
                },
            )
            response.raise_for_status()
            data = response.json()

        content = data["choices"][0]["message"]["content"]
        result = json.loads(content) if isinstance(content, str) else content
        logger.info(f"Call-Extraktion erfolgreich: call_type={result.get('call_type')}")
        return result

    except Exception as e:
        logger.error(f"Call-Extraktion fehlgeschlagen: {e}")
        return None


async def _call_store_or_assign(
    call_type: str,
    transcript: str,
    call_summary: str,
    extracted_data: dict,
    mt_payload: dict,
) -> dict:
    """Ruft die store-or-assign Pipeline intern auf.

    Nutzt die gleiche Logik wie der n8n Webhook-Endpoint, aber
    direkt ueber die DB statt via HTTP.
    """
    try:
        from app.database import async_session_maker
        from app.models.unassigned_call import UnassignedCall

        async with async_session_maker() as db:
            # Als unassigned_call speichern (wird im Dashboard zur manuellen Zuordnung angezeigt)
            staged_call = UnassignedCall(
                phone_number=None,  # Telegram hat keine Telefonnummer
                direction="outbound",
                call_date=datetime.now(timezone.utc),
                transcript=transcript if transcript else None,
                call_summary=call_summary,
                extracted_data={**extracted_data, "source": "telegram"},
                recording_topic="Telegram Call-Log",
                mt_payload=mt_payload,
                assigned=False,
            )
            db.add(staged_call)
            await db.commit()
            await db.refresh(staged_call)

            logger.info(f"Telegram Call als unassigned_call gespeichert: {staged_call.id}")
            return {
                "status": "staged",
                "unassigned_call_id": str(staged_call.id),
                "call_type": call_type,
            }

    except Exception as e:
        logger.error(f"store-or-assign fehlgeschlagen: {e}", exc_info=True)
        return {"status": "error", "error": str(e)}


def _format_telegram_response(call_type: str, data: dict, assign_result: dict) -> str:
    """Formatiert die Telegram-Antwort nach der Call-Analyse."""
    type_labels = {
        "qualifizierung": "Qualifizierungsgespraech",
        "kurzer_call": "Kurzer Call",
        "akquise": "Akquise/Kundengespraech",
        "sonstiges": "Sonstiges",
    }

    lines = [
        f"<b>Anruf analysiert: {type_labels.get(call_type, call_type)}</b>\n",
    ]

    # Pflichtthemen
    pflicht = data.get("pflichtthemen_erkannt", {})
    if pflicht:
        gehalt = pflicht.get("gehalt", False)
        kuendigung = pflicht.get("kuendigungsfrist", False)
        lines.append(f"Gehalt: {'ja' if gehalt else 'nein'} | Kuendigungsfrist: {'ja' if kuendigung else 'nein'}")

    # Zusammenfassung
    summary = data.get("call_summary", "")
    if summary:
        lines.append(f"\n<i>{summary[:400]}</i>")

    # Extrahierte Daten
    details = []
    if data.get("desired_positions"):
        pos = ", ".join(data["desired_positions"][:3])
        details.append(f"Position: {pos}")
    if data.get("salary"):
        details.append(f"Gehalt: {data['salary']}")
    if data.get("notice_period"):
        details.append(f"Kuendigungsfrist: {data['notice_period']}")
    if data.get("erp_main"):
        details.append(f"ERP: {data['erp_main']}")
    if data.get("home_office_days") is not None:
        details.append(f"Home-Office: {data['home_office_days']} Tage")
    if data.get("employment_type"):
        details.append(f"Anstellung: {data['employment_type']}")
    if data.get("willingness_to_change") and data["willingness_to_change"] != "unklar":
        details.append(f"Wechselbereitschaft: {data['willingness_to_change']}")

    if details:
        lines.append("\n<b>Extrahierte Daten:</b>")
        for d in details:
            lines.append(f"  {d}")

    # Action Items
    action_items = data.get("action_items") or []
    if action_items:
        lines.append("\n<b>Action Items:</b>")
        for item in action_items[:5]:
            if isinstance(item, dict):
                title = item.get("title", "")
                prio = item.get("priority", "")
                prio_icon = {"hoch": "!!", "mittel": "!", "niedrig": ""}.get(prio, "")
                lines.append(f"  - {prio_icon} {title}")
            elif isinstance(item, str):
                lines.append(f"  - {item}")

    # Completed Tasks
    completed = data.get("completed_tasks") or []
    if completed:
        lines.append("\n<b>Erledigte Aufgaben:</b>")
        for task in completed[:3]:
            if isinstance(task, dict):
                lines.append(f"  - {task.get('reason', '')}")

    # Email Actions
    email_actions = data.get("email_actions") or []
    if email_actions:
        lines.append("\n<b>Email-Aktionen:</b>")
        for ea in email_actions[:3]:
            if isinstance(ea, dict):
                lines.append(f"  - {ea.get('type', '')}: {ea.get('description', '')}")

    # Status
    status = assign_result.get("status", "unknown")
    if status == "staged":
        lines.append("\nIm Zwischenspeicher abgelegt — bitte im Dashboard zuordnen.")
    elif status == "auto_assigned":
        entity = assign_result.get("entity_name", "")
        lines.append(f"\nAutomatisch zugeordnet: <b>{entity}</b>")

    return "\n".join(lines)
