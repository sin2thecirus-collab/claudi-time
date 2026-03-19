"""Interview-Scheduling-Service fuer ATS Pipeline.

Erstellt Outlook-Kalendertermine via Microsoft Graph API,
generiert GPT-Einladungstexte und verwaltet Interview-Daten.
"""

import json
import logging
from datetime import datetime, timedelta, timezone

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# ── GPT-Prompt fuer Einladungstext ───────────────────────────────

INTERVIEW_INVITE_PROMPT = """Du bist Milad Hamdard, ein erfahrener Personalberater. Du schreibst eine
persoenliche Einladung zu einem Bewerbungsgespraech an einen Kandidaten.

Der Ton ist WARM, PERSOENLICH und PROFESSIONELL — wie ein Brief von einem Menschen,
nicht wie eine automatisierte E-Mail. Du sprichst den Kandidaten direkt an und freust dich
aufrichtig fuer ihn/sie.

STRUKTUR (EXAKT in dieser Reihenfolge, JEDER Block als eigener <p>-Absatz mit Leerzeile dazwischen):

1. <p>Begruessing + Einleitung (1 Absatz):
   "Hallo [Anrede] [Nachname]," — dann ein persoenlicher Satz wie
   "ich freue mich sehr, Ihnen mitteilen zu duerfen, dass unser Kunde [Firma]
   Sie zu einem Bewerbungsgespraech fuer die Position [Jobtitel] einladen moechte."</p>

2. <p><strong>Termindetails</strong> als saubere Auflistung:</p>
   <table> mit Zeilen fuer Datum, Uhrzeit, Art, Ort — KEIN ul/li, sondern eine
   schlichte HTML-Tabelle ohne Rahmen:
   <table style="border-collapse:collapse; margin:10px 0 10px 0;">
   <tr><td style="padding:4px 16px 4px 0; color:#555;"><strong>Datum:</strong></td><td>[Wochentag], [Tag]. [Monat] [Jahr]</td></tr>
   <tr><td style="padding:4px 16px 4px 0; color:#555;"><strong>Uhrzeit:</strong></td><td>[HH:MM] Uhr</td></tr>
   <tr><td style="padding:4px 16px 4px 0; color:#555;"><strong>Art:</strong></td><td>[Digital / Vor Ort]</td></tr>
   <tr><td style="padding:4px 16px 4px 0; color:#555;"><strong>Ort:</strong></td><td>[Adresse oder "Microsoft Teams"]</td></tr>
   </table>

3. Falls DIGITAL: <p>Hinweis zum Teams-Link — der Link ist in der Kalendereinladung.
   Empfehlung: 5-10 Minuten vorher einloggen, Kamera/Mikro/Internet testen.
   Bei technischen Problemen: Milad anrufen unter 0176 8000 4741.</p>

   Falls VOR ORT: <p>Adresse und Empfangs-Hinweis erwaehnen.</p>

4. <p><strong>Ihre Gespraechspartner:</strong></p>
   <p>JEDEN Teilnehmer EINZELN auflisten — einer pro Zeile mit <br>:
   "• [Anrede] [Vorname] [Nachname] — [Rolle]<br>"
   WICHTIG: ALLE uebergebenen Teilnehmer muessen erscheinen, KEINEN weglassen!</p>

5. <p>Falls verhindert: rechtzeitig melden. Erreichbarkeit: 0176 8000 4741.</p>

6. <p>Abschluss: Persoenlicher Erfolgswunsch.</p>

7. <p>Mit freundlichen Gruessen</p>
   (KEINE Signatur danach — wird automatisch angehaengt)

FORMATIERUNGS-REGELN:
- Antworte NUR mit validem JSON: {"subject": "...", "body_html": "..."}
- body_html MUSS sauberes HTML sein mit <p>-Tags fuer JEDEN Absatz
- ZWISCHEN jedem <p>-Block eine Leerzeile (margin-bottom: 12px auf jedem <p>)
- Verwende inline-styles: <p style="margin:0 0 12px 0;">
- Termindetails als <table> (NICHT als Fliesstext oder ul/li)
- Gespraechspartner mit Bullet-Points (•) und <br> pro Person
- subject: EXAKT dieses Format: "Bewerbungsgespraech [Anrede] [Vorname] [Nachname] — [Firmenname]"
  Beispiel: "Bewerbungsgespraech Frau Kerstin Angerer — Fahrrad XXL Service GmbH"
- Schreibe in der Sie-Form, duze NICHT
- Kein Markdown, kein Code-Block — NUR das JSON-Objekt"""


