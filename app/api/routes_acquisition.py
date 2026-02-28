"""Akquise-API Endpoints — Import, Leads, Calls, Emails, Conversion.

Prefix: /api/akquise (registriert in main.py)
"""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy import and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.acquisition_call import AcquisitionCall
from app.models.acquisition_email import AcquisitionEmail
from app.models.company import Company
from app.models.company_contact import CompanyContact
from app.models.job import Job

router = APIRouter(prefix="/akquise", tags=["Akquise"])


# ── Pydantic Models ──

class CallRequest(BaseModel):
    contact_id: uuid.UUID | None = None
    disposition: str
    call_type: str = "erstanruf"
    notes: str | None = None
    qualification_data: dict | None = None
    duration_seconds: int | None = None
    follow_up_date: datetime | None = None
    follow_up_note: str | None = None
    email_consent: bool = True
    extra_data: dict | None = None


class EmailDraftRequest(BaseModel):
    contact_id: uuid.UUID
    email_type: str = "initial"
    from_email: str | None = None


class EmailSendRequest(BaseModel):
    from_email: str | None = None


class EmailUpdateRequest(BaseModel):
    subject: str | None = None
    body_plain: str | None = None


class StatusUpdateRequest(BaseModel):
    status: str


class BatchStatusRequest(BaseModel):
    job_ids: list[uuid.UUID]
    status: str


class QualifyRequest(BaseModel):
    qualification_data: dict | None = None


# ── Import-Endpoints ──

