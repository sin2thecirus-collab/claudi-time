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