async def schedule_interview(
    entry_id,
    data: dict,
    db,
) -> dict:
    """Speichert Interview-Daten auf einem Pipeline-Entry.

    Returns: {"success": True, "entry_id": "..."} oder {"success": False, "error": "..."}
    """
    try:
        from app.models.ats_pipeline import ATSPipelineEntry
        from app.models.ats_activity import ActivityType, ATSActivity
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload, attributes

        entry = await db.get(ATSPipelineEntry, entry_id)
        if not entry:
            return {"success": False, "error": "Pipeline-Eintrag nicht gefunden"}

        # Validiere: Entry muss in Interview-Stage sein
        if not entry.stage.value.startswith("interview_"):
            return {"success": False, "error": f"Entry ist in Stage '{entry.stage.value}' — nicht Interview"}

        # Alte Interview-Daten loggen (fuer Historie bei Verschiebung)
        old_data = {}
        if entry.interview_at:
            old_data = {
                "old_interview_at": entry.interview_at.isoformat() if entry.interview_at else None,
                "old_interview_type": entry.interview_type,
                "old_interview_location": entry.interview_location,
            }

        # ── Teilnehmer als CompanyContacts in DB speichern ──
        participants_list = data.get("interview_participants") or []
        if participants_list:
            try:
                from app.models.ats_job import ATSJob
                from app.services.company_service import CompanyService

                job = await db.get(ATSJob, entry.ats_job_id)
                if job and job.company_id:
                    company_svc = CompanyService(db)
                    for p in participants_list:
                        nachname = (p.get("nachname") or "").strip()
                        if not nachname:
                            continue
                        try:
                            contact = await company_svc.get_or_create_contact(
                                company_id=job.company_id,
                                first_name=(p.get("vorname") or "").strip() or None,
                                last_name=nachname,
                                email=(p.get("email") or "").strip() or None,
                                phone=None,
                                salutation=(p.get("anrede") or "").strip() or None,
                                position=(p.get("rolle") or "").strip() or None,
                            )
                            logger.info(f"Kontakt gespeichert/gefunden: {contact.id} — {nachname} fuer Company {job.company_id}")
                        except Exception as ce:
                            logger.error(f"Kontakt-Speicherung fehlgeschlagen: {nachname} — {ce}", exc_info=True)
                    await db.flush()
                    logger.info(f"Teilnehmer als CompanyContacts gespeichert/aktualisiert: {len(participants_list)} fuer Company {job.company_id}")
                else:
                    logger.warning(f"Teilnehmer-Speicherung: Job {entry.ats_job_id} hat keine company_id")
            except Exception as e:
                logger.error(f"Teilnehmer-Speicherung fehlgeschlagen: {e}", exc_info=True)

        # Interview-Daten setzen
        entry.interview_at = data.get("interview_at")
        entry.interview_type = data.get("interview_type")
        entry.interview_location = data.get("interview_location")
        entry.interview_hint = data.get("interview_hint")
        entry.interview_participants = participants_list
        entry.interview_invite_by = data.get("interview_invite_by")
        # JSONB Mutability-Fix: SQLAlchemy erkennt JSONB-Aenderungen sonst nicht
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(entry, "interview_participants")
        logger.info(f"Interview-Daten gesetzt: participants={entry.interview_participants}, type={entry.interview_type}")
        # invite_sent wird NICHT hier gesetzt — erst nach erfolgreichem Senden im Background-Task

        # Activity loggen
        metadata = {
            "interview_at": entry.interview_at.isoformat() if entry.interview_at else None,
            "interview_type": entry.interview_type,
            "interview_invite_by": entry.interview_invite_by,
            "stage": entry.stage.value,
            **old_data,
        }
        activity = ATSActivity(
            activity_type=ActivityType.INTERVIEW_SCHEDULED,
            description=f"Interview geplant: {entry.interview_at.strftime('%d.%m.%Y %H:%M') if entry.interview_at else '?'}",
            ats_job_id=entry.ats_job_id,
            pipeline_entry_id=entry.id,
            candidate_id=entry.candidate_id,
            metadata_json=metadata,
        )
        db.add(activity)
        await db.flush()

        logger.info(f"Interview geplant: Entry {entry_id}, {entry.interview_at}, Typ: {entry.interview_type}")
        return {"success": True, "entry_id": str(entry.id)}

    except Exception as e:
        logger.error(f"Interview-Scheduling Fehler: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def send_interview_invite(entry_id, participants_override: list[dict] | None = None) -> dict:
    """Background-Task: Generiert Einladungstext via GPT + erstellt Calendar-Event via Graph.

    participants_override: Falls uebergeben, werden diese Teilnehmer verwendet statt aus DB.
    Das verhindert Race-Conditions wenn JSONB noch nicht committed ist.

    RAILWAY 30s PATTERN: Pro DB-Zugriff eigene Session, API-Calls OHNE offene Session.
    """
    try:
        from app.database import async_session_maker
        from app.models.ats_pipeline import ATSPipelineEntry
        from app.models.ats_job import ATSJob
        from app.models.company import Company
        from app.models.candidate import Candidate
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        # ── Session 1: Alle Daten laden → als Dicts → Session schliessen ──
        candidate_data = {}
        job_data = {}
        company_data = {}
        interview_data = {}
        participants = []

        async with async_session_maker() as db:
            entry = await db.get(
                ATSPipelineEntry,
                entry_id,
                options=[
                    selectinload(ATSPipelineEntry.ats_job).selectinload(ATSJob.company),
                    selectinload(ATSPipelineEntry.candidate),
                ],
            )
            if not entry:
                logger.error(f"Interview-Invite: Entry {entry_id} nicht gefunden")
                return {"success": False, "error": "Entry nicht gefunden"}

            # Kandidaten-Daten extrahieren
            cand = entry.candidate
            if cand:
                # Gender → Anrede Mapping
                anrede = ""
                if cand.gender:
                    gender_lower = cand.gender.strip().lower()
                    if gender_lower in ("herr", "männlich", "male", "m"):
                        anrede = "Herr"
                    elif gender_lower in ("frau", "weiblich", "female", "f"):
                        anrede = "Frau"
                candidate_data = {
                    "anrede": anrede,
                    "vorname": cand.first_name or "",
                    "nachname": cand.last_name or "",
                    "email": cand.email or "",
                    "full_name": f"{cand.first_name or ''} {cand.last_name or ''}".strip(),
                }

            # Job + Company Daten
            job = entry.ats_job
            if job:
                job_data = {"title": job.title or ""}
                comp = job.company
                if comp:
                    company_data = {
                        "name": comp.name or "",
                        "address": comp.address or "",
                        "postal_code": comp.postal_code or "",
                        "city": comp.city or "",
                    }

            # Interview-Daten
            interview_data = {
                "at": entry.interview_at,
                "type": entry.interview_type or "digital",
                "location": entry.interview_location or "",
                "hint": entry.interview_hint or "",
                "invite_by": entry.interview_invite_by or "recruiter",
                "event_id": entry.interview_event_id,
                "stage": entry.stage.value,
            }
            db_participants = entry.interview_participants or []
            # Override hat Vorrang (direkt aus Request, kein JSONB-Timing-Problem)
            participants = participants_override if participants_override is not None else db_participants
            logger.info(f"Interview-Invite: participants={participants} (Anzahl: {len(participants)}, override={participants_override is not None}, db={len(db_participants)})")
        # Session 1 geschlossen!

        if not candidate_data.get("email"):
            logger.error(f"Interview-Invite: Kandidat hat keine E-Mail (Entry {entry_id})")
            return {"success": False, "error": "Kandidat hat keine E-Mail"}

        # ── Alten Calendar-Event loeschen (bei Verschiebung) ──
        old_event_id = interview_data.get("event_id")
        if old_event_id:
            await delete_calendar_event(old_event_id)

        # ── GPT-4o: Einladungstext generieren (KEIN DB offen!) ──
        interview_nr = interview_data["stage"].replace("interview_", "")
        datum_str = ""
        uhrzeit_str = ""
        if interview_data["at"]:
            dt = interview_data["at"]
            # Deutsche Wochentage
            wochentage = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
            monate = ["Januar", "Februar", "Maerz", "April", "Mai", "Juni",
                       "Juli", "August", "September", "Oktober", "November", "Dezember"]
            datum_str = f"{wochentage[dt.weekday()]}, {dt.day}. {monate[dt.month - 1]} {dt.year}"
            uhrzeit_str = dt.strftime("%H:%M Uhr")

        # Teilnehmer-String bauen (einzeln pro Zeile)
        teilnehmer_str = ""
        teilnehmer_count = len(participants) if participants else 0
        if participants:
            parts = []
            for p in participants:
                anrede = p.get("anrede", "").strip()
                vorname = p.get("vorname", "").strip()
                nachname = p.get("nachname", "").strip()
                rolle = p.get("rolle", "").strip()
                name = f"{anrede} {vorname} {nachname}".strip() if vorname else f"{anrede} {nachname}".strip()
                if rolle:
                    parts.append(f"• {name} — {rolle}")
                else:
                    parts.append(f"• {name}")
            teilnehmer_str = "\n".join(parts)
            logger.info(f"Interview-Invite: teilnehmer_str = {teilnehmer_str}")

        # Ort-String
        if interview_data["type"] == "digital":
            ort_str = "Microsoft Teams (der Einladungslink ist in der Kalendereinladung enthalten)"
        else:
            ort_parts = [interview_data["location"]] if interview_data["location"] else []
            if not ort_parts and company_data:
                addr_parts = [company_data["address"], company_data["postal_code"], company_data["city"]]
                ort_parts = [p for p in addr_parts if p]
            ort_str = ", ".join(ort_parts) if ort_parts else "wird noch bekannt gegeben"

        user_prompt = f"""Erstelle eine Einladungs-E-Mail mit folgenden Daten:

Kandidat Anrede: {candidate_data.get('anrede', '')}
Kandidat Vorname: {candidate_data.get('vorname', '')}
Kandidat Nachname: {candidate_data.get('nachname', '')}
Firma: {company_data.get('name', 'Unbekannt')}
Jobtitel: {job_data.get('title', 'Unbekannt')}
Interview-Nummer: {interview_nr}
Datum: {datum_str}
Uhrzeit: {uhrzeit_str}
Art: {'Digitales Gespraech (Microsoft Teams)' if interview_data['type'] == 'digital' else 'Vor-Ort-Gespraech'}
Ort: {ort_str}
Empfangs-Hinweis: {interview_data.get('hint', '') or 'keiner'}

Gespraechspartner ({teilnehmer_count} Personen — ALLE muessen im Text erscheinen!):
{teilnehmer_str or 'werden noch bekannt gegeben'}

WICHTIG: Es sind {teilnehmer_count} Gespraechspartner — liste ALLE {teilnehmer_count} einzeln auf!"""

        invite_text = {"subject": "", "body_html": ""}
        try:
            async with httpx.AsyncClient(
                base_url="https://api.openai.com/v1",
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                timeout=30.0,
            ) as client:
                resp = await client.post(
                    "/chat/completions",
                    json={
                        "model": "gpt-4o",
                        "messages": [
                            {"role": "system", "content": INTERVIEW_INVITE_PROMPT},
                            {"role": "user", "content": user_prompt},
                        ],
                        "temperature": 0.3,
                        "max_tokens": 2000,
                        "response_format": {"type": "json_object"},
                    },
                )
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"]
                invite_text = json.loads(content)
                logger.info(f"GPT Einladungstext generiert: {invite_text.get('subject', '?')[:60]}")
        except Exception as e:
            logger.error(f"GPT Einladungstext Fehler: {e}", exc_info=True)
            # Fallback: Einfacher Text ohne GPT
            invite_text = {
                "subject": f"Bewerbungsgespraech {candidate_data.get('anrede', '')} {candidate_data.get('vorname', '')} {candidate_data.get('nachname', '')} — {company_data.get('name', '')}".replace("  ", " ").strip(),
                "body_html": f"<p>Hallo {candidate_data.get('anrede', '')} {candidate_data.get('nachname', '')},</p>"
                f"<p>hiermit lade ich Sie im Auftrag der Firma {company_data.get('name', '')} "
                f"zum Bewerbungsgespraech ein.</p>"
                f"<p><strong>Termin:</strong> {datum_str}, {uhrzeit_str}<br>"
                f"<strong>Art:</strong> {'Digital (Teams)' if interview_data['type'] == 'digital' else 'Vor Ort'}<br>"
                f"<strong>Ort:</strong> {ort_str}</p>"
                f"<p>Bei Fragen bin ich erreichbar unter 017680004741.</p>"
                f"<p>Viel Erfolg!<br>Mit freundlichen Gruessen</p>",
            }

        # ── Graph API: Calendar Event erstellen (KEIN DB offen!) ──
        is_digital = interview_data["type"] == "digital"
        event_result = await _create_interview_calendar_event(
            subject=invite_text["subject"],
            start_dt=interview_data["at"],
            body_html=invite_text["body_html"],
            candidate_email=candidate_data["email"],
            candidate_name=candidate_data["full_name"],
            participants=participants,
            is_online=is_digital,
        )

        if not event_result["success"]:
            logger.error(f"Calendar-Event Fehler: {event_result['error']}")
            # Trotzdem die Daten als gespeichert markieren
            async with async_session_maker() as db2:
                entry2 = await db2.get(ATSPipelineEntry, entry_id)
                if entry2:
                    entry2.interview_invite_sent = False  # Fehlgeschlagen
                    await db2.commit()
            return event_result

        # ── Session 2: Ergebnis speichern ──
        async with async_session_maker() as db2:
            entry2 = await db2.get(ATSPipelineEntry, entry_id)
            if entry2:
                entry2.interview_invite_sent = True
                entry2.interview_event_id = event_result.get("event_id", "")
                # Bei Digital: Teams-URL speichern
                teams_url = event_result.get("teams_url")
                if teams_url and is_digital:
                    entry2.interview_location = teams_url
                await db2.commit()
                logger.info(f"Interview-Einladung erfolgreich: Entry {entry_id}, Event-ID: {entry2.interview_event_id[:20] if entry2.interview_event_id else '?'}")
        # Session 2 geschlossen!

        return {"success": True, "event_id": event_result.get("event_id", "")}

    except Exception as e:
        logger.error(f"send_interview_invite Fehler: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def _create_interview_calendar_event(
    subject: str,
    start_dt: datetime,
    body_html: str,
    candidate_email: str,
    candidate_name: str,
    participants: list[dict],
    is_online: bool = False,
) -> dict:
    """Erstellt einen Kalender-Termin via Microsoft Graph Calendar API.

    Bei is_online=True wird isOnlineMeeting gesetzt → automatischer Teams-Link.
    Fallback: Falls 403 (fehlende Permission), Event ohne Teams-Link erstellen.

    Returns: {"success": True, "event_id": "...", "teams_url": "..." | None}
    """
    from app.services.email_service import EMAIL_SIGNATURE, MicrosoftGraphClient

    sender = settings.microsoft_sender_email
    if not sender:
        return {"success": False, "error": "Kein MICROSOFT_SENDER_EMAIL konfiguriert"}

    try:
        token = await MicrosoftGraphClient._get_access_token()
    except Exception as e:
        logger.error(f"Graph Token-Fehler: {e}")
        return {"success": False, "error": f"Token-Fehler: {e}"}

    graph_url = f"https://graph.microsoft.com/v1.0/users/{sender}/calendar/events"

    # Body mit Signatur
    full_body_html = f"""<div style="font-family: Arial, Helvetica, sans-serif;">
    {body_html}
    <br>
    {EMAIL_SIGNATURE}
</div>"""

    # Ende = Start + 1 Stunde (Standard-Interview-Dauer)
    end_dt = start_dt + timedelta(hours=1)

    event_payload = {
        "subject": subject,
        "body": {"contentType": "HTML", "content": full_body_html},
        "start": {"dateTime": start_dt.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "Europe/Berlin"},
        "end": {"dateTime": end_dt.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "Europe/Berlin"},
        "reminderMinutesBeforeStart": 15,
    }

    # Attendees: Kandidat + Firma-Teilnehmer mit Email
    attendees = [
        {"emailAddress": {"address": candidate_email, "name": candidate_name}, "type": "required"}
    ]
    for p in participants:
        email = p.get("email", "").strip()
        if email:
            name = f"{p.get('vorname', '')} {p.get('nachname', '')}".strip() or email
            attendees.append(
                {"emailAddress": {"address": email, "name": name}, "type": "required"}
            )
    event_payload["attendees"] = attendees

    # Online-Meeting (Teams-Link) — Try/Fallback
    if is_online:
        event_payload["isOnlineMeeting"] = True
        event_payload["onlineMeetingProvider"] = "teamsForBusiness"

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
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
            teams_url = None
            # Teams-URL aus Response extrahieren
            online_meeting = data.get("onlineMeeting")
            if online_meeting:
                teams_url = online_meeting.get("joinUrl")
            logger.info(f"Calendar-Event erstellt: {subject[:50]} (ID: {event_id[:20]})")
            return {"success": True, "event_id": event_id, "teams_url": teams_url}

        elif resp.status_code == 403 and is_online:
            # Fallback: Event OHNE Teams-Link erstellen
            logger.warning("Graph 403 bei isOnlineMeeting=true — Fallback ohne Teams-Link")
            event_payload.pop("isOnlineMeeting", None)
            event_payload.pop("onlineMeetingProvider", None)

            async with httpx.AsyncClient(timeout=20.0) as client2:
                resp2 = await client2.post(
                    graph_url,
                    json=event_payload,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                )
            if resp2.status_code == 201:
                data2 = resp2.json()
                event_id2 = data2.get("id", "")
                logger.info(f"Calendar-Event (Fallback ohne Teams) erstellt: {event_id2[:20]}")
                return {"success": True, "event_id": event_id2, "teams_url": None}
            else:
                error_text = resp2.text[:500]
                logger.error(f"Graph Calendar-Fallback-Fehler {resp2.status_code}: {error_text}")
                return {"success": False, "error": f"HTTP {resp2.status_code}: {error_text}"}
        else:
            error_text = resp.text[:500]
            logger.error(f"Graph Calendar-Fehler {resp.status_code}: {error_text}")
            return {"success": False, "error": f"HTTP {resp.status_code}: {error_text}"}

    except Exception as e:
        logger.error(f"Graph Calendar-Exception: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def delete_calendar_event(event_id: str) -> dict:
    """Loescht einen Kalender-Termin via Microsoft Graph (fuer Verschiebungen/Absagen)."""
    from app.services.email_service import MicrosoftGraphClient

    sender = settings.microsoft_sender_email
    if not sender or not event_id:
        return {"success": False, "error": "Kein Sender oder Event-ID"}

    try:
        token = await MicrosoftGraphClient._get_access_token()
        graph_url = f"https://graph.microsoft.com/v1.0/users/{sender}/calendar/events/{event_id}"

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.delete(
                graph_url,
                headers={"Authorization": f"Bearer {token}"},
            )

        if resp.status_code in (204, 200):
            logger.info(f"Calendar-Event geloescht: {event_id[:20]}")
            return {"success": True}
        else:
            logger.warning(f"Calendar-Event Delete Fehler {resp.status_code}: {resp.text[:200]}")
            return {"success": False, "error": f"HTTP {resp.status_code}"}

    except Exception as e:
        logger.error(f"Calendar-Event Delete Exception: {e}")
        return {"success": False, "error": str(e)}