@router.post("/import")
async def import_csv(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    """CSV von advertsdata.com hochladen und importieren."""
    import logging
    import traceback

    logger = logging.getLogger(__name__)

    if not file.filename or not file.filename.endswith(".csv"):
        raise HTTPException(400, "Nur CSV-Dateien erlaubt")

    content = await file.read()
    if len(content) > 50 * 1024 * 1024:
        raise HTTPException(400, "Datei zu gross (max 50 MB)")

    try:
        from app.services.acquisition_import_service import AcquisitionImportService

        service = AcquisitionImportService(db)
        result = await service.import_csv(content, filename=file.filename)
        return result
    except Exception as e:
        logger.error(f"CSV-Import Fehler: {e}\n{traceback.format_exc()}")
        return {
            "batch_id": None,
            "total_rows": 0,
            "imported": 0,
            "duplicates_refreshed": 0,
            "blacklisted_skipped": 0,
            "errors": 1,
            "error_details": [f"Server-Fehler: {str(e)[:500]}"],
        }


@router.post("/import/preview")
async def import_preview(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    """CSV analysieren ohne Import (Duplikate, bekannte Firmen)."""
    import logging
    import traceback

    logger = logging.getLogger(__name__)
    content = await file.read()

    try:
        from app.services.acquisition_import_service import AcquisitionImportService

        service = AcquisitionImportService(db)
        result = await service.preview_csv(content)
        return result
    except Exception as e:
        logger.error(f"CSV-Preview Fehler: {e}\n{traceback.format_exc()}")
        return {
            "total_rows": 0,
            "new_leads": 0,
            "duplicates": 0,
            "blacklisted": 0,
            "errors": 1,
            "error_details": [f"Server-Fehler: {str(e)[:500]}"],
            "known_companies": 0,
        }


@router.post("/import/rollback/{batch_id}")
async def import_rollback(
    batch_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Fehlerhaften Import rueckgaengig machen (nur Status='neu')."""
    from app.services.acquisition_import_service import AcquisitionImportService

    service = AcquisitionImportService(db)
    result = await service.rollback_batch(batch_id)
    return result


# ── Lead-Endpoints ──

@router.get("/leads")
async def list_leads(
    status: str | None = Query(None),
    tab: str | None = Query(None),
    city: str | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Liste der Akquise-Leads, gruppiert nach Company."""
    query = (
        select(Job)
        .where(
            Job.acquisition_source.isnot(None),
            Job.deleted_at.is_(None),
        )
        .options(selectinload(Job.company))
    )

    # Tab-Filter
    if tab == "heute":
        # Heute faellige + neue Leads mit hoher Prioritaet
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        query = query.where(
            Job.akquise_status.in_(["neu", "angerufen", "wiedervorlage"])
        )
    elif tab == "neu":
        query = query.where(Job.akquise_status == "neu")
    elif tab == "wiedervorlagen":
        query = query.where(Job.akquise_status == "wiedervorlage")
    elif tab == "nicht_erreicht":
        query = query.where(Job.akquise_status.in_(["email_gesendet", "email_followup"]))
    elif tab == "qualifiziert":
        query = query.where(Job.akquise_status.in_(["qualifiziert", "stelle_erstellt"]))
    elif tab == "archiv":
        query = query.where(Job.akquise_status.in_([
            "blacklist_hart", "blacklist_weich", "verloren", "followup_abgeschlossen",
        ]))

    # Expliziter Status-Filter
    if status:
        query = query.where(Job.akquise_status == status)

    # Stadt-Filter
    if city:
        query = query.where(func.lower(Job.city) == city.lower())

    # Sortierung: Prioritaet DESC, dann aelteste zuerst
    query = query.order_by(
        Job.akquise_priority.desc().nullslast(),
        Job.first_seen_at.asc().nullslast(),
    )

    # Pagination
    total_result = await db.execute(
        select(func.count()).select_from(query.subquery())
    )
    total = total_result.scalar_one()

    query = query.offset((page - 1) * per_page).limit(per_page)
    result = await db.execute(query)
    jobs = result.scalars().all()

    # Gruppierung nach Company
    groups: dict[str, dict] = {}
    for job in jobs:
        key = str(job.company_id) if job.company_id else job.company_name
        if key not in groups:
            groups[key] = {
                "company_id": str(job.company_id) if job.company_id else None,
                "company_name": job.company_name,
                "city": job.city,
                "company_status": job.company.acquisition_status if job.company else None,
                "jobs": [],
            }
        groups[key]["jobs"].append({
            "id": str(job.id),
            "position": job.position,
            "akquise_status": job.akquise_status,
            "akquise_priority": job.akquise_priority,
            "first_seen_at": job.first_seen_at.isoformat() if job.first_seen_at else None,
            "last_seen_at": job.last_seen_at.isoformat() if job.last_seen_at else None,
            "city": job.city,
            "employment_type": job.employment_type,
        })

    return {
        "groups": list(groups.values()),
        "total": total,
        "page": page,
        "pages": (total + per_page - 1) // per_page,
    }


@router.get("/leads/{job_id}")
async def get_lead_detail(
    job_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Lead-Detail mit Company, Contacts, Call-History, weitere Stellen."""
    job = await db.execute(
        select(Job)
        .where(Job.id == job_id)
        .options(selectinload(Job.company))
    )
    job = job.scalar_one_or_none()

    if not job:
        raise HTTPException(404, "Lead nicht gefunden")

    # Contacts laden
    contacts = []
    if job.company_id:
        contacts_result = await db.execute(
            select(CompanyContact)
            .where(CompanyContact.company_id == job.company_id)
            .order_by(CompanyContact.created_at.desc())
        )
        contacts = [
            {
                "id": str(c.id),
                "name": c.full_name,
                "position": c.position,
                "phone": c.phone,
                "mobile": c.mobile,
                "email": c.email,
                "contact_role": c.contact_role,
                "source": c.source,
            }
            for c in contacts_result.scalars().all()
        ]

    # Call-History
    from app.services.acquisition_call_service import AcquisitionCallService
    call_service = AcquisitionCallService(db)
    call_history = await call_service.get_call_history(job_id)

    # Email-History
    from app.services.acquisition_email_service import AcquisitionEmailService
    email_service = AcquisitionEmailService(db)
    email_history = await email_service.get_emails_for_job(job_id)

    # Weitere Stellen der Firma
    other_jobs = []
    if job.company_id:
        other_result = await db.execute(
            select(Job)
            .where(
                Job.company_id == job.company_id,
                Job.id != job_id,
                Job.acquisition_source.isnot(None),
                Job.deleted_at.is_(None),
            )
            .order_by(Job.akquise_priority.desc())
        )
        other_jobs = [
            {
                "id": str(j.id),
                "position": j.position,
                "akquise_status": j.akquise_status,
                "akquise_priority": j.akquise_priority,
            }
            for j in other_result.scalars().all()
        ]

    return {
        "job": {
            "id": str(job.id),
            "position": job.position,
            "company_name": job.company_name,
            "city": job.city,
            "postal_code": job.postal_code,
            "job_url": job.job_url,
            "job_text": job.job_text,
            "employment_type": job.employment_type,
            "industry": job.industry,
            "company_size": job.company_size,
            "akquise_status": job.akquise_status,
            "akquise_priority": job.akquise_priority,
            "first_seen_at": job.first_seen_at.isoformat() if job.first_seen_at else None,
            "anzeigen_id": job.anzeigen_id,
        },
        "company": {
            "id": str(job.company.id) if job.company else None,
            "name": job.company.name if job.company else job.company_name,
            "acquisition_status": job.company.acquisition_status if job.company else None,
            "phone": job.company.phone if job.company else None,
            "domain": job.company.domain if job.company else None,
        } if job.company else None,
        "contacts": contacts,
        "call_history": call_history,
        "email_history": email_history,
        "other_jobs": other_jobs,
    }


@router.patch("/leads/{job_id}/status")
async def update_lead_status(
    job_id: uuid.UUID,
    body: StatusUpdateRequest,
    db: AsyncSession = Depends(get_db),
):
    """Status eines Leads manuell aendern."""
    job = await db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "Lead nicht gefunden")

    from app.services.acquisition_call_service import AcquisitionCallService
    call_service = AcquisitionCallService(db)

    try:
        call_service._validate_transition(job.akquise_status, body.status)
    except ValueError as e:
        raise HTTPException(400, str(e))

    job.akquise_status = body.status
    job.akquise_status_changed_at = datetime.now(timezone.utc)
    await db.commit()

    return {"job_id": str(job_id), "new_status": body.status}


@router.patch("/leads/batch-status")
async def batch_update_status(
    body: BatchStatusRequest,
    db: AsyncSession = Depends(get_db),
):
    """Mehrere Leads gleichzeitig auf neuen Status setzen."""
    now = datetime.now(timezone.utc)
    updated = 0

    for jid in body.job_ids:
        job = await db.get(Job, jid)
        if job and job.acquisition_source:
            job.akquise_status = body.status
            job.akquise_status_changed_at = now
            updated += 1

    await db.commit()
    return {"updated": updated}


# ── Call-Endpoints ──

@router.post("/leads/{job_id}/call")
async def record_call(
    job_id: uuid.UUID,
    body: CallRequest,
    db: AsyncSession = Depends(get_db),
):
    """Anruf protokollieren mit Disposition."""
    from app.services.acquisition_call_service import AcquisitionCallService

    service = AcquisitionCallService(db)
    try:
        result = await service.record_call(
            job_id=job_id,
            contact_id=body.contact_id,
            disposition=body.disposition,
            call_type=body.call_type,
            notes=body.notes,
            qualification_data=body.qualification_data,
            duration_seconds=body.duration_seconds,
            follow_up_date=body.follow_up_date,
            follow_up_note=body.follow_up_note,
            email_consent=body.email_consent,
            extra_data=body.extra_data,
        )
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/leads/{job_id}/calls")
async def get_call_history(
    job_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Call-Historie fuer einen Lead."""
    from app.services.acquisition_call_service import AcquisitionCallService

    service = AcquisitionCallService(db)
    return await service.get_call_history(job_id)


# ── E-Mail-Endpoints ──

@router.post("/leads/{job_id}/email/draft")
async def generate_email_draft(
    job_id: uuid.UUID,
    body: EmailDraftRequest,
    db: AsyncSession = Depends(get_db),
):
    """GPT-E-Mail generieren (Vorschau)."""
    from app.services.acquisition_email_service import AcquisitionEmailService

    service = AcquisitionEmailService(db)
    try:
        result = await service.generate_draft(
            job_id=job_id,
            contact_id=body.contact_id,
            email_type=body.email_type,
            from_email=body.from_email,
        )
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.put("/leads/{job_id}/email/{email_id}")
async def update_email_draft(
    job_id: uuid.UUID,
    email_id: uuid.UUID,
    body: EmailUpdateRequest,
    db: AsyncSession = Depends(get_db),
):
    """E-Mail-Draft bearbeiten."""
    from app.services.acquisition_email_service import AcquisitionEmailService

    service = AcquisitionEmailService(db)
    try:
        return await service.update_draft(email_id, body.subject, body.body_plain)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/leads/{job_id}/email/{email_id}/send")
async def send_email(
    job_id: uuid.UUID,
    email_id: uuid.UUID,
    body: EmailSendRequest = EmailSendRequest(),
    db: AsyncSession = Depends(get_db),
):
    """E-Mail nach Vorschau senden."""
    from app.services.acquisition_email_service import AcquisitionEmailService

    service = AcquisitionEmailService(db)
    try:
        return await service.send_email(email_id, body.from_email)
    except ValueError as e:
        raise HTTPException(400, str(e))


# ── Conversion-Endpoints ──

@router.post("/leads/{job_id}/qualify")
async def qualify_lead(
    job_id: uuid.UUID,
    body: QualifyRequest = QualifyRequest(),
    db: AsyncSession = Depends(get_db),
):
    """Lead → ATSJob konvertieren."""
    from app.services.acquisition_qualify_service import AcquisitionQualifyService

    service = AcquisitionQualifyService(db)
    try:
        return await service.convert_to_ats(job_id, body.qualification_data)
    except ValueError as e:
        raise HTTPException(400, str(e))


# ── Utility-Endpoints ──

@router.get("/wiedervorlagen")
async def get_wiedervorlagen(
    db: AsyncSession = Depends(get_db),
):
    """Heutige faellige Wiedervorlagen."""
    from app.services.acquisition_call_service import AcquisitionCallService

    service = AcquisitionCallService(db)
    return await service.get_wiedervorlagen()


@router.get("/stats")
async def get_stats(
    db: AsyncSession = Depends(get_db),
):
    """Tages-KPIs (Anrufe, Erreicht, Qualifiziert, E-Mails, Conversion)."""
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    # Anrufe heute
    calls_today = await db.execute(
        select(func.count(AcquisitionCall.id)).where(
            AcquisitionCall.created_at >= today,
        )
    )

    # Erreicht (nicht_erreicht/besetzt/sekretariat zaehlen NICHT)
    reached_today = await db.execute(
        select(func.count(AcquisitionCall.id)).where(
            AcquisitionCall.created_at >= today,
            AcquisitionCall.disposition.notin_(["nicht_erreicht", "besetzt", "sekretariat", "falsche_nummer"]),
        )
    )

    # Qualifiziert heute
    qualified_today = await db.execute(
        select(func.count(AcquisitionCall.id)).where(
            AcquisitionCall.created_at >= today,
            AcquisitionCall.disposition.in_(["qualifiziert_erst", "voll_qualifiziert"]),
        )
    )

    # Emails heute
    emails_today = await db.execute(
        select(func.count(AcquisitionEmail.id)).where(
            AcquisitionEmail.sent_at >= today,
            AcquisitionEmail.status == "sent",
        )
    )

    # Offene Leads gesamt
    open_leads = await db.execute(
        select(func.count(Job.id)).where(
            Job.acquisition_source.isnot(None),
            Job.deleted_at.is_(None),
            Job.akquise_status.notin_([
                "blacklist_hart", "blacklist_weich", "stelle_erstellt",
                "verloren", "followup_abgeschlossen",
            ]),
        )
    )

    return {
        "calls_today": calls_today.scalar_one(),
        "reached_today": reached_today.scalar_one(),
        "qualified_today": qualified_today.scalar_one(),
        "emails_today": emails_today.scalar_one(),
        "open_leads": open_leads.scalar_one(),
        "date": today.strftime("%d.%m.%Y"),
    }


@router.get("/rueckruf/{phone}")
async def lookup_phone(
    phone: str,
    db: AsyncSession = Depends(get_db),
):
    """Telefonnummer-Lookup (Company/Contact/Jobs)."""
    from app.services.acquisition_call_service import AcquisitionCallService

    service = AcquisitionCallService(db)
    result = await service.lookup_phone(phone)

    if not result:
        raise HTTPException(404, "Telefonnummer nicht gefunden")

    return result


@router.get("/mailboxes")
async def get_mailboxes(
    db: AsyncSession = Depends(get_db),
):
    """Verfuegbare Postfaecher mit Tages-Limit-Status."""
    from app.config import settings

    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    # E-Mails pro Postfach heute zaehlen
    sent_counts = await db.execute(
        select(
            AcquisitionEmail.from_email,
            func.count(AcquisitionEmail.id),
        )
        .where(
            AcquisitionEmail.sent_at >= today,
            AcquisitionEmail.status == "sent",
        )
        .group_by(AcquisitionEmail.from_email)
    )
    counts = {row[0]: row[1] for row in sent_counts.all()}

    # Mailbox-Konfiguration
    mailboxes = [
        {"email": settings.microsoft_sender_email, "provider": "M365", "daily_limit": 100, "purpose": "Haupt"},
        {"email": "hamdard@sincirus-karriere.de", "provider": "IONOS", "daily_limit": 20, "purpose": "Erst-Mail"},
        {"email": "m.hamdard@sincirus-karriere.de", "provider": "IONOS", "daily_limit": 20, "purpose": "Follow-up"},
        {"email": "m.hamdard@jobs-sincirus.com", "provider": "IONOS", "daily_limit": 20, "purpose": "Break-up"},
        {"email": "hamdard@jobs-sincirus.com", "provider": "IONOS", "daily_limit": 20, "purpose": "Reserve"},
    ]

    for mb in mailboxes:
        mb["sent_today"] = counts.get(mb["email"], 0)
        mb["remaining"] = mb["daily_limit"] - mb["sent_today"]

    return {"mailboxes": mailboxes}


# ── SSE Event-Trigger (fuer n8n / Webex) ──


class IncomingCallEvent(BaseModel):
    phone: str
    caller_name: str | None = None


class ProcessReplyRequest(BaseModel):
    from_email: str  # Wer hat geantwortet
    to_email: str  # An welches Postfach
    subject: str | None = None
    in_reply_to: str | None = None  # Graph Message-ID der Original-Mail


class ProcessBounceRequest(BaseModel):
    original_to_email: str  # Empfaenger der gebouncten Mail
    bounce_reason: str | None = None


@router.post("/events/incoming-call")
async def trigger_incoming_call(
    body: IncomingCallEvent,
    db: AsyncSession = Depends(get_db),
):
    """Eingehenden Anruf melden → SSE-Event an Browser.

    Wird von n8n oder Webex-Webhook aufgerufen.
    Macht Phone-Lookup und pusht Ergebnis an alle SSE-Clients.
    """
    from app.services.acquisition_call_service import AcquisitionCallService
    from app.services.acquisition_event_bus import publish

    service = AcquisitionCallService(db)
    lookup_result = await service.lookup_phone(body.phone)

    event_data = {
        "phone": body.phone,
        "caller_name": body.caller_name,
        "found": lookup_result is not None,
        "lookup": lookup_result,
    }

    delivered = await publish("incoming-call", event_data)

    return {
        "event": "incoming-call",
        "delivered_to": delivered,
        "found": lookup_result is not None,
    }


# ── n8n-Endpoints (fuer Workflow-Automation) ──


@router.post("/n8n/send-scheduled-emails")
async def send_scheduled_emails(
    db: AsyncSession = Depends(get_db),
):
    """Sendet alle faelligen geplanten E-Mails (n8n Cron alle 15 Min).

    Prueft acquisition_emails WHERE status='scheduled' AND scheduled_send_at <= NOW().
    """
    from app.services.acquisition_email_service import AcquisitionEmailService

    service = AcquisitionEmailService(db)
    return await service.send_scheduled_emails()


@router.get("/n8n/followup-due")
async def get_followup_due(
    db: AsyncSession = Depends(get_db),
):
    """Leads die Follow-up oder Break-up E-Mail brauchen.

    Follow-up faellig: Erst-Mail gesendet vor 5-7 Tagen, kein Follow-up, kein Reply.
    Break-up faellig: Follow-up gesendet vor 7-10 Tagen, kein Break-up, kein Reply.
    """
    from datetime import timedelta

    now = datetime.now(timezone.utc)

    # --- Follow-up faellig ---
    followup_window_start = now - timedelta(days=7)
    followup_window_end = now - timedelta(days=5)

    result = await db.execute(
        select(AcquisitionEmail)
        .where(
            AcquisitionEmail.email_type == "initial",
            AcquisitionEmail.status == "sent",
            AcquisitionEmail.sent_at >= followup_window_start,
            AcquisitionEmail.sent_at <= followup_window_end,
        )
        .options(
            selectinload(AcquisitionEmail.job),
            selectinload(AcquisitionEmail.contact),
        )
    )
    initial_emails = result.scalars().all()

    followup_due = []
    for email in initial_emails:
        # Skip wenn Follow-up bereits existiert
        existing = await db.execute(
            select(func.count(AcquisitionEmail.id)).where(
                AcquisitionEmail.parent_email_id == email.id,
                AcquisitionEmail.email_type == "follow_up",
            )
        )
        if existing.scalar_one() > 0:
            continue

        followup_due.append({
            "email_id": str(email.id),
            "job_id": str(email.job_id) if email.job_id else None,
            "company_name": email.job.company_name if email.job else None,
            "position": email.job.position if email.job else None,
            "contact_name": email.contact.full_name if email.contact else None,
            "to_email": email.to_email,
            "sent_at": email.sent_at.isoformat() if email.sent_at else None,
            "days_since_sent": (now - email.sent_at).days if email.sent_at else None,
        })

    # --- Break-up faellig ---
    breakup_window_start = now - timedelta(days=10)
    breakup_window_end = now - timedelta(days=7)

    result2 = await db.execute(
        select(AcquisitionEmail)
        .where(
            AcquisitionEmail.email_type == "follow_up",
            AcquisitionEmail.status == "sent",
            AcquisitionEmail.sent_at >= breakup_window_start,
            AcquisitionEmail.sent_at <= breakup_window_end,
        )
        .options(
            selectinload(AcquisitionEmail.job),
            selectinload(AcquisitionEmail.contact),
        )
    )
    followup_emails = result2.scalars().all()

    breakup_due = []
    for email in followup_emails:
        # Skip wenn Break-up bereits existiert fuer diesen Job
        existing2 = await db.execute(
            select(func.count(AcquisitionEmail.id)).where(
                AcquisitionEmail.job_id == email.job_id,
                AcquisitionEmail.email_type == "break_up",
            )
        )
        if existing2.scalar_one() > 0:
            continue

        breakup_due.append({
            "email_id": str(email.id),
            "job_id": str(email.job_id) if email.job_id else None,
            "company_name": email.job.company_name if email.job else None,
            "position": email.job.position if email.job else None,
            "contact_name": email.contact.full_name if email.contact else None,
            "to_email": email.to_email,
            "sent_at": email.sent_at.isoformat() if email.sent_at else None,
            "days_since_sent": (now - email.sent_at).days if email.sent_at else None,
        })

    return {
        "followup_due": followup_due,
        "followup_count": len(followup_due),
        "breakup_due": breakup_due,
        "breakup_count": len(breakup_due),
    }


@router.post("/n8n/auto-followup")
async def auto_followup(
    db: AsyncSession = Depends(get_db),
):
    """Generiert und plant Follow-up/Break-up E-Mails automatisch (n8n Cron taeglich 09:00).

    1. Ruft intern followup-due Logik auf
    2. Generiert GPT-Draft fuer jeden faelligen Lead
    3. Scheduled den Versand (2h Delay via send_email)
    """
    from app.services.acquisition_email_service import AcquisitionEmailService
    from datetime import timedelta as td

    service = AcquisitionEmailService(db)
    now = datetime.now(timezone.utc)
    generated = 0
    errors = []

    # Follow-ups faellig (5-7 Tage nach Initial)
    fu_start = now - td(days=7)
    fu_end = now - td(days=5)

    result = await db.execute(
        select(AcquisitionEmail)
        .where(
            AcquisitionEmail.email_type == "initial",
            AcquisitionEmail.status == "sent",
            AcquisitionEmail.sent_at >= fu_start,
            AcquisitionEmail.sent_at <= fu_end,
        )
    )
    for email in result.scalars().all():
        # Skip wenn Follow-up bereits existiert
        existing = await db.execute(
            select(func.count(AcquisitionEmail.id)).where(
                AcquisitionEmail.parent_email_id == email.id,
                AcquisitionEmail.email_type == "follow_up",
            )
        )
        if existing.scalar_one() > 0:
            continue
        try:
            draft = await service.generate_draft(
                job_id=email.job_id,
                contact_id=email.contact_id,
                email_type="follow_up",
                from_email=email.from_email,
            )
            if draft.get("email_id"):
                await service.send_email(uuid.UUID(draft["email_id"]), email.from_email)
                generated += 1
        except Exception as e:
            errors.append({"job_id": str(email.job_id), "error": str(e), "type": "follow_up"})

    # Break-ups faellig (7-10 Tage nach Follow-up)
    bu_start = now - td(days=10)
    bu_end = now - td(days=7)

    result2 = await db.execute(
        select(AcquisitionEmail)
        .where(
            AcquisitionEmail.email_type == "follow_up",
            AcquisitionEmail.status == "sent",
            AcquisitionEmail.sent_at >= bu_start,
            AcquisitionEmail.sent_at <= bu_end,
        )
    )
    for email in result2.scalars().all():
        existing2 = await db.execute(
            select(func.count(AcquisitionEmail.id)).where(
                AcquisitionEmail.job_id == email.job_id,
                AcquisitionEmail.email_type == "break_up",
            )
        )
        if existing2.scalar_one() > 0:
            continue
        try:
            draft = await service.generate_draft(
                job_id=email.job_id,
                contact_id=email.contact_id,
                email_type="break_up",
                from_email=email.from_email,
            )
            if draft.get("email_id"):
                await service.send_email(uuid.UUID(draft["email_id"]), email.from_email)
                generated += 1
        except Exception as e:
            errors.append({"job_id": str(email.job_id), "error": str(e), "type": "break_up"})

    return {
        "generated": generated,
        "errors": len(errors),
        "error_details": errors[:10],
    }


@router.get("/n8n/eskalation-due")
async def get_eskalation_due(
    apply: bool = Query(False, description="Wenn true, Status auf followup_abgeschlossen setzen"),
    db: AsyncSession = Depends(get_db),
):
    """Leads mit E-Mail + 3 Anrufversuche danach ohne Erreichen.

    Kriterien:
    - Job-Status ist email_gesendet oder email_followup
    - Nach der letzten E-Mail wurden 3+ Anrufe gemacht
    - Alle Anrufe ohne Erreichen (nicht_erreicht, besetzt, mailbox, sekretariat)
    """
    jobs_result = await db.execute(
        select(Job)
        .where(
            Job.acquisition_source.isnot(None),
            Job.akquise_status.in_(["email_gesendet", "email_followup"]),
            Job.deleted_at.is_(None),
        )
    )
    jobs = jobs_result.scalars().all()

    eskalation_due = []
    now = datetime.now(timezone.utc)

    for job in jobs:
        # Letzte E-Mail fuer diesen Job
        last_email = await db.execute(
            select(AcquisitionEmail.sent_at)
            .where(
                AcquisitionEmail.job_id == job.id,
                AcquisitionEmail.status == "sent",
            )
            .order_by(AcquisitionEmail.sent_at.desc())
            .limit(1)
        )
        email_sent_at = last_email.scalar_one_or_none()
        if not email_sent_at:
            continue

        # Anrufe NACH der E-Mail zaehlen (nur nicht-erreicht Dispositionen)
        calls_after = await db.execute(
            select(func.count(AcquisitionCall.id))
            .where(
                AcquisitionCall.job_id == job.id,
                AcquisitionCall.created_at > email_sent_at,
                AcquisitionCall.disposition.in_([
                    "nicht_erreicht", "besetzt", "mailbox_besprochen", "sekretariat",
                ]),
            )
        )
        call_count = calls_after.scalar_one()

        if call_count >= 3:
            eskalation_due.append({
                "job_id": str(job.id),
                "company_name": job.company_name,
                "position": job.position,
                "city": job.city,
                "email_sent_at": email_sent_at.isoformat(),
                "call_attempts_after_email": call_count,
                "current_status": job.akquise_status,
            })

            if apply:
                job.akquise_status = "followup_abgeschlossen"
                job.akquise_status_changed_at = now

    if apply and eskalation_due:
        await db.commit()

    return {
        "eskalation_due": eskalation_due,
        "count": len(eskalation_due),
        "applied": apply,
    }


@router.post("/n8n/process-reply")
async def process_reply(
    body: ProcessReplyRequest,
    db: AsyncSession = Depends(get_db),
):
    """Reply auf Akquise-E-Mail verarbeiten (von n8n aufgerufen).

    n8n checkt die Inbox via Microsoft Graph, findet Antworten,
    und ruft diesen Endpoint auf. Backend matched und updated Status.
    """
    # Match ueber to_email (unser Postfach = from_email der Original-Mail)
    query = select(AcquisitionEmail).where(
        AcquisitionEmail.status == "sent",
        AcquisitionEmail.to_email == body.from_email,
    )

    # Wenn in_reply_to vorhanden, praeziser matchen
    if body.in_reply_to:
        query = query.where(AcquisitionEmail.graph_message_id == body.in_reply_to)

    query = query.order_by(AcquisitionEmail.sent_at.desc()).limit(1)
    query = query.options(selectinload(AcquisitionEmail.job))

    result = await db.execute(query)
    email = result.scalar_one_or_none()

    if not email:
        return {"matched": False, "reason": "Keine passende gesendete E-Mail gefunden"}

    # E-Mail-Status auf replied setzen
    email.status = "replied"

    # Job-Status auf kontaktiert setzen (wenn noch im E-Mail-Flow)
    if email.job and email.job.akquise_status in ("email_gesendet", "email_followup"):
        email.job.akquise_status = "kontaktiert"
        email.job.akquise_status_changed_at = datetime.now(timezone.utc)

    await db.commit()

    return {
        "matched": True,
        "email_id": str(email.id),
        "job_id": str(email.job_id) if email.job_id else None,
        "company_name": email.job.company_name if email.job else None,
        "new_job_status": email.job.akquise_status if email.job else None,
    }


@router.post("/n8n/check-inbox")
async def check_inbox(
    minutes: int = Query(15, description="Zeitfenster in Minuten"),
    db: AsyncSession = Depends(get_db),
):
    """Inbox aller Akquise-Mailboxen pruefen (Reply + Bounce Detection).

    Wird von n8n alle 15 Min aufgerufen.
    Nutzt Microsoft Graph API um neue Nachrichten zu lesen.
    Matched Replies/Bounces gegen gesendete acquisition_emails.

    WICHTIG: DB-Session wird NICHT offen gehalten waehrend Graph-API-Calls!
    Railway killt idle-in-transaction Connections nach 30s.
    Daher: Erst alle Nachrichten per Graph laden, DANN DB-Matching.
    """
    import httpx
    import re
    from datetime import timedelta
    from app.config import settings

    # ── Phase 1: Graph-Token holen (KEINE DB noetig) ──

    try:
        from app.services.email_service import MicrosoftGraphClient
        token = await MicrosoftGraphClient._get_access_token()
    except Exception:
        # Fallback: Token direkt holen
        tenant_id = settings.microsoft_tenant_id
        client_id = settings.microsoft_client_id
        client_secret = settings.microsoft_client_secret

        if not all([tenant_id, client_id, client_secret]):
            return {"error": "Microsoft Graph nicht konfiguriert", "replies": [], "bounces": []}

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "scope": "https://graph.microsoft.com/.default",
                },
            )
            if resp.status_code != 200:
                return {"error": f"Token-Fehler: {resp.status_code}", "replies": [], "bounces": []}
            token = resp.json()["access_token"]

    now = datetime.now(timezone.utc)
    since = (now - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Alle Akquise-Mailboxen
    mailboxes = [
        settings.microsoft_sender_email,
        "hamdard@sincirus-karriere.de",
        "m.hamdard@sincirus-karriere.de",
        "m.hamdard@jobs-sincirus.com",
        "hamdard@jobs-sincirus.com",
    ]
    mailboxes = [m for m in mailboxes if m]

    # ── Phase 2: Alle Nachrichten per Graph laden (KEINE DB-Session offen!) ──

    incoming_replies = []  # (from_addr, subject, mailbox)
    incoming_bounces = []  # (original_to, mailbox)

    async with httpx.AsyncClient(timeout=30.0) as http:
        for mailbox in mailboxes:
            try:
                resp = await http.get(
                    f"https://graph.microsoft.com/v1.0/users/{mailbox}/messages",
                    params={
                        "$filter": f"receivedDateTime ge {since}",
                        "$select": "from,subject,body,internetMessageHeaders,receivedDateTime",
                        "$top": "50",
                        "$orderby": "receivedDateTime desc",
                    },
                    headers={"Authorization": f"Bearer {token}"},
                )

                if resp.status_code != 200:
                    continue

                messages = resp.json().get("value", [])

                for msg in messages:
                    from_addr = msg.get("from", {}).get("emailAddress", {}).get("address", "")
                    subject = msg.get("subject", "")

                    # NDR/Bounce erkennen (Non-Delivery Report)
                    is_bounce = (
                        "MAILER-DAEMON" in from_addr.upper()
                        or "postmaster" in from_addr.lower()
                        or subject.startswith("Undeliverable:")
                        or subject.startswith("Delivery Status Notification")
                        or "Mail Delivery" in subject
                    )

                    if is_bounce:
                        body_content = msg.get("body", {}).get("content", "")
                        email_match = re.search(r'[\w.+-]+@[\w.-]+\.\w+', body_content)
                        original_to = email_match.group(0) if email_match else None
                        if original_to:
                            incoming_bounces.append((original_to, mailbox))
                    else:
                        if from_addr:
                            incoming_replies.append((from_addr, subject, mailbox))

            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(f"Inbox-Check {mailbox}: {e}")
                continue

    # ── Phase 3: DB-Matching (kurze DB-Session, KEINE externen Calls mehr) ──

    replies_found = []
    bounces_found = []

    # Bounces matchen
    for original_to, mailbox in incoming_bounces:
        bounce_email = await db.execute(
            select(AcquisitionEmail)
            .where(
                AcquisitionEmail.status == "sent",
                AcquisitionEmail.to_email == original_to,
            )
            .options(selectinload(AcquisitionEmail.contact))
            .order_by(AcquisitionEmail.sent_at.desc())
            .limit(1)
        )
        acq_email = bounce_email.scalar_one_or_none()

        if acq_email:
            acq_email.status = "bounced"
            if acq_email.contact:
                acq_email.contact.notes = (
                    (acq_email.contact.notes or "")
                    + f"\nE-Mail bounced ({now.strftime('%d.%m.%Y')})"
                )
            bounces_found.append({"email": original_to, "mailbox": mailbox})

    # Replies matchen
    for from_addr, subject, mailbox in incoming_replies:
        reply_email = await db.execute(
            select(AcquisitionEmail)
            .where(
                AcquisitionEmail.status == "sent",
                AcquisitionEmail.to_email == from_addr,
            )
            .options(selectinload(AcquisitionEmail.job))
            .order_by(AcquisitionEmail.sent_at.desc())
            .limit(1)
        )
        acq_email = reply_email.scalar_one_or_none()

        if acq_email:
            acq_email.status = "replied"

            if acq_email.job and acq_email.job.akquise_status in (
                "email_gesendet", "email_followup",
            ):
                acq_email.job.akquise_status = "kontaktiert"
                acq_email.job.akquise_status_changed_at = now

            replies_found.append({
                "from": from_addr,
                "subject": subject,
                "company_name": acq_email.job.company_name if acq_email.job else None,
                "mailbox": mailbox,
            })

    if replies_found or bounces_found:
        await db.commit()

    return {
        "checked_mailboxes": len(mailboxes),
        "replies": replies_found,
        "reply_count": len(replies_found),
        "bounces": bounces_found,
        "bounce_count": len(bounces_found),
        "since": since,
    }


@router.post("/n8n/process-bounce")
async def process_bounce(
    body: ProcessBounceRequest,
    db: AsyncSession = Depends(get_db),
):
    """Bounce einer Akquise-E-Mail verarbeiten (von n8n aufgerufen).

    n8n findet NDR (Non-Delivery-Report) in Inbox und ruft diesen Endpoint auf.
    Backend markiert E-Mail als bounced und Contact-E-Mail als ungueltig.
    """
    # Match ueber original_to_email
    result = await db.execute(
        select(AcquisitionEmail)
        .where(
            AcquisitionEmail.status == "sent",
            AcquisitionEmail.to_email == body.original_to_email,
        )
        .options(selectinload(AcquisitionEmail.contact))
        .order_by(AcquisitionEmail.sent_at.desc())
        .limit(1)
    )
    email = result.scalar_one_or_none()

    if not email:
        return {"matched": False, "reason": "Keine passende gesendete E-Mail gefunden"}

    # E-Mail-Status auf bounced setzen
    email.status = "bounced"

    # Contact-E-Mail als ungueltig markieren (Notes-Feld)
    if email.contact and email.contact.email:
        email.contact.notes = (
            (email.contact.notes or "")
            + f"\nE-Mail bounced ({datetime.now(timezone.utc).strftime('%d.%m.%Y')})"
            + (f": {body.bounce_reason}" if body.bounce_reason else "")
        )

    await db.commit()

    return {
        "matched": True,
        "email_id": str(email.id),
        "contact_email": body.original_to_email,
        "bounce_reason": body.bounce_reason,
    }


# ── Oeffentlicher Endpoint (kein Auth!) ──

@router.get("/unsubscribe/{token}", response_class=HTMLResponse)
async def unsubscribe(
    token: str,
    db: AsyncSession = Depends(get_db),
):
    """Abmelde-Link aus E-Mails (oeffentlich, kein Auth)."""
    from app.services.acquisition_email_service import AcquisitionEmailService

    service = AcquisitionEmailService(db)
    success = await service.handle_unsubscribe(token)

    if success:
        return HTMLResponse(
            "<html><body style='font-family:sans-serif;text-align:center;padding:50px'>"
            "<h2>Erfolgreich abgemeldet</h2>"
            "<p>Sie erhalten keine weiteren E-Mails von uns.</p>"
            "</body></html>"
        )
    else:
        return HTMLResponse(
            "<html><body style='font-family:sans-serif;text-align:center;padding:50px'>"
            "<h2>Link ungueltig</h2>"
            "<p>Dieser Abmelde-Link ist nicht mehr gueltig.</p>"
            "</body></html>",
            status_code=404,
        )


# ── Test-Modus Endpoints ──


class SimulateCallRequest(BaseModel):
    scenario: str  # nicht_erreicht, besetzt, sekretariat, kein_bedarf, interesse, qualifiziert, falsche_nummer, nie_wieder
    duration_seconds: int | None = None


@router.post("/test/simulate-call/{job_id}")
async def simulate_call(
    job_id: uuid.UUID,
    body: SimulateCallRequest,
    db: AsyncSession = Depends(get_db),
):
    """Simuliert einen Anruf im Test-Modus (kein echter Anruf).

    Erstellt einen acquisition_calls Eintrag mit simulierter Dauer.
    Nur im Test-Modus verfuegbar.
    """
    from app.services.acquisition_test_helpers import is_test_mode

    if not await is_test_mode(db):
        raise HTTPException(403, "Nur im Test-Modus verfuegbar")

    # Szenario → Disposition + Dauer
    scenarios = {
        "nicht_erreicht": ("nicht_erreicht", 15),
        "besetzt": ("besetzt_mailbox", 5),
        "sekretariat": ("sekretariat", 45),
        "kein_bedarf": ("erreicht_kein_bedarf", 120),
        "interesse": ("erreicht_interesse", 180),
        "qualifiziert": ("erreicht_qualifiziert", 300),
        "falsche_nummer": ("falsche_nummer", 3),
        "nie_wieder": ("nie_wieder", 30),
    }

    if body.scenario not in scenarios:
        raise HTTPException(400, f"Unbekanntes Szenario: {body.scenario}")

    disposition, default_duration = scenarios[body.scenario]
    duration = body.duration_seconds or default_duration

    # Job pruefen
    job = await db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "Job nicht gefunden")

    return {
        "simulated": True,
        "disposition": disposition,
        "duration_seconds": duration,
        "message": f"Anruf simuliert: {body.scenario} ({duration}s)",
    }


@router.post("/test/simulate-callback")
async def simulate_callback(
    phone: str = Query(..., description="Telefonnummer fuer Rueckruf-Simulation"),
    db: AsyncSession = Depends(get_db),
):
    """Simuliert einen eingehenden Rueckruf im Test-Modus.

    Loest das gleiche SSE-Event aus wie ein echter Rueckruf.
    Nur im Test-Modus verfuegbar.
    """
    from app.services.acquisition_test_helpers import is_test_mode

    if not await is_test_mode(db):
        raise HTTPException(403, "Nur im Test-Modus verfuegbar")

    # Phone-Lookup (gleiche Logik wie Rueckruf-Endpoint)
    from app.models.company_contact import CompanyContact

    result = await db.execute(
        select(CompanyContact)
        .where(CompanyContact.phone_normalized == phone)
        .limit(1)
    )
    contact = result.scalar_one_or_none()

    callback_data = {
        "phone": phone,
        "simulated": True,
    }

    if contact:
        # Company + offene Jobs laden
        company = await db.get(Company, contact.company_id) if contact.company_id else None
        jobs_result = await db.execute(
            select(Job)
            .where(
                Job.company_id == contact.company_id,
                Job.acquisition_source.isnot(None),
                Job.akquise_status.notin_(["verloren", "blacklist_hart"]),
            )
            .limit(5)
        )
        jobs = jobs_result.scalars().all()

        callback_data.update({
            "contact_name": contact.full_name,
            "company_name": company.name if company else None,
            "jobs": [{"id": str(j.id), "position": j.position} for j in jobs],
        })

    # SSE-Event pushen
    from app.services.acquisition_event_bus import publish

    await publish("incoming_call", callback_data)

    return callback_data


@router.get("/test/status")
async def test_mode_status(db: AsyncSession = Depends(get_db)):
    """Gibt den aktuellen Test-Modus-Status zurueck."""
    from app.services.acquisition_test_helpers import is_test_mode, get_test_email

    mode = await is_test_mode(db)
    email = await get_test_email(db) if mode else None

    return {
        "test_mode": mode,
        "test_email": email,
    }
