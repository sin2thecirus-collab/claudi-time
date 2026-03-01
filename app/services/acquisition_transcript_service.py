"""AcquisitionTranscriptService — Verknuepft Webex-Transkripte mit Akquise-Jobs.

Flow:
1. Phone-Lookup → CompanyContact finden
2. Neuesten AcquisitionCall fuer diesen Contact (letzte 2h, ohne Transcript) → job_id
3. Transcript + Summary auf AcquisitionCall speichern
4. 17-Fragen-Extraktion via GPT (eigene DB-Session wg. Railway 30s Timeout!)
5. Ergebnis auf Job.qualification_answers speichern (additiv, ueberschreibt nie manuell)
6. SSE-Event fuer Live-Update im Browser
"""

import logging
import re
from datetime import datetime, timedelta, timezone
from uuid import UUID

logger = logging.getLogger(__name__)


def _normalize_phone(phone: str) -> str:
    """Letzte 8 Ziffern fuer Vergleich (identisch zu routes_n8n_webhooks)."""
    if not phone:
        return ""
    digits = re.sub(r"\D", "", phone)
    return digits[-8:] if len(digits) >= 8 else digits


async def process_transcript(
    phone_number: str,
    transcript: str,
    call_summary: str | None = None,
    duration_seconds: int | None = None,
    webex_recording_id: str | None = None,
) -> dict:
    """Hauptmethode: Transkript zuordnen, speichern und Qualifizierung extrahieren.

    WICHTIG: Nutzt eigene DB-Sessions pro Schritt (Railway 30s Timeout!).
    """
    try:
        from app.database import async_session_maker
        from app.models.acquisition_call import AcquisitionCall
        from app.models.company_contact import CompanyContact
        from app.models.job import Job
        from sqlalchemy import select, text
    except ImportError as e:
        logger.exception(f"Import-Fehler: {e}")
        return {"success": False, "error": f"Import-Fehler: {e}"}

    now = datetime.now(timezone.utc)
    normalized = _normalize_phone(phone_number)
    if not normalized or len(normalized) < 4:
        return {"success": False, "error": "Ungueltige Telefonnummer"}

    # ── Schritt 1: Phone-Lookup → Contact + Company finden ──
    contact_id = None
    company_id = None
    contact_name = "Unbekannt"
    company_name = "Unbekannt"

    async with async_session_maker() as db:
        like_pattern = f"%{normalized}"
        result = await db.execute(
            text("""
                SELECT cc.id, cc.first_name, cc.last_name, cc.company_id, c.name
                FROM company_contacts cc
                LEFT JOIN companies c ON c.id = cc.company_id
                WHERE (REGEXP_REPLACE(COALESCE(cc.phone, ''), '[^0-9]', '', 'g') LIKE :pattern
                    OR REGEXP_REPLACE(COALESCE(cc.mobile, ''), '[^0-9]', '', 'g') LIKE :pattern)
                LIMIT 1
            """),
            {"pattern": like_pattern},
        )
        row = result.fetchone()
        if not row:
            logger.info(f"Kein Contact fuer Telefon {phone_number} gefunden")
            return {"success": False, "error": "Kein Contact gefunden", "phone": phone_number}

        contact_id = row[0]
        contact_name = f"{row[1] or ''} {row[2] or ''}".strip()
        company_id = row[3]
        company_name = row[4] or "Unbekannt"
    # Session geschlossen

    logger.info(f"Phone-Match: {phone_number} → {contact_name} ({company_name})")

    # ── Schritt 2: Neuesten AcquisitionCall finden (letzte 2h, ohne Transcript) ──
    call_id = None
    job_id = None
    job_position = "Unbekannt"

    async with async_session_maker() as db:
        result = await db.execute(
            select(AcquisitionCall.id, AcquisitionCall.job_id)
            .where(
                AcquisitionCall.contact_id == contact_id,
                AcquisitionCall.transcript.is_(None),
                AcquisitionCall.created_at >= (now - timedelta(hours=2)),
            )
            .order_by(AcquisitionCall.created_at.desc())
            .limit(1)
        )
        row = result.fetchone()

        if row:
            call_id = row[0]
            job_id = row[1]

        # Fallback: Wenn kein AcquisitionCall, suche den neuesten offenen Job dieser Firma
        if not job_id and company_id:
            result = await db.execute(
                select(Job.id, Job.position)
                .where(
                    Job.company_id == company_id,
                    Job.acquisition_source.isnot(None),
                    Job.deleted_at.is_(None),
                    Job.akquise_status.in_(["neu", "angerufen", "kontaktiert", "qualifiziert", "wiedervorlage"]),
                )
                .order_by(Job.akquise_status_changed_at.desc().nullsfirst())
                .limit(1)
            )
            job_row = result.fetchone()
            if job_row:
                job_id = job_row[0]
                job_position = job_row[1] or "Unbekannt"

        # Job-Position holen (wenn via AcquisitionCall gefunden)
        if job_id and job_position == "Unbekannt":
            result = await db.execute(
                select(Job.position).where(Job.id == job_id)
            )
            pos_row = result.fetchone()
            if pos_row:
                job_position = pos_row[0] or "Unbekannt"
    # Session geschlossen

    if not job_id:
        logger.info(f"Kein Akquise-Job fuer {contact_name} ({company_name}) gefunden")
        return {
            "success": False,
            "error": "Kein Akquise-Job gefunden",
            "contact": contact_name,
            "company": company_name,
        }

    logger.info(f"Verknuepfung: Call={call_id} → Job={job_id} ({job_position})")

    # ── Schritt 3: Transcript auf AcquisitionCall speichern ──
    if call_id:
        async with async_session_maker() as db:
            from sqlalchemy import update
            await db.execute(
                update(AcquisitionCall)
                .where(AcquisitionCall.id == call_id)
                .values(
                    transcript=transcript,
                    call_summary=call_summary,
                    webex_recording_id=webex_recording_id,
                )
            )
            await db.commit()
        logger.info(f"Transcript gespeichert auf AcquisitionCall {call_id}")
    # Session geschlossen

    # ── Schritt 4: 17-Fragen-Extraktion via GPT (KEINE DB-Session offen!) ──
    extraction_result = None
    try:
        async with async_session_maker() as db:
            from app.services.call_transcription_service import CallTranscriptionService
            service = CallTranscriptionService(db)
            extraction_result = await service.extract_17_questions(
                transcript=transcript,
                contact_name=contact_name,
                company_name=company_name,
                job_position=job_position,
            )
            await service.close()
    except Exception as e:
        logger.exception(f"17-Fragen-Extraktion Fehler: {e}")
    # Session geschlossen

    if not extraction_result or not extraction_result.get("success"):
        error_msg = extraction_result.get("error", "Unbekannt") if extraction_result else "Timeout"
        logger.warning(f"17-Fragen-Extraktion fehlgeschlagen: {error_msg}")
        return {
            "success": True,  # Transcript gespeichert, nur Extraktion fehlgeschlagen
            "call_id": str(call_id) if call_id else None,
            "job_id": str(job_id),
            "qualification_extracted": False,
            "error": f"Extraktion fehlgeschlagen: {error_msg}",
        }

    answers = extraction_result["qualification_answers"]

    # ── Schritt 5: Ergebnis auf Job.qualification_answers speichern (additiv) ──
    async with async_session_maker() as db:
        result = await db.execute(
            select(Job.qualification_answers).where(Job.id == job_id)
        )
        existing_row = result.fetchone()
        existing = (existing_row[0] if existing_row and existing_row[0] else {}) or {}

        # Additiver Merge: Auto ueberschreibt nie manuell
        from sqlalchemy import update
        for key, new_val in answers.items():
            if key not in existing:
                existing[key] = new_val
            elif existing[key].get("source") == "manual" and new_val.get("answer"):
                # Manuelle Antwort behalten, Auto-Antwort dazuschreiben
                existing[key]["auto_answer"] = new_val["answer"]
                existing[key]["source"] = "manual+auto"
            elif not existing[key].get("answer") and new_val.get("answer"):
                # Noch keine Antwort da → Auto-Antwort uebernehmen
                existing[key] = new_val

        await db.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(
                qualification_answers=existing,
                qualification_updated_at=now,
            )
        )

        # transcript_processed_at setzen
        if call_id:
            await db.execute(
                update(AcquisitionCall)
                .where(AcquisitionCall.id == call_id)
                .values(transcript_processed_at=now)
            )

        await db.commit()
    # Session geschlossen

    logger.info(
        f"Qualifizierung gespeichert: Job={job_id}, "
        f"{extraction_result['questions_answered']}/17 beantwortet, "
        f"Kosten=${extraction_result.get('cost_usd', 0):.4f}"
    )

    # ── Schritt 6: SSE-Event fuer Live-Update ──
    try:
        from app.services.acquisition_event_bus import publish
        await publish("qualification_extracted", {
            "job_id": str(job_id),
            "questions_answered": extraction_result["questions_answered"],
            "questions_total": 17,
        })
    except Exception as e:
        logger.warning(f"SSE-Event Fehler (nicht kritisch): {e}")

    return {
        "success": True,
        "call_id": str(call_id) if call_id else None,
        "job_id": str(job_id),
        "job_position": job_position,
        "contact": contact_name,
        "company": company_name,
        "qualification_extracted": True,
        "questions_answered": extraction_result["questions_answered"],
        "questions_total": 17,
        "cost_usd": extraction_result.get("cost_usd", 0),
    }
